import asyncio
import json
import traceback
import os
import hashlib
import requests
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse
from typing import Dict, Tuple
import pandas as pd
from astropy.coordinates import SkyCoord
import astropy.units as u

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

def load_settings() -> dict:
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, 'r') as f: return json.load(f)
    return {"telescope": {"focal_length": 1000}, "camera": {"sensor_width": 23.5, "sensor_height": 15.7}, "location": {"latitude": 55.70, "longitude": 13.19}, "catalogs": ["messier"], "min_altitude": 30.0}

def save_settings(settings: dict):
    with open(SETTINGS_FILE, 'w') as f: json.dump(settings, f, indent=2)

def get_setup_hash(telescope: Telescope, camera: Camera) -> str:
    s = f"f{telescope.focal_length}_w{camera.sensor_width}_h{camera.sensor_height}"
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
    print(f"    -> get_sky_survey_url: Calling SkyCoord with RA='{ra_hms}', Dec='{dec_dms}'")
    coords = SkyCoord(ra_hms, dec_dms, unit=(u.hourangle, u.deg))
    print("    -> get_sky_survey_url: SkyCoord parsing successful.")
    download_fov = max(fov_in_degrees, 0.25) * 1.5
    base_url = "https://skyview.gsfc.nasa.gov/current/cgi/runquery.pl"
    params = f"Survey=dss2r&Position={coords.ra.deg:.5f},{coords.dec.deg:.5f}&Size={download_fov:.4f}&Pixels=512&Return=JPG"
    return f"{base_url}?{params}"

