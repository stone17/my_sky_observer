import asyncio
import json
import traceback
import os
import sys
import shutil
import yaml
import numpy as np
from fastapi import FastAPI, Request, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse
from typing import Dict, List, Optional, Union
import httpx
import requests
from astropy.coordinates import SkyCoord, EarthLocation
import astropy.units as u
from astropy.time import Time
from astroplan import Observer
from pydantic import BaseModel
import pandas as pd

from .models import Telescope, Camera, Location, FOVRectangle
from .data_manager import CatalogManager
from .astro_utils import AstroCalculator, auto_stretch_image

# --- Configuration & Globals ---
CACHE_DIR = "image_cache"
SETTINGS_USER_FILE = "settings_user.yaml"
SETTINGS_DEFAULT_FILE = "settings_default.yaml"
SETTINGS_JSON_LEGACY = "settings.json"
COMPONENTS_FILE = "components.yaml"

app = FastAPI()
calculator = AstroCalculator()
catalogs = CatalogManager()

# --- Helper Functions ---
def get_resource_path(relative_path):
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath("."), relative_path)

def get_setup_hash(fov_w_deg: float, fov_h_deg: float, image_padding: float, resolution: int, source: str) -> str:
    return f"fov_{fov_w_deg:.2f}_{fov_h_deg:.2f}_p{image_padding:.2f}_r{resolution}_{source}"

def get_cache_info(object_name: str, setup_hash: str):
    invalid_chars = '<>:"/\\|?*'
    sanitized_name = object_name
    for char in invalid_chars:
        sanitized_name = sanitized_name.replace(char, "")
    sanitized_name = sanitized_name.replace(" ", "_").replace("°", "d").replace("'", "m")

    filename = f"{sanitized_name}_{setup_hash}.jpg"
    setup_dir = os.path.join(CACHE_DIR, setup_hash)
    filepath = os.path.join(setup_dir, filename)
    url = f"/cache/{setup_hash}/{filename}"
    return url, filepath, setup_dir

async def download_image(ra: float, dec: float, fov: float, object_id: str, setup_hash: str, resolution: int = 512, source: str = "dss2r", timeout: int = 60) -> str:
    url, filepath, setup_dir = get_cache_info(object_id, setup_hash)
    if os.path.exists(filepath):
        return url

    coords = SkyCoord(ra, dec, unit=(u.deg, u.deg))
    download_fov = max(fov, 0.25)
    base_url = "https://skyview.gsfc.nasa.gov/current/cgi/runquery.pl"
    params = f"Survey={source}&Position={coords.ra.deg:.5f},{coords.dec.deg:.5f}&Size={download_fov:.4f}&Pixels={resolution}&Return=JPG"
    live_url = f"{base_url}?{params}"

    try:
        if not os.path.exists(setup_dir): os.makedirs(setup_dir, exist_ok=True)
        print(f"    -> Downloading {object_id} from SkyView...")
        
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(live_url)
            response.raise_for_status()

            content_type = response.headers.get("Content-Type", "")
            if "text" in content_type:
                raise ValueError(f"SkyView returned text/html: {response.text[:100]}")
            
            stretched_bytes = await asyncio.to_thread(auto_stretch_image, response.content)
            
            with open(filepath, 'wb') as f: 
                f.write(stretched_bytes)
        return url
    except Exception as e:
        print(f"    -> ERROR downloading {object_id}: {e}")
        raise e

# --- Settings ---
def load_settings() -> dict:
    settings = {}
    default_path = get_resource_path(SETTINGS_DEFAULT_FILE)
    if os.path.exists(default_path):
        try:
            with open(default_path, 'r') as f: settings = yaml.safe_load(f) or {}
        except Exception: pass

    if os.path.exists(SETTINGS_USER_FILE):
        try:
            with open(SETTINGS_USER_FILE, 'r') as f:
                user = yaml.safe_load(f) or {}
                for k, v in user.items():
                    if isinstance(v, dict) and k in settings and isinstance(settings[k], dict):
                        settings[k].update(v)
                    else:
                        settings[k] = v
        except Exception: pass

    if 'profiles' not in settings: settings['profiles'] = {}
    return settings

def save_settings(settings: dict):
    with open(SETTINGS_USER_FILE, 'w') as f:
        yaml.dump(settings, f, default_flow_style=False)

