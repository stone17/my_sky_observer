import asyncio
import json
import traceback
import os
import hashlib
import requests
import shutil
from fastapi import FastAPI, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse
from typing import Dict, Tuple, List, Optional
import pandas as pd
from astropy.coordinates import SkyCoord
import astropy.units as u
from pydantic import BaseModel
import math

from .models import Telescope, Camera, Location, FOVRectangle
from .data_manager import CatalogManager
from .astro_utils import AstroCalculator, auto_stretch_image

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
            return settings
    return {"telescope": {"focal_length": 1000}, "camera": {"sensor_width": 23.5, "sensor_height": 15.7}, "location": {"latitude": 55.70, "longitude": 13.19}, "catalogs": ["messier"], "min_altitude": 30.0, "image_padding": 1.05}

def save_settings(settings: dict):
    with open(SETTINGS_FILE, 'w') as f: json.dump(settings, f, indent=2)

def get_setup_hash(telescope: Telescope, camera: Camera, image_padding: float) -> str:
    s = f"f{telescope.focal_length}_w{camera.sensor_width}_h{camera.sensor_height}_p{image_padding}"
    return hashlib.md5(s.encode()).hexdigest()[:12]

def get_cache_info(object_name: str, setup_hash: str) -> Tuple[str, str, str]:
    sanitized_name = object_name.replace(" ", "_").replace("/", "-")
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
            print("    -> Download successful. Stretching image...")
            stretched_bytes = await asyncio.to_thread(auto_stretch_image, response.content)
            with open(filepath, 'wb') as f: f.write(stretched_bytes)
            print(f"    -> Image saved to {filepath}")
    except Exception as e:
        print(f"    -> ERROR in download_and_cache_image: {e}")
        raise e

def get_sky_survey_url(ra_hms: str, dec_dms: str, fov_in_degrees: float) -> str:
    coords = SkyCoord(ra_hms, dec_dms, unit=(u.hourangle, u.deg))
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
    filtered_objects = calculator.filter_objects_by_fov(all_objects, (fov_w_arcmin, fov_h_arcmin))
    
    processed_objects = []
    sort_key = settings.get('sort_key', 'time')
    min_altitude = settings.get('min_altitude', 30.0)
    
    # Pre-computation for sorting
    for i, (_, obj) in enumerate(filtered_objects.iterrows()):
        temp_obj = obj.to_dict()
        
        priority = 2
        if str(temp_obj.get('catalog', '')).upper() == 'MESSIER':
            priority = 1
        
        # Metrics
        metric_alt = calculator.get_max_altitude(temp_obj['dec'], location.latitude)
        metric_mag = pd.to_numeric(temp_obj.get('mag', 99), errors='coerce')
        metric_size = temp_obj['maj_ax']

        # Fast approximation for "hours above altitude" for sorting purposes
        metric_hours = calculator.get_approx_hours_above(temp_obj['dec'], location.latitude, min_altitude)

        temp_obj['max_altitude'] = metric_alt
        temp_obj['magnitude'] = metric_mag
        temp_obj['size'] = metric_size
        temp_obj['hours_visible'] = metric_hours
        temp_obj['priority'] = priority

        processed_objects.append(temp_obj)

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
        
    return processed_objects

# --- N.I.N.A Integration ---

class NinaFramingRequest(BaseModel):
    ra: str
    dec: str
    rotation: float = 0.0

