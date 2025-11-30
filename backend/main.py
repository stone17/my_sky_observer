import asyncio
import json
import traceback
import os
import hashlib
import requests
import shutil
import numpy as np
from fastapi import FastAPI, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse
from typing import Dict, Tuple, List, Optional, Union
import pandas as pd
from astropy.coordinates import SkyCoord, EarthLocation
import astropy.units as u
from pydantic import BaseModel
import math

from .models import Telescope, Camera, Location, FOVRectangle
from .data_manager import CatalogManager
from .astro_utils import AstroCalculator, auto_stretch_image
from astropy.time import Time

CACHE_DIR = "image_cache"
SETTINGS_FILE = "settings.json"
EQUIPMENT_PRESETS = {
    "cameras": {
        "ASI1600MM Pro": {"width": 21.9, "height": 16.7},
        "ASI294MC Pro": {"width": 19.1, "height": 13.0},
        "ASI183MC Pro": {"width": 13.2, "height": 8.8},
        "ASI533MC Pro": {"width": 11.3, "height": 11.3},
        "ASI2600MC Pro": {"width": 23.5, "height": 15.7},
        "ASI6200MC Pro": {"width": 36, "height": 24},
    }
}

app = FastAPI()
calculator = AstroCalculator()
catalogs = CatalogManager()

# --- Helper Functions ---

def load_settings() -> dict:
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, 'r') as f:
            settings = json.load(f)
            if 'image_padding' not in settings: settings['image_padding'] = 1.05
            if 'location' in settings and 'city_name' not in settings['location']:
                 settings['location']['city_name'] = ""
            return settings
    return {"telescope": {"focal_length": 1000}, "camera": {"sensor_width": 23.5, "sensor_height": 15.7}, "location": {"latitude": 55.70, "longitude": 13.19, "city_name": ""}, "catalogs": ["messier"], "min_altitude": 30.0, "image_padding": 1.05}

def save_settings(settings: dict):
    with open(SETTINGS_FILE, 'w') as f: json.dump(settings, f, indent=2)

def get_setup_hash(fov_w_deg: float, fov_h_deg: float, image_padding: float) -> str:
    # Readable cache folder name based on FOV and padding
    # Format: fov_W.WW_H.HH_pP.PP
    # We use 2 decimal places for the folder name to keep it clean but sufficiently unique
    return f"fov_{fov_w_deg:.2f}_{fov_h_deg:.2f}_p{image_padding:.2f}"

def get_cache_info(object_name: str, setup_hash: str) -> Tuple[str, str, str]:
    # Sanitize filename aggressively to prevent OS errors (especially on Windows)
    # Remove chars: < > : " / \ | ? * and replace spaces/slashes
    invalid_chars = '<>:"/\\|?*'
    sanitized_name = object_name
    for char in invalid_chars:
        sanitized_name = sanitized_name.replace(char, "")

    # Also clean up common coordinate symbols for cleaner filenames
    sanitized_name = sanitized_name.replace(" ", "_").replace("°", "d").replace("'", "m")

    filename = f"{sanitized_name}_{setup_hash}.jpg"
    setup_dir = os.path.join(CACHE_DIR, setup_hash)
    filepath = os.path.join(setup_dir, filename)
    url = f"/cache/{setup_hash}/{filename}"
    return url, filepath, setup_dir

async def download_and_cache_image(image_url: str, filepath: str, setup_dir: str):
    try:
        if not os.path.exists(setup_dir): os.makedirs(setup_dir, exist_ok=True)
        if not os.path.exists(filepath):
            print(f"    -> Downloading from {image_url}")
            response = await asyncio.to_thread(requests.get, image_url, timeout=60)
            response.raise_for_status()

            # Check for non-image content (SkyView often returns 200 OK with HTML error)
            content_type = response.headers.get("Content-Type", "")
            if content_type.startswith("text/"):
                print(f"    -> SkyView returned text/html instead of image. Preview: {response.text[:200]}")
                raise ValueError(f"SkyView returned non-image data: {content_type}")

            print("    -> Download successful. Stretching image...")
            try:
                stretched_bytes = await asyncio.to_thread(auto_stretch_image, response.content)
            except Exception as stretch_err:
                # Log content snippet if image identification fails
                print(f"    -> Auto-stretch failed. Content preview: {response.content[:100]}")
                raise stretch_err

            with open(filepath, 'wb') as f: f.write(stretched_bytes)
            print(f"    -> Image saved to {filepath}")
    except Exception as e:
        print(f"    -> ERROR in download_and_cache_image: {e}")
        raise e