# --- Sort Logic ---
def get_sorted_objects(settings: dict, location: Location, telescope: Telescope, camera: Camera) -> List[dict]:
    raw_objects = catalogs.get_all_objects(settings.get('catalogs', []))
    if raw_objects.empty: return []

    observer = Observer(location=EarthLocation(lat=location.latitude*u.deg, lon=location.longitude*u.deg))
    try:
        session_start, session_end = calculator.get_observing_session(observer, Time.now())
        altaz_frame, night_duration = calculator.prepare_night_frame(location, session_start, session_end)
    except Exception:
        altaz_frame, night_duration = None, 0.0

    df = raw_objects.copy()
    all_decs = pd.to_numeric(df['dec'], errors='coerce').fillna(0).values
    df['max_altitude'] = calculator.batch_get_max_altitude(all_decs, location.latitude)
    
    if altaz_frame is not None:
        all_ras = pd.to_numeric(df['ra'], errors='coerce').fillna(0).values
        min_alt = settings.get('min_altitude', 30.0)
        df['hours_visible'] = calculator.batch_calculate_nightly_hours(all_ras, all_decs, altaz_frame, night_duration, min_alt)
    else:
        df['hours_visible'] = 0.0

    df['magnitude'] = pd.to_numeric(df['mag'], errors='coerce').fillna(99)
    df['size'] = pd.to_numeric(df['maj_ax'], errors='coerce').fillna(0)
    df = df.replace([np.nan, np.inf, -np.inf], None)
    
    objects = df.to_dict('records')
    sort_key = settings.get('sort_key', 'time')
    sort_keys = sort_key.split(',') if sort_key else ['time']
    
    def sort_func(x):
        res = []
        for k in sort_keys:
            if k == 'brightness': res.append(x.get('magnitude', 99))
            elif k == 'size': res.append(-(x.get('size') or 0))
            elif k == 'time' or k == 'altitude': res.append(-(x.get('max_altitude') or 0))
            elif k == 'hours_above': res.append(-(x.get('hours_visible') or 0))
        return tuple(res)

    objects.sort(key=sort_func)
    return objects