@app.post("/api/nina/framing")
async def send_to_nina(request: NinaFramingRequest):
    nina_url = "http://localhost:1888/api/v1/framing/manualtarget"
    try:
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

        setup_hash = get_setup_hash(telescope, camera, image_padding)
        
        processed_objects = await asyncio.to_thread(get_sorted_objects, settings, telescope, camera, location)
        
        if not processed_objects:
            print("-> No objects found.")
            yield "event: close\ndata: No objects found\n\n"; return

        yield f"event: total\ndata: {len(processed_objects)}\n\n"
        
        twilight = await asyncio.to_thread(calculator.get_twilight_periods, location)
        yield f"event: twilight_info\ndata: {json.dumps(twilight)}\n\n"

        top_objects = processed_objects[:200]
        print(f"--- Streaming detailed data for {len(top_objects)} objects ---")
        
        fov_w_arcmin, fov_h_arcmin = calculator.calculate_fov(telescope, camera)
        fov_w_deg, fov_h_deg = fov_w_arcmin / 60.0, fov_h_arcmin / 60.0
        fov_diag_deg = math.sqrt(fov_w_deg**2 + fov_h_deg**2)
        download_fov = max(fov_diag_deg * image_padding, 0.25)

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
            altitude_data = await asyncio.to_thread(calculator.get_altitude_graph, obj_dict['ra'], obj_dict['dec'], location)

            # Calculate hours above min based on graph
            hours_above_min = await asyncio.to_thread(calculator.calculate_time_above_altitude, altitude_data['target'], min_altitude)

            image_url, status = ("", "queued")
            if is_cached:
                image_url, status = (cache_url, "cached")
            else:
                objects_to_download.append(obj_dict)

            size_str = f"{obj_dict['maj_ax']}'" + (f" x {obj_dict['min_ax']}'" if 'min_ax' in obj_dict and obj_dict['min_ax'] > 0 else "")
            
            result_obj = {
                "name": object_id, 
                "ra": obj_dict['ra'], 
                "dec": obj_dict['dec'], 
                "catalog": obj_dict['catalog'], 
                "size": size_str, 
                "image_url": image_url, 
                "altitude_graph": [p.model_dump() for p in altitude_data['target']],
                "moon_graph": [p.model_dump() for p in altitude_data['moon']],
                "fov_rectangle": fov_rect.model_dump(), 
                "hours_above_min": hours_above_min, 
                "maj_ax": obj_dict['maj_ax'], 
                "mag": obj_dict.get('mag', 99), 
                "setup_hash": setup_hash, 
                "status": status 
            }
            yield f"event: object_data\ndata: {json.dumps(result_obj)}\n\n"

        print(f"\n--- Starting download for {len(objects_to_download)} images ---")
        for obj_dict in objects_to_download:
            if await request.is_disconnected(): break
            object_id = obj_dict['id']
            
            cache_url, cache_filepath, setup_dir = get_cache_info(object_id, setup_hash)
            
            yield f"event: image_status\ndata: {json.dumps({'name': object_id, 'status': 'downloading'})}\n\n"
            
            try:
                live_image_url = get_sky_survey_url(obj_dict['ra'], obj_dict['dec'], download_fov)
                await download_and_cache_image(live_image_url, cache_filepath, setup_dir)
                yield f"event: image_status\ndata: {json.dumps({'name': object_id, 'status': 'cached', 'url': cache_url})}\n\n"
            except Exception as download_error:
                print(f"  -> FAILED: {object_id} - {download_error}")
                yield f"event: image_status\ndata: {json.dumps({'name': object_id, 'status': 'error'})}\n\n"
                await asyncio.sleep(0.5)

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
async def stream_objects(request: Request, focal_length: float, sensor_width: float, sensor_height: float, latitude: float, longitude: float, catalogs: str, sort_key: str, min_altitude: float = 30.0, image_padding: float = 1.1):
    settings = {
        "telescope": {"focal_length": focal_length},
        "camera": {"sensor_width": sensor_width, "sensor_height": sensor_height},
        "location": {"latitude": latitude, "longitude": longitude},
        "catalogs": catalogs.split(',') if catalogs else [],
        "min_altitude": min_altitude,
        "sort_key": sort_key,
        "image_padding": image_padding
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
    ra: str
    dec: str
    fov: float # in degrees

@app.post("/api/fetch-custom-image")
async def fetch_custom_image(req: FetchImageRequest):
    setup_hash = f"custom_fov_{req.fov:.4f}"
    name = f"RADEC_{req.ra}_{req.dec}"
    url, filepath, setup_dir = get_cache_info(name, setup_hash)

    if not os.path.exists(filepath):
        try:
            live_url = get_sky_survey_url(req.ra, req.dec, req.fov)
            await download_and_cache_image(live_url, filepath, setup_dir)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    return {"url": url}


if not os.path.exists("frontend"): os.makedirs("frontend", exist_ok=True)

@app.get("/cache/{setup_hash}/{image_filename}")
async def get_cached_image(setup_hash: str, image_filename: str):
    filepath = os.path.join(CACHE_DIR, setup_hash, image_filename)
    if not os.path.exists(filepath): return JSONResponse(content={"error": "File not found"}, status_code=404)
    return FileResponse(filepath, headers={"Cache-Control": "public, max-age=31536000, immutable"})

static_dir = "frontend/dist" if os.path.exists("frontend/dist") else "frontend"
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")

import httpx
