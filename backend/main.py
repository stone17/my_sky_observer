import os
import sys

import astropy.units as u
import httpx
from astropy.coordinates import SkyCoord
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .astro_utils import AstroCalculator, auto_stretch_image
from .config import (
    CACHE_DIR,
    COMPONENTS_FILE,
    FAVORITES_USER_FILE,
    SETTINGS_DEFAULT_FILE,
    SETTINGS_JSON_LEGACY,
    SETTINGS_USER_FILE,
    USER_DATA_DIR,
    get_resource_path,
    invalidate_settings_cache,
    load_favorites,
    load_presets,
    load_settings,
    merge_settings_update,
    save_favorites,
    save_settings,
    validate_nina_host,
)
from .data_manager import CatalogManager
from .image_service import (
    download_image,
    get_cache_info,
    get_cache_status_info,
    get_custom_coord_hash,
    get_setup_hash,
    list_cached_sky_images,
    purge_image_cache,
    resolve_safe_cache_path,
    save_custom_image_meta,
)
from .models import (
    Camera,
    FetchImageRequest,
    FOVRectangle,
    Location,
    NinaFramingRequest,
    Telescope,
)
from .stream_service import (
    StreamSession,
    apply_stream_query_overrides,
    background_download_manager,
    build_single_object_details,
    calculator,
    catalogs,
    compute_fov_and_setup,
    get_sorted_objects,
)

# Legacy alias for older test scripts
SETTINGS_FILE = SETTINGS_JSON_LEGACY

app = FastAPI()