# --- Stream Session ---
class StreamSession:
    def __init__(self, request: Request, settings: dict):
        self.request = request
        self.settings = settings
        self.telescope = Telescope(**settings['telescope'])
        self.camera = Camera(**settings['camera'])
        self.location = Location(**settings['location'])
        
        self.img_res = settings.get('image_resolution', 512)
        self.img_source = settings.get('image_source', 'dss2r')
        self.img_padding = settings.get('image_padding', 1.05)
        self.img_timeout = settings.get('image_timeout', 60)
        
        fov_w_min, fov_h_min = calculator.calculate_fov(self.telescope, self.camera)
        self.fov_w_deg = fov_w_min / 60.0
        self.fov_h_deg = fov_h_min / 60.0
        max_fov = max(self.fov_w_deg, self.fov_h_deg)
        self.download_fov = max(max_fov * self.img_padding, 0.25)
        
        self.sensor_fov_data = {"w": self.fov_w_deg, "h": self.fov_h_deg}
        self.fov_rect = FOVRectangle(
            width_percent=(self.fov_w_deg / self.download_fov) * 100.0,
            height_percent=(self.fov_h_deg / self.download_fov) * 100.0
        )
        
        self.setup_hash = get_setup_hash(self.fov_w_deg, self.fov_h_deg, self.img_padding, self.img_res, self.img_source)
        self.all_objects = []
        self.top_objects = []
        self.download_list = []
        
        observer = Observer(location=EarthLocation(lat=self.location.latitude*u.deg, lon=self.location.longitude*u.deg))
        self.session_start, self.session_end = calculator.get_observing_session(observer, Time.now())
        self.twilight = calculator.get_twilight_periods(self.location, self.session_start)

    async def generate_stream(self):
        try:
            self.all_objects = await asyncio.to_thread(
                get_sorted_objects, self.settings, self.location, self.telescope, self.camera
            )
            
            if not self.all_objects:
                yield "event: close\ndata: No objects found\n\n"
                return

            if await self.request.is_disconnected(): return

            # Initial Metadata
            yield f"event: total\ndata: {len(self.all_objects)}\n\n"
            yield f"event: twilight_info\ndata: {json.dumps(self.twilight)}\n\n"
            yield f"event: night_times\ndata: {json.dumps(self.twilight)}\n\n"
            
            if await self.request.is_disconnected(): return
            yield self.get_initial_metadata_event()
            
            # Details loop (NOW ASYNC)
            self.prioritize_top_objects()
            async for item in self.stream_details():
                if await self.request.is_disconnected(): return
                yield item
            
            # Downloads loop (Async)
            async for item in self.stream_downloads():
                if await self.request.is_disconnected(): return
                yield item
            
            if not await self.request.is_disconnected():
                yield "event: close\ndata: Stream complete\n\n"

        except Exception as e:
            if "Disconnect" not in str(e):
                traceback.print_exc()
                try: 
                    if not await self.request.is_disconnected():
                        yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"
                except: pass

    def get_initial_metadata_event(self):
        metadata = []
        for obj in self.all_objects:
            size_str = f"{obj.get('maj_ax', 0)}'"
            if obj.get('min_ax', 0) > 0:
                size_str += f" x {obj['min_ax']}'"
                
            metadata.append({
                "name": obj['id'],
                "common_name": obj.get('name', 'N/A'),
                "type": obj.get('type', ''),
                "constellation": obj.get('constellation', ''),
                "other_id": obj.get('other_id', ''),
                "ra": obj['ra'],
                "dec": obj['dec'],
                "mag": obj.get('magnitude', 99),
                "size": size_str,
                "maj_ax": obj.get('maj_ax', 0),
                "max_altitude": obj.get('max_altitude', 0),
                "hours_visible": obj.get('hours_visible', 0),
                "status": "pending",
                "sensor_fov": self.sensor_fov_data,
                "image_fov": self.download_fov,
            })
        return f"event: catalog_metadata\ndata: {json.dumps(metadata)}\n\n"

    def prioritize_top_objects(self):
        mode = self.settings.get('download_mode', 'selected')
        min_alt = self.settings.get('min_altitude', 30)
        min_hours = self.settings.get('min_hours', 0.0)
        max_mag = self.settings.get('max_magnitude', 12.0)
        min_size = self.settings.get('min_size', 0.0)
        sel_types = self.settings.get('selected_types', [])
        
        # Normalize selected types for robust matching
        raw_types = self.settings.get('selected_types', [])
        sel_types = [t.strip().lower() for t in raw_types if t.strip()]
        
        def check_obj(o):
            if o.get('max_altitude', 0) < min_alt: return False
            if o.get('hours_visible', 0) < min_hours: return False
            
            mag = o.get('magnitude', 99)
            if mag > max_mag: return False
            
            if o.get('size', 0) < min_size: return False
            
            if sel_types:
                o_type = str(o.get('type', '')).strip().lower()
                if o_type not in sel_types: return False
            return True

        filtered_candidates = [o for o in self.all_objects if check_obj(o)]
        
        self.top_objects = filtered_candidates[:50]
        
        if mode == 'all':
            self.download_list = self.all_objects
        elif mode == 'filtered':
            self.download_list = filtered_candidates
        else:
            self.download_list = []

    # FIX: Made this an async generator
    async def stream_details(self):
        download_ids = set(o['id'] for o in self.download_list)
        
        for obj in self.top_objects:
            # Check disconnect per item
            if await self.request.is_disconnected(): return

            obj_id = obj['id']
            url, filepath, _ = get_cache_info(obj_id, self.setup_hash)
            is_cached = os.path.exists(filepath)
            
            # FIX: Run heavy math in thread so we don't block the loop
            alt_data = await asyncio.to_thread(
                calculator.get_altitude_graph,
                obj['ra'], obj['dec'], self.location, 60, self.session_start, self.session_end
            )
            hours = calculator.calculate_time_above_altitude(
                alt_data['target'], self.settings.get('min_altitude', 30), self.twilight
            )
            
            status = "cached" if is_cached else ("queued" if obj_id in download_ids else "pending")
            image_url = url if is_cached else ""
            
            detail = {
                "name": obj_id,
                "image_url": image_url,
                "altitude_graph": [p.model_dump() for p in alt_data['target']],
                "moon_graph": [p.model_dump() for p in alt_data['moon']],
                "fov_rectangle": self.fov_rect.model_dump(),
                "sensor_fov": self.sensor_fov_data,
                "image_fov": self.download_fov,
                "hours_above_min": hours,
                "setup_hash": self.setup_hash,
                "status": status
            }
            yield f"event: object_details\ndata: {json.dumps(detail)}\n\n"

    async def stream_downloads(self):
        to_download = []
        for obj in self.download_list:
            _, filepath, _ = get_cache_info(obj['id'], self.setup_hash)
            if not os.path.exists(filepath):
                to_download.append(obj)
        
        total = len(to_download)
        if total == 0: return

        yield f"event: download_progress\ndata: {json.dumps({'current': 0, 'total': total})}\n\n"

        queue = asyncio.Queue()
        semaphore = asyncio.Semaphore(5)
        completed = 0

        async def worker(obj):
            nonlocal completed
            async with semaphore:
                if await self.request.is_disconnected(): return
                obj_id = obj['id']
                await queue.put({"name": obj_id, "status": "downloading"})
                
                try:
                    url = await download_image(
                        obj['ra'], obj['dec'], self.download_fov, obj_id, self.setup_hash, 
                        self.img_res, self.img_source, self.img_timeout
                    )
                    await queue.put({"name": obj_id, "status": "cached", "url": url})
                except Exception:
                    await queue.put({"name": obj_id, "status": "error"})
                finally:
                    completed += 1
                    await queue.put({"progress": True, "current": completed, "total": total})

        tasks = [asyncio.create_task(worker(o)) for o in to_download]
        
        async def monitor():
            if tasks: await asyncio.gather(*tasks)
            await queue.put(None)
        
        asyncio.create_task(monitor())

        while True:
            msg = await queue.get()
            if msg is None: break
            if "progress" in msg:
                yield f"event: download_progress\ndata: {json.dumps(msg)}\n\n"
            else:
                yield f"event: image_status\ndata: {json.dumps(msg)}\n\n"