async def event_stream(request: Request, settings: dict):
    try:
        print("\n--- Event Stream Started ---")
        telescope = Telescope(**settings['telescope'])
        camera = Camera(**settings['camera'])
        location = Location(**settings['location'])
        min_altitude = settings.get('min_altitude', 30.0)
        sort_key = settings.get('sort_key', 'time')
        setup_hash = get_setup_hash(telescope, camera)
        print(f"-> Settings: Sort='{sort_key}', Hash='{setup_hash}', Catalogs={settings.get('catalogs', [])}")
        
        all_objects = catalogs.get_all_objects(settings.get('catalogs', []))
        if all_objects.empty:
            print("-> No objects found in selected catalogs. Closing stream.")
            yield "event: close\ndata: No objects found\n\n"; return
        print(f"-> Loaded {len(all_objects)} total objects from data files.")

        fov_w_arcmin, fov_h_arcmin = calculator.calculate_fov(telescope, camera)
        filtered_objects = calculator.filter_objects_by_fov(all_objects, (fov_w_arcmin, fov_h_arcmin))
        print(f"-> Filtered to {len(filtered_objects)} objects based on FOV.")
        
        yield f"event: total\ndata: {len(filtered_objects)}\n\n"
        yield f"event: twilight_info\ndata: {json.dumps(await asyncio.to_thread(calculator.get_twilight_periods, location))}\n\n"

        print("--- STAGE 1: Starting fast pre-computation and sorting ---")
        processed_objects = []
        for i, (_, obj) in enumerate(filtered_objects.iterrows()):
            if await request.is_disconnected(): break
            if i > 0 and i % 100 == 0: yield f"event: processing_progress\ndata: {i}\n\n"
            
            temp_obj = obj.to_dict()
            if sort_key == 'time':
                temp_obj['sort_metric'] = calculator.get_max_altitude(temp_obj['dec'], location.latitude)
            elif sort_key == 'brightness':
                temp_obj['sort_metric'] = pd.to_numeric(temp_obj.get('mag', 99), errors='coerce')
            else:
                temp_obj['sort_metric'] = temp_obj['maj_ax']
            processed_objects.append(temp_obj)
        print(f"-> Pre-computation complete for {len(processed_objects)} objects.")

        if sort_key == 'brightness':
            processed_objects.sort(key=lambda x: x['sort_metric'])
        else:
            processed_objects.sort(key=lambda x: x['sort_metric'], reverse=True)
        print("-> Sorting complete.")

        top_objects = processed_objects[:200]
        print(f"--- STAGE 2: Sliced to top {len(top_objects)} objects. Streaming detailed data... ---")
        
        fov_w_deg, fov_h_deg = fov_w_arcmin / 60.0, fov_h_arcmin / 60.0
        largest_fov_dim_deg = max(fov_w_deg, fov_h_deg)
        download_fov = largest_fov_dim_deg * 1.5
        fov_rect = FOVRectangle(width_percent=(fov_w_deg / download_fov) * 100.0, height_percent=(fov_h_deg / download_fov) * 100.0)

        objects_to_download = []
        for obj_dict in top_objects:
            if await request.is_disconnected(): break
            object_id = obj_dict['id']
            print(f"\n[+] Processing {object_id}...")
            
            cache_url, cache_filepath, _ = get_cache_info(object_id, setup_hash)
            is_cached = os.path.exists(cache_filepath)
            print(f"  -> Cache check for {cache_filepath}: {'HIT' if is_cached else 'MISS'}")
            
            altitude_graph = await asyncio.to_thread(calculator.get_altitude_graph, obj_dict['ra'], obj_dict['dec'], location)
            hours_above_min = await asyncio.to_thread(calculator.calculate_time_above_altitude, altitude_graph, min_altitude)

            image_url, status = ("", "queued")
            if is_cached:
                image_url, status = (cache_url, "cached")
            else:
                objects_to_download.append(obj_dict)

            size_str = f"{obj_dict['maj_ax']}'" + (f" x {obj_dict['min_ax']}'" if 'min_ax' in obj_dict and obj_dict['min_ax'] > 0 else "")
            
            result_obj = {"name": object_id, "ra": obj_dict['ra'], "dec": obj_dict['dec'], "catalog": obj_dict['catalog'], "size": size_str, "image_url": image_url, "altitude_graph": [ag.model_dump() for ag in altitude_graph], "fov_rectangle": fov_rect.model_dump(), "hours_above_min": hours_above_min, "maj_ax": obj_dict['maj_ax'], "mag": obj_dict.get('mag', 99), "setup_hash": setup_hash, "status": status }
            yield f"event: object_data\ndata: {json.dumps(result_obj)}\n\n"
            print(f"  -> Sent object_data for {object_id} with status '{status}'.")

        print(f"\n--- STAGE 3: Starting serial download for {len(objects_to_download)} missing images ---")
        for obj_dict in objects_to_download:
            if await request.is_disconnected(): break
            object_id = obj_dict['id']
            print(f"\n[*] Downloading image for {object_id}...")
            cache_url, cache_filepath, setup_dir = get_cache_info(object_id, setup_hash)
            
            yield f"event: image_status\ndata: {json.dumps({'name': object_id, 'status': 'downloading'})}\n\n"
            
            try:
                live_image_url = get_sky_survey_url(obj_dict['ra'], obj_dict['dec'], largest_fov_dim_deg)
                await download_and_cache_image(live_image_url, cache_filepath, setup_dir)
                print(f"  -> SUCCESS: Download complete for {object_id}.")
                yield f"event: image_status\ndata: {json.dumps({'name': object_id, 'status': 'cached', 'url': cache_url})}\n\n"
            except Exception as download_error:
                print(f"  -> FAILED: Download for {object_id} failed. Reason: {download_error}")
                yield f"event: image_status\ndata: {json.dumps({'name': object_id, 'status': 'error'})}\n\n"

        print("--- Event Stream Finished ---")
        yield "event: close\ndata: Stream complete\n\n"
    except Exception as e:
        print("\n--- CATASTROPHIC ERROR IN EVENT STREAM ---")
        traceback.print_exc()
        yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"

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
async def stream_objects(request: Request, focal_length: float, sensor_width: float, sensor_height: float, latitude: float, longitude: float, catalogs: str, sort_key: str, min_altitude: float = 30.0):
    settings = { "telescope": {"focal_length": focal_length}, "camera": {"sensor_width": sensor_width, "sensor_height": sensor_height}, "location": {"latitude": latitude, "longitude": longitude}, "catalogs": catalogs.split(',') if catalogs else [], "min_altitude": min_altitude, "sort_key": sort_key }
    return StreamingResponse(event_stream(request, settings), media_type="text/event-stream")

@app.get("/api/presets")
def get_presets(): return EQUIPMENT_PRESETS

if not os.path.exists("frontend"): os.makedirs("frontend", exist_ok=True)

@app.get("/cache/{setup_hash}/{image_filename}")
async def get_cached_image(setup_hash: str, image_filename: str):
    filepath = os.path.join(CACHE_DIR, setup_hash, image_filename)
    if not os.path.exists(filepath): return JSONResponse(content={"error": "File not found"}, status_code=404)
    return FileResponse(filepath, headers={"Cache-Control": "public, max-age=31536000, immutable"})

app.mount("/", StaticFiles(directory="frontend", html=True), name="static")