async def download_image(ra: float, dec: float, fov: float, object_id: str, setup_hash: str) -> str:
    """
    Unified function to download or retrieve an image from cache.
    Returns the web-accessible URL of the image.
    """
    url, filepath, setup_dir = get_cache_info(object_id, setup_hash)
    
    if os.path.exists(filepath):
        return url

    # Not in cache, download it
    live_url = get_sky_survey_url(ra, dec, fov)
    await download_and_cache_image(live_url, filepath, setup_dir)
    return url

def get_sky_survey_url(ra_deg: float, dec_deg: float, fov_in_degrees: float) -> str:
    coords = SkyCoord(ra_deg, dec_deg, unit=(u.deg, u.deg))
    download_fov = max(fov_in_degrees, 0.25)
    base_url = "https://skyview.gsfc.nasa.gov/current/cgi/runquery.pl"
    params = f"Survey=dss2r&Position={coords.ra.deg:.5f},{coords.dec.deg:.5f}&Size={download_fov:.4f}&Pixels=512&Return=JPG"
    return f"{base_url}?{params}"

# --- Logic Extraction ---

def get_sorted_objects(settings: dict, telescope: Telescope, camera: Camera, location: Location) -> List[dict]:
    """
    Loads, filters, and sorts objects based on settings.
    """
    all_objects = catalogs.get_all_objects(settings.get('catalogs', []))
    if all_objects.empty:
        return []

    fov_w_arcmin, fov_h_arcmin = calculator.calculate_fov(telescope, camera)
    # filtered_objects = calculator.filter_objects_by_fov(all_objects, (fov_w_arcmin, fov_h_arcmin))
    # We now send ALL objects to the frontend to allow searching hidden/large objects
    filtered_objects = all_objects
    
    processed_objects = []
    sort_key = settings.get('sort_key', 'time')
    min_altitude = settings.get('min_altitude', 30.0)
    min_hours = settings.get('min_hours', 0.0)
    
    # Pre-calculate twilight periods for sorting if needed
    night_start, night_end = None, None
    altaz_frame, night_duration = None, 0.0

    # Always calculate twilight for hours_visible
    try:
        twilight = calculator.get_twilight_periods(location)
        if "night" in twilight:
            night_start = Time(twilight["night"][0])
            night_end = Time(twilight["night"][1])

            # Pre-calculate frame for fast batch processing
            result = calculator.prepare_night_frame(location, night_start, night_end)
            if result:
                altaz_frame, night_duration = result
    except Exception as e:
        print(f"Error calculating twilight: {e}")

    # Pre-computation for sorting
    # --- Vectorized Calculation ---
    
    # 1. Max Altitude (Vectorized)
    # Ensure columns are numeric
    all_decs = pd.to_numeric(filtered_objects['dec'], errors='coerce').fillna(0).values
    all_ras = pd.to_numeric(filtered_objects['ra'], errors='coerce').fillna(0).values
    
    max_alts = calculator.batch_get_max_altitude(all_decs, location.latitude)
    filtered_objects['max_altitude'] = max_alts
    
    # 2. Nightly Hours (Vectorized)
    if altaz_frame is not None:
        print(f"DEBUG: Calculating hours for {len(filtered_objects)} objects. Night duration: {night_duration}")
        hours_vis = calculator.batch_calculate_nightly_hours(
            all_ras, all_decs, altaz_frame, night_duration, min_altitude
        )
        print(f"DEBUG: Hours calculated. Shape: {hours_vis.shape}, Type: {type(hours_vis)}")
        print(f"DEBUG: Hours sample (first 5): {hours_vis[:5]}")
        filtered_objects['hours_visible'] = hours_vis
    else:
        print("DEBUG: altaz_frame is None. Setting hours_visible to 0.0")
        filtered_objects['hours_visible'] = 0.0

    # 3. Priority & Formatting
    # Vectorized priority assignment
    filtered_objects['priority'] = 2
    # Case-insensitive check for 'MESSIER'
    if 'catalog' in filtered_objects.columns:
        is_messier = filtered_objects['catalog'].astype(str).str.upper() == 'MESSIER'
        filtered_objects.loc[is_messier, 'priority'] = 1
        
    # Ensure magnitude is numeric
    filtered_objects['magnitude'] = pd.to_numeric(filtered_objects['mag'], errors='coerce').fillna(99)
    # Ensure size is numeric
    filtered_objects['size'] = pd.to_numeric(filtered_objects['maj_ax'], errors='coerce').fillna(0)

    # Replace all NaN/Infinity with None for valid JSON
    filtered_objects = filtered_objects.replace([np.nan, np.inf, -np.inf], None)

    # DEBUG: Check columns before conversion
    print(f"DEBUG: Columns before to_dict: {filtered_objects.columns.tolist()}")
    if 'hours_visible' not in filtered_objects.columns:
        print("CRITICAL: hours_visible column MISSING!")
    else:
        print(f"DEBUG: hours_visible column present. First value: {filtered_objects['hours_visible'].iloc[0]}")

    # Convert to list of dicts
    processed_objects = filtered_objects.to_dict('records')
    
    if processed_objects:
        print(f"DEBUG: First processed object keys: {list(processed_objects[0].keys())}")
        print(f"DEBUG: First processed object hours_visible: {processed_objects[0].get('hours_visible')}")

    sort_keys = sort_key.split(',') if sort_key else ['time']

    def sort_function(x):
        result = []
        for key in sort_keys:
            if key == 'brightness':
                result.append(x['magnitude']) # Ascending
            elif key == 'size':
                result.append(-x['size']) # Descending
            elif key == 'time' or key == 'altitude':
                result.append(-x['max_altitude']) # Descending
            elif key == 'hours_above':
                result.append(-x['hours_visible']) # Descending

        # Fallback
        result.append(x['priority'])
        return tuple(result)

    processed_objects.sort(key=sort_function)
        
    # DEBUG: Add debug info to first object
    if processed_objects:
        processed_objects[0]['_debug_info'] = {
            'columns_present': 'hours_visible' in filtered_objects.columns,
            'hours_val': processed_objects[0].get('hours_visible'),
            'night_duration': night_duration,
            'altaz_frame_ok': altaz_frame is not None
        }

    return processed_objects