# --- API Endpoints ---
@app.get("/api/settings")
def get_settings(): return JSONResponse(content=load_settings())

@app.post("/api/settings")
async def set_settings(request: Request):
    save_settings(await request.json())
    return {"status": "ok"}

@app.get("/api/geocode")
async def geocode_city(city: str):
    if not city or len(city) < 2: return JSONResponse(content={"error": "City name is too short"}, status_code=400)
    url = "https://geocoding-api.open-meteo.com/v1/search"
    params = {"name": city, "count": 10, "language": "en", "format": "json"}
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        if "results" not in data: return JSONResponse(content=[], status_code=200)
        results = [{"name": r.get("name"), "country": r.get("country"), "admin1": r.get("admin1", ""), "latitude": round(r.get("latitude"), 4), "longitude": round(r.get("longitude"), 4)} for r in data["results"]]
        return JSONResponse(content=results)
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)

@app.get("/api/stream-objects")
async def stream_objects_endpoint(request: Request, focal_length: float, sensor_width: float, sensor_height: float, latitude: float, longitude: float, catalogs: str, sort_key: str):
    settings = load_settings()
    settings.update({
        "telescope": {"focal_length": focal_length},
        "camera": {"sensor_width": sensor_width, "sensor_height": sensor_height},
        "location": {"latitude": latitude, "longitude": longitude},
        "catalogs": catalogs.split(',') if catalogs else [],
        "sort_key": sort_key
    })
    
    qp = request.query_params
    if 'min_altitude' in qp: settings['min_altitude'] = float(qp['min_altitude'])
    if 'min_hours' in qp: settings['min_hours'] = float(qp['min_hours'])
    if 'image_resolution' in qp: settings['image_resolution'] = int(qp['image_resolution'])
    if 'max_magnitude' in qp: settings['max_magnitude'] = float(qp['max_magnitude'])
    if 'min_size' in qp: settings['min_size'] = float(qp['min_size'])
    if 'image_padding' in qp: settings['image_padding'] = float(qp['image_padding'])
    if 'image_timeout' in qp: settings['image_timeout'] = int(qp['image_timeout'])
    if 'download_mode' in qp: settings['download_mode'] = qp['download_mode']
    if 'image_source' in qp: settings['image_source'] = qp['image_source']
    
    if 'selected_types' in qp and qp['selected_types']:
        settings['selected_types'] = qp['selected_types'].split(',')
    
    session = StreamSession(request, settings)
    return StreamingResponse(session.generate_stream(), media_type="text/event-stream")

@app.get("/api/catalogs")
def get_catalogs(): return CatalogManager.get_available_catalogs()

@app.get("/api/profiles")
def get_profiles(): return load_settings().get('profiles', {})

@app.post("/api/profiles/{name}")
async def save_profile(name: str, request: Request):
    data = await request.json()
    settings = load_settings()
    settings['profiles'][name] = data
    save_settings(settings)
    return {"status": "saved"}

@app.delete("/api/profiles/{name}")
def delete_profile(name: str):
    settings = load_settings()
    if 'profiles' in settings and name in settings['profiles']:
        del settings['profiles'][name]
        save_settings(settings)
        return {"status": "deleted"}
    return JSONResponse(content={"error": "Not found"}, status_code=404)