_default_cors_origins = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
]
_env_cors = os.environ.get("CORS_ORIGINS", "")
cors_origins = (
    [o.strip() for o in _env_cors.split(",") if o.strip()]
    if _env_cors.strip()
    else _default_cors_origins
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


# --- Settings & Profiles Endpoints ---
@app.get("/api/settings")
def get_settings():
    settings = load_settings()
    if "profiles" in settings:
        for p in settings["profiles"].values():
            if "catalogs" in p:
                del p["catalogs"]
    return JSONResponse(content=settings)


@app.post("/api/settings")
async def set_settings(request: Request):
    data = await request.json()
    current_settings = load_settings()
    try:
        updated_settings = merge_settings_update(current_settings, data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    save_settings(updated_settings)
    return {"status": "ok"}


@app.get("/api/favorites")
def get_favorites_endpoint():
    settings = load_settings()
    cs = settings.get("client_settings", {})
    fav_data = load_favorites()
    if not os.path.exists(FAVORITES_USER_FILE) and cs.get("favorites"):
        fav_data["favorites"] = cs.get("favorites", [])
        fav_data["filter_favorites_only"] = bool(cs.get("filter_favorites_only", False))
    return JSONResponse(content=fav_data)


@app.post("/api/favorites")
async def set_favorites_endpoint(request: Request):
    data = await request.json()
    if isinstance(data, list):
        favs = data
        filt = None
    elif isinstance(data, dict):
        favs = data.get("favorites", [])
        filt = data.get("filter_favorites_only")
    else:
        raise HTTPException(status_code=400, detail="Invalid favorites payload")

    saved_favs = save_favorites(favs, filter_favorites_only=filt)
    current_settings = load_settings()
    cs = current_settings.setdefault("client_settings", {})
    cs["favorites"] = saved_favs["favorites"]
    cs["filter_favorites_only"] = saved_favs["filter_favorites_only"]
    save_settings(current_settings)
    return JSONResponse(content={"status": "ok", **saved_favs})


@app.get("/api/profiles")
def get_profiles():
    settings = load_settings()
    profiles = settings.get("profiles", {})
    for p in profiles.values():
        if "catalogs" in p:
            del p["catalogs"]
    return profiles


@app.post("/api/profiles/{name}")
async def save_profile(name: str, request: Request):
    data = await request.json()
    if "catalogs" in data:
        del data["catalogs"]
    settings = load_settings()
    settings.setdefault("profiles", {})[name] = data
    save_settings(settings)
    return {"status": "saved"}


@app.delete("/api/profiles/{name}")
def delete_profile(name: str):
    settings = load_settings()
    if "profiles" in settings and name in settings["profiles"]:
        del settings["profiles"][name]
        save_settings(settings)
        return {"status": "deleted"}
    return JSONResponse(content={"error": "Not found"}, status_code=404)


@app.get("/api/presets")
def get_presets():
    return load_presets()


@app.get("/api/catalogs")
def get_catalogs():
    return CatalogManager.get_available_catalogs()


# --- Geocoding Endpoint ---
@app.get("/api/geocode")
async def geocode_city(city: str):
    if not city or len(city) < 2:
        return JSONResponse(
            content={"error": "City name is too short"}, status_code=400
        )
    url = "https://geocoding-api.open-meteo.com/v1/search"
    params = {"name": city, "count": 10, "language": "en", "format": "json"}
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        if "results" not in data:
            return JSONResponse(content=[], status_code=200)
        results = [
            {
                "name": r.get("name"),
                "country": r.get("country"),
                "admin1": r.get("admin1", ""),
                "latitude": round(r.get("latitude"), 4),
                "longitude": round(r.get("longitude"), 4),
            }
            for r in data["results"]
        ]
        return JSONResponse(content=results)
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)


# --- Streaming & Object Details Endpoints ---
@app.get("/api/stream-objects")
async def stream_objects_endpoint(
    request: Request,
    focal_length: float,
    sensor_width: float,
    sensor_height: float,
    latitude: float,
    longitude: float,
    sort_key: str = "time",
    catalogs: str = "",
):
    settings = apply_stream_query_overrides(
        load_settings(),
        request,
        focal_length,
        sensor_width,
        sensor_height,
        latitude,
        longitude,
        catalogs,
        sort_key,
    )
    session = StreamSession(request, settings)
    return StreamingResponse(session.generate_stream(), media_type="text/event-stream")


@app.post("/api/downloads/start")
async def start_background_downloads(request: Request):
    base = load_settings()
    body = {}
    try:
        raw_body = await request.body()
        if raw_body:
            parsed = await request.json()
            if isinstance(parsed, dict):
                body = parsed
    except Exception:
        body = {}

    qp = request.query_params
    focal_length = float(
        qp.get(
            "focal_length",
            body.get("focal_length", base.get("telescope", {}).get("focal_length", 400)),
        )
    )
    sensor_width = float(
        qp.get(
            "sensor_width",
            body.get("sensor_width", base.get("camera", {}).get("sensor_width", 23.5)),
        )
    )
    sensor_height = float(
        qp.get(
            "sensor_height",
            body.get("sensor_height", base.get("camera", {}).get("sensor_height", 15.6)),
        )
    )
    latitude = float(
        qp.get(
            "latitude",
            body.get("latitude", base.get("location", {}).get("latitude", 52.0)),
        )
    )
    longitude = float(
        qp.get(
            "longitude",
            body.get("longitude", base.get("location", {}).get("longitude", 10.0)),
        )
    )
    sort_key = str(
        qp.get(
            "sort_key",
            body.get("sort_key", base.get("client_settings", {}).get("sort_key", "time")),
        )
    )
    catalogs_val = qp.get("catalogs", body.get("catalogs", ""))
    if isinstance(catalogs_val, list):
        catalogs_str = ",".join(str(c) for c in catalogs_val)
    else:
        catalogs_str = str(catalogs_val or "")

    settings = apply_stream_query_overrides(
        base,
        request,
        focal_length,
        sensor_width,
        sensor_height,
        latitude,
        longitude,
        catalogs_str,
        sort_key,
    )
    if "download_mode" in body:
        settings["download_mode"] = str(body["download_mode"])
    elif "mode" in body:
        settings["download_mode"] = str(body["mode"])

    status = await background_download_manager.start_from_settings(settings)
    return JSONResponse(content={"status": "started", **status})


@app.get("/api/downloads/status")
def get_background_downloads_status():
    return JSONResponse(content=background_download_manager.get_status())


@app.post("/api/downloads/stop")
async def stop_background_downloads():
    status = await background_download_manager.stop()
    return JSONResponse(content={"status": "stopped", **status})


@app.get("/api/object-details/{obj_id}")
async def get_object_details(obj_id: str):
    settings = load_settings()
    settings.pop("catalogs", None)
    return await build_single_object_details(
        obj_id, settings, catalog_manager=catalogs
    )


# --- Image & Cache Endpoints ---
@app.get("/api/cache/status")
def get_cache_status(setup_hash: str = ""):
    effective_hash = setup_hash.strip()
    if not effective_hash:
        try:
            settings = load_settings()
            telescope = Telescope(**settings.get("telescope", {"focal_length": 400}))
            camera = Camera(
                **settings.get("camera", {"sensor_width": 23.5, "sensor_height": 15.6})
            )
            fov_info = compute_fov_and_setup(telescope, camera, settings)
            effective_hash = fov_info["setup_hash"]
        except Exception:
            effective_hash = ""
    return get_cache_status_info(CACHE_DIR, setup_hash=effective_hash or None)


@app.get("/api/cache/sky-images")
def get_cached_sky_images(setup_hash: str = ""):
    settings = load_settings()
    effective_hash = setup_hash.strip()
    fallback_fov = 1.0
    try:
        telescope = Telescope(**settings.get("telescope", {"focal_length": 400}))
        camera = Camera(
            **settings.get("camera", {"sensor_width": 23.5, "sensor_height": 15.6})
        )
        fov_info = compute_fov_and_setup(telescope, camera, settings)
        fallback_fov = float(fov_info.get("default_download_fov") or 1.0)
        if not effective_hash:
            effective_hash = fov_info.get("setup_hash", "")
    except Exception:
        pass

    images = list_cached_sky_images(
        cache_dir=CACHE_DIR,
        settings=settings,
        active_setup_hash=effective_hash or None,
        fallback_fov=fallback_fov,
    )
    return JSONResponse(content={"images": images})


@app.post("/api/cache/purge")
def purge_cache():
    purge_image_cache(CACHE_DIR)
    return {"status": "purged"}


@app.post("/api/fetch-custom-image")
async def fetch_custom_image(req: FetchImageRequest):
    if isinstance(req.ra, str):
        try:
            coords = SkyCoord(req.ra, req.dec, unit=(u.hourangle, u.deg))
            req.ra = coords.ra.deg
            req.dec = coords.dec.deg
        except Exception:
            pass

    effective_fov = max(float(req.fov), 0.25)
    setup_hash = get_custom_coord_hash(
        req.ra, req.dec, effective_fov, req.resolution, req.source
    )
    name = f"RADEC_{req.ra:.3f}_{req.dec:.3f}"
    try:
        url = await download_image(
            req.ra,
            req.dec,
            effective_fov,
            name,
            setup_hash,
            resolution=req.resolution,
            source=req.source,
            timeout=req.timeout,
        )
        save_custom_image_meta(
            setup_hash=setup_hash,
            ra=float(req.ra),
            dec=float(req.dec),
            fov=effective_fov,
            resolution=req.resolution,
            source=req.source,
            cache_dir=CACHE_DIR,
        )
        return {"url": url, "status": "cached", "image_fov": effective_fov}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/download-object")
async def download_object_endpoint(request: Request):
    try:
        data = await request.json()
        req_settings = data["settings"]
        override_fov = data.get("fov", req_settings.get("fov"))
        telescope = Telescope(**req_settings["telescope"])
        camera = Camera(**req_settings["camera"])
        fov_info = compute_fov_and_setup(
            telescope, camera, req_settings, override_fov=override_fov
        )

        obj = data["object"]
        url = await download_image(
            obj["ra"],
            obj["dec"],
            fov_info["download_fov"],
            obj["name"],
            fov_info["setup_hash"],
            resolution=fov_info["img_res"],
            source=fov_info["img_source"],
            timeout=fov_info["img_timeout"],
        )
        return {
            "url": url,
            "status": "cached",
            "image_fov": fov_info["download_fov"],
            "sensor_fov": fov_info["sensor_fov"],
            "fov_rectangle": fov_info["fov_rect"].model_dump(),
            "setup_hash": fov_info["setup_hash"],
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/cache/{setup_hash}/{image_filename}")
async def get_cached_image(setup_hash: str, image_filename: str):
    try:
        filepath = resolve_safe_cache_path(CACHE_DIR, setup_hash, image_filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not os.path.exists(filepath):
        return JSONResponse(content={"error": "File not found"}, status_code=404)
    return FileResponse(
        filepath, headers={"Cache-Control": "no-cache, no-store, must-revalidate"}
    )


# --- N.I.N.A. Integration Endpoint ---
@app.post("/api/nina/framing")
async def send_to_nina(request: NinaFramingRequest):
    settings = load_settings()
    raw_nina_host = (
        settings.get("nina_host") or os.environ.get("NINA_HOST") or "localhost"
    )
    try:
        nina_host = validate_nina_host(raw_nina_host)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    coord_url = f"http://{nina_host}:1888/v2/api/framing/set-coordinates"
    rot_url = f"http://{nina_host}:1888/v2/api/framing/set-rotation"

    try:
        if isinstance(request.ra, (float, int)) and isinstance(
            request.dec, (float, int)
        ):
            coords = SkyCoord(request.ra, request.dec, unit=(u.deg, u.deg))
        else:
            coords = SkyCoord(request.ra, request.dec, unit=(u.hourangle, u.deg))

        async with httpx.AsyncClient() as client:
            resp_coord = await client.get(
                coord_url,
                params={"RAangle": coords.ra.deg, "DecAngle": coords.dec.deg},
                timeout=2.0,
            )
            if resp_coord.status_code >= 400:
                raise HTTPException(
                    status_code=502,
                    detail=f"Coordinate error: {resp_coord.status_code}",
                )

            resp_rot = await client.get(
                rot_url, params={"rotation": -request.rotation}, timeout=2.0
            )
            if resp_rot.status_code >= 400:
                raise HTTPException(
                    status_code=502, detail=f"Rotation error: {resp_rot.status_code}"
                )

        return {"status": "success", "message": "Sent to N.I.N.A"}
    except HTTPException:
        raise
    except Exception as e:
        print(f"NINA Connection Error: {e}")
        raise HTTPException(status_code=502, detail="Could not connect to N.I.N.A.")


# --- Static Frontend Mount ---
static_relative = (
    "frontend/dist"
    if hasattr(sys, "_MEIPASS") or os.path.exists("frontend/dist")
    else "frontend"
)
static_dir = get_resource_path(static_relative)
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