# --- N.I.N.A Integration ---

class NinaFramingRequest(BaseModel):
    ra: Union[float, str]
    dec: Union[float, str]
    rotation: float = 0.0

@app.post("/api/nina/framing")
async def send_to_nina(request: NinaFramingRequest):
    nina_url = "http://localhost:1888/api/v1/framing/manualtarget"
    try:
        if isinstance(request.ra, (float, int)) and isinstance(request.dec, (float, int)):
             coords = SkyCoord(request.ra, request.dec, unit=(u.deg, u.deg))
        else:
             coords = SkyCoord(request.ra, request.dec, unit=(u.hourangle, u.deg))
             
        payload = {
            "RightAscension": coords.ra.deg,
            "Declination": coords.dec.deg,
            "Rotation": request.rotation
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid coordinates: {e}")

    try:
        async with httpx.AsyncClient() as client:
             resp = await client.post(nina_url, json=payload, timeout=2.0)
             if resp.status_code >= 400:
                 print(f"NINA Error: {resp.status_code} - {resp.text}")
                 raise HTTPException(status_code=502, detail=f"N.I.N.A returned error: {resp.status_code}")
    except Exception as e:
        print(f"Failed to contact NINA: {e}")
        raise HTTPException(status_code=502, detail="Could not connect to N.I.N.A. Is it running on port 1888?")

    return {"status": "success", "message": "Sent to N.I.N.A"}


async def event_stream(request: Request, settings: dict):
    try:
        print("\n--- Event Stream Started ---")
        telescope = Telescope(**settings['telescope'])
        camera = Camera(**settings['camera'])
        location = Location(**settings['location'])
        min_altitude = settings.get('min_altitude', 30.0)
        image_padding = settings.get('image_padding', 1.05)

        fov_w_arcmin, fov_h_arcmin = calculator.calculate_fov(telescope, camera)
        fov_w_deg, fov_h_deg = fov_w_arcmin / 60.0, fov_h_arcmin / 60.0
        
        setup_hash = get_setup_hash(fov_w_deg, fov_h_deg, image_padding)
        
        processed_objects = await asyncio.to_thread(get_sorted_objects, settings, telescope, camera, location)
        
        if not processed_objects:
            print("-> No objects found.")
            yield "event: close\ndata: No objects found\n\n"; return

        yield f"event: total\ndata: {len(processed_objects)}\n\n"
        
        # Calculate observing session times once
        from astroplan import Observer
        observer_loc = EarthLocation(lat=location.latitude * u.deg, lon=location.longitude * u.deg)
        observer = Observer(location=observer_loc)
        
        session_start, session_end = await asyncio.to_thread(calculator.get_observing_session, observer, Time.now())
        
        # Get twilight periods relative to this session start
        twilight = await asyncio.to_thread(calculator.get_twilight_periods, location, session_start)
        yield f"event: twilight_info\ndata: {json.dumps(twilight)}\n\n"

        # Also send current night times in a dedicated event for the UI to use globally
        yield f"event: night_times\ndata: {json.dumps(twilight)}\n\n"

        top_objects = processed_objects[:200]
        print(f"--- Streaming detailed data for {len(top_objects)} objects ---")
        
        # fov_w_deg and fov_h_deg already calculated above
        
        # Calculate download FOV: Max dimension + padding
        # User requested: "take the calculated FOV (camera+telescope) and then add 5% margin to the longer sider"
        max_fov_deg = max(fov_w_deg, fov_h_deg)
        download_fov = max(max_fov_deg * image_padding, 0.25)
        print(f"Calculated download FOV: {download_fov:.4f} degrees (Sensor: {fov_w_deg:.4f}x{fov_h_deg:.4f}, Padding: {image_padding})")

        fov_rect = FOVRectangle(
            width_percent=(fov_w_deg / download_fov) * 100.0, 
            height_percent=(fov_h_deg / download_fov) * 100.0
        )

        objects_to_download = []
        
        for obj_dict in top_objects:
            if await request.is_disconnected(): break
            object_id = obj_dict['id']
            
            cache_url, cache_filepath, _ = get_cache_info(object_id, setup_hash)
            is_cached = os.path.exists(cache_filepath)
            
            # Return dictionary with 'target' and 'moon' lists
            # Pass the synchronized session times
            altitude_data = await asyncio.to_thread(calculator.get_altitude_graph, obj_dict['ra'], obj_dict['dec'], location, 60, session_start, session_end)

            # Calculate hours above min based on graph
            hours_above_min = await asyncio.to_thread(calculator.calculate_time_above_altitude, altitude_data['target'], min_altitude, twilight)

            image_url, status = ("", "queued")
            if is_cached:
                image_url, status = (cache_url, "cached")
            else:
                # Download Mode Logic
                download_mode = settings.get('download_mode', 'selected')
                if download_mode == 'all':
                    objects_to_download.append(obj_dict)
                else:
                    # 'selected' or 'filtered' (without fetch-all trigger)
                    # Do not download automatically
                    status = "pending"

            size_str = f"{obj_dict['maj_ax']}'" + (f" x {obj_dict['min_ax']}'" if 'min_ax' in obj_dict and obj_dict['min_ax'] > 0 else "")
            
            result_obj = {
                "name": object_id, 
                "common_name": obj_dict.get('name', 'N/A'),
                "type": obj_dict.get('type', ''),
                "constellation": obj_dict.get('constellation', ''),
                "other_id": obj_dict.get('other_id', ''),
                "surface_brightness": obj_dict.get('surface_brightness', ''),
                "ra": obj_dict['ra'], 
                "dec": obj_dict['dec'], 
                "catalog": obj_dict['catalog'], 
                "size": size_str, 
                "image_url": image_url, 
                "altitude_graph": [p.model_dump() for p in altitude_data['target']],
                "moon_graph": [p.model_dump() for p in altitude_data['moon']],
                "fov_rectangle": fov_rect.model_dump(),
                "image_fov": download_fov, # Explicit FOV of the image
                "hours_above_min": hours_above_min, 
                "hours_visible": obj_dict.get('hours_visible', 0), # Vectorized calculation
                "maj_ax": obj_dict['maj_ax'], 
                "mag": obj_dict.get('mag', 99), 
                "max_altitude": obj_dict.get('max_altitude', 0), # Added for client-side sorting
                "setup_hash": setup_hash, 
                "status": status,
                "_debug_info": obj_dict.get('_debug_info')
            }
            yield f"event: object_data\ndata: {json.dumps(result_obj)}\n\n"

        print(f"\n--- Starting download for {len(objects_to_download)} images ---")

        # Parallel Download Management
        download_queue = asyncio.Queue()
        semaphore = asyncio.Semaphore(5) # Limit to 5 concurrent downloads

        async def worker(obj_dict):
             async with semaphore:
                if await request.is_disconnected(): return

                object_id = obj_dict['id']
                cache_url, cache_filepath, setup_dir = get_cache_info(object_id, setup_hash)

                await download_queue.put(f"event: image_status\ndata: {json.dumps({'name': object_id, 'status': 'downloading'})}\n\n")

                try:
                    # Use unified download function
                    # Note: download_image returns the URL, but we also need to know if it was cached or downloaded for the status update.
                    # Since download_image handles cache check internally, we can just call it.
                    # However, to maintain the specific status messages ('downloading' vs 'cached'), we might want to check cache first or modify download_image to return status.
                    # But the user asked to streamline. Let's trust download_image.
                    # Actually, for the UI feedback "downloading...", we want to emit that event BEFORE calling download_image if it's not cached.
                    # But download_image checks cache.
                    # Let's just emit "downloading" (it's harmless if it returns instantly from cache) or check cache here too.
                    # The outer loop already checked cache and only added to queue if not cached.
                    # So we can assume it needs downloading (or race condition, which is fine).
                    
                    await download_queue.put(f"event: image_status\ndata: {json.dumps({'name': object_id, 'status': 'downloading'})}\n\n")
                    
                    url = await download_image(obj_dict['ra'], obj_dict['dec'], download_fov, object_id, setup_hash)
                    
                    await download_queue.put(f"event: image_status\ndata: {json.dumps({'name': object_id, 'status': 'cached', 'url': url})}\n\n")
                except Exception as download_error:
                    print(f"  -> FAILED: {object_id} - {download_error}")
                    await download_queue.put(f"event: image_status\ndata: {json.dumps({'name': object_id, 'status': 'error'})}\n\n")

        # Start workers
        workers = [asyncio.create_task(worker(obj)) for obj in objects_to_download]

        # Monitor completion
        async def monitor_completion():
            await asyncio.gather(*workers)
            await download_queue.put(None) # Signal end

        asyncio.create_task(monitor_completion())

        # Stream events from queue
        while True:
            if await request.is_disconnected():
                break
            
            event = await download_queue.get()
            if event is None: break
            
            yield event

        print("--- Event Stream Finished ---")
        yield "event: close\ndata: Stream complete\n\n"
    except Exception as e:
        print("\n--- ERROR IN EVENT STREAM ---")
        traceback.print_exc()
        yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"

# --- Endpoints ---

@app.get("/api/settings")
def get_settings(): return JSONResponse(content=load_settings())

@app.post("/api/settings")
async def set_settings(request: Request):
    save_settings(await request.json())
    return {"status": "ok"}

@app.get("/api/geocode")
async def geocode_city(city: str):
    if not city or len(city) < 2: return JSONResponse(content={"error": "City name is too short"}, status_code=400)
    GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
    params = {"name": city, "count": 10, "language": "en", "format": "json"}
    try:
        response = requests.get(GEOCODING_URL, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        if "results" not in data: return JSONResponse(content=[], status_code=200)
        results = [{"name": res.get("name"), "country": res.get("country"), "admin1": res.get("admin1", ""), "latitude": round(res.get("latitude"), 4), "longitude": round(res.get("longitude"), 4)} for res in data["results"]]
        return JSONResponse(content=results)
    except requests.RequestException as e:
        return JSONResponse(content={"error": f"Geocoding API error: {e}"}, status_code=500)

@app.get("/api/stream-objects")
async def stream_objects(request: Request, focal_length: float, sensor_width: float, sensor_height: float, latitude: float, longitude: float, catalogs: str, sort_key: str, min_altitude: float = 30.0, min_hours: float = 0.0, image_padding: float = 1.1, download_mode: str = 'selected'):
    settings = {
        "telescope": {"focal_length": focal_length},
        "camera": {"sensor_width": sensor_width, "sensor_height": sensor_height},
        "location": {"latitude": latitude, "longitude": longitude},
        "catalogs": catalogs.split(',') if catalogs else [],
        "min_altitude": min_altitude,
        "min_hours": min_hours,
        "sort_key": sort_key,
        "image_padding": image_padding,
        "download_mode": download_mode
    }
    return StreamingResponse(event_stream(request, settings), media_type="text/event-stream")

# Profile Management
PROFILES_FILE = "profiles.json"

@app.get("/api/profiles")
def get_profiles():
    if os.path.exists(PROFILES_FILE):
        with open(PROFILES_FILE, 'r') as f: return json.load(f)
    return {}

@app.post("/api/profiles/{name}")
async def save_profile(name: str, request: Request):
    data = await request.json()
    profiles = {}
    if os.path.exists(PROFILES_FILE):
        with open(PROFILES_FILE, 'r') as f: profiles = json.load(f)
    profiles[name] = data
    with open(PROFILES_FILE, 'w') as f: json.dump(profiles, f, indent=2)
    return {"status": "saved"}

@app.delete("/api/profiles/{name}")
def delete_profile(name: str):
    if os.path.exists(PROFILES_FILE):
        with open(PROFILES_FILE, 'r') as f: profiles = json.load(f)
        if name in profiles:
            del profiles[name]
            with open(PROFILES_FILE, 'w') as f: json.dump(profiles, f, indent=2)
            return {"status": "deleted"}
    return JSONResponse(content={"error": "Profile not found"}, status_code=404)

@app.get("/api/presets")
def get_presets(): return EQUIPMENT_PRESETS

# Cache Management
@app.get("/api/cache/status")
def get_cache_status():
    if not os.path.exists(CACHE_DIR): return {"size_mb": 0, "count": 0}
    total_size = 0
    count = 0
    for root, dirs, files in os.walk(CACHE_DIR):
        for f in files:
            fp = os.path.join(root, f)
            total_size += os.path.getsize(fp)
            count += 1
    return {"size_mb": round(total_size / (1024*1024), 2), "count": count}

@app.post("/api/cache/purge")
def purge_cache():
    if os.path.exists(CACHE_DIR):
        shutil.rmtree(CACHE_DIR)
    os.makedirs(CACHE_DIR, exist_ok=True)
    return {"status": "purged"}

# Fetch Custom Image (with specific FOV)
class FetchImageRequest(BaseModel):
    ra: float
    dec: float
    fov: float # in degrees

@app.post("/api/fetch-custom-image")
async def fetch_custom_image(req: FetchImageRequest):
    setup_hash = f"custom_fov_{req.fov:.4f}"
    name = f"RADEC_{req.ra}_{req.dec}"
    
    print(f"Fetching custom image: FOV={req.fov}, Hash={setup_hash}")

    try:
        url = await download_image(req.ra, req.dec, req.fov, name, setup_hash)
        return {"url": url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/download-object")
async def download_object_endpoint(request: Request):
    try:
        data = await request.json()
        
        # Reconstruct settings to match stream logic
        telescope = Telescope(**data['settings']['telescope'])
        camera = Camera(**data['settings']['camera'])
        image_padding = data['settings'].get('image_padding', 1.05)
        
        fov_w_arcmin, fov_h_arcmin = calculator.calculate_fov(telescope, camera)
        fov_w_deg, fov_h_deg = fov_w_arcmin / 60.0, fov_h_arcmin / 60.0
        
        setup_hash = get_setup_hash(fov_w_deg, fov_h_deg, image_padding)
        max_fov_deg = max(fov_w_deg, fov_h_deg)
        download_fov = max(max_fov_deg * image_padding, 0.25)
        
        obj = data['object']
        print(f"On-demand download for {obj['name']} (FOV: {download_fov:.4f})")
        
        url = await download_image(obj['ra'], obj['dec'], download_fov, obj['name'], setup_hash)
        return {"url": url, "status": "cached"}
    except Exception as e:
        print(f"Error in on-demand download: {e}")
        raise HTTPException(status_code=500, detail=str(e))


if not os.path.exists("frontend"): os.makedirs("frontend", exist_ok=True)

@app.get("/cache/{setup_hash}/{image_filename}")
async def get_cached_image(setup_hash: str, image_filename: str):
    filepath = os.path.join(CACHE_DIR, setup_hash, image_filename)
    if not os.path.exists(filepath): return JSONResponse(content={"error": "File not found"}, status_code=404)
    # Disable caching to prevent browser from holding onto wrong images during development/debugging
    return FileResponse(filepath, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

static_dir = "frontend/dist" if os.path.exists("frontend/dist") else "frontend"
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")

import httpx