@app.get("/api/presets")
def get_presets():
    comp_path = get_resource_path(COMPONENTS_FILE)
    if os.path.exists(comp_path):
        try:
            with open(comp_path, 'r') as f: return yaml.safe_load(f) or {}
        except Exception: pass
    return {}

@app.get("/api/cache/status")
def get_cache_status():
    if not os.path.exists(CACHE_DIR): return {"size_mb": 0, "count": 0}
    total_size = sum(os.path.getsize(os.path.join(r, f)) for r, _, files in os.walk(CACHE_DIR) for f in files)
    count = sum(len(files) for _, _, files in os.walk(CACHE_DIR))
    return {"size_mb": round(total_size / (1024*1024), 2), "count": count}

@app.post("/api/cache/purge")
def purge_cache():
    if os.path.exists(CACHE_DIR): shutil.rmtree(CACHE_DIR)
    os.makedirs(CACHE_DIR, exist_ok=True)
    return {"status": "purged"}

class FetchImageRequest(BaseModel):
    ra: Union[float, str]; dec: Union[float, str]; fov: float; resolution: int = 512; source: str = "dss2r"; timeout: int = 60

@app.post("/api/fetch-custom-image")
async def fetch_custom_image(req: FetchImageRequest):
    if isinstance(req.ra, str):
        try:
            coords = SkyCoord(req.ra, req.dec, unit=(u.hourangle, u.deg))
            req.ra = coords.ra.deg; req.dec = coords.dec.deg
        except Exception: pass
    
    setup_hash = f"custom_fov_{req.fov:.4f}_res{req.resolution}_{req.source}"
    name = f"RADEC_{req.ra}_{req.dec}"
    try:
        url = await download_image(req.ra, req.dec, req.fov, name, setup_hash, resolution=req.resolution, source=req.source, timeout=req.timeout)
        return {"url": url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/download-object")
async def download_object_endpoint(request: Request):
    try:
        data = await request.json()
        telescope = Telescope(**data['settings']['telescope'])
        camera = Camera(**data['settings']['camera'])
        padding = data['settings'].get('image_padding', 1.05)
        img_srv = data['settings'].get('image_server', {})
        
        fov_w_min, fov_h_min = calculator.calculate_fov(telescope, camera)
        fov_w, fov_h = fov_w_min/60.0, fov_h_min/60.0
        setup_hash = get_setup_hash(fov_w, fov_h, padding, img_srv.get('resolution', 512), img_srv.get('source', 'dss2r'))
        download_fov = max(max(fov_w, fov_h) * padding, 0.25)
        
        obj = data['object']
        url = await download_image(obj['ra'], obj['dec'], download_fov, obj['name'], setup_hash, resolution=img_srv.get('resolution', 512), source=img_srv.get('source', 'dss2r'), timeout=img_srv.get('timeout', 60))
        return {"url": url, "status": "cached"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

class NinaFramingRequest(BaseModel):
    ra: Union[float, str]; dec: Union[float, str]; rotation: float = 0.0

@app.post("/api/nina/framing")
async def send_to_nina(request: NinaFramingRequest):
    nina_url = "http://localhost:1888/api/v1/framing/manualtarget"
    try:
        if isinstance(request.ra, (float, int)) and isinstance(request.dec, (float, int)):
             coords = SkyCoord(request.ra, request.dec, unit=(u.deg, u.deg))
        else:
             coords = SkyCoord(request.ra, request.dec, unit=(u.hourangle, u.deg))
        
        async with httpx.AsyncClient() as client:
             resp = await client.post(nina_url, json={"RightAscension": coords.ra.deg, "Declination": coords.dec.deg, "Rotation": request.rotation}, timeout=2.0)
             if resp.status_code >= 400: raise HTTPException(status_code=502, detail=f"N.I.N.A error: {resp.status_code}")
        return {"status": "success", "message": "Sent to N.I.N.A"}
    except Exception as e:
        raise HTTPException(status_code=502, detail="Could not connect to N.I.N.A.")

@app.get("/cache/{setup_hash}/{image_filename}")
async def get_cached_image(setup_hash: str, image_filename: str):
    filepath = os.path.join(CACHE_DIR, setup_hash, image_filename)
    if not os.path.exists(filepath): return JSONResponse(content={"error": "File not found"}, status_code=404)
    return FileResponse(filepath, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})

static_relative = "frontend/dist" if hasattr(sys, '_MEIPASS') or os.path.exists("frontend/dist") else "frontend"
static_dir = get_resource_path(static_relative)
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")