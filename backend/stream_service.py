import asyncio
import json
import os
import traceback
from typing import Dict, List, Optional

import astropy.units as u
import numpy as np
import pandas as pd
from astroplan import Observer
from astropy.coordinates import EarthLocation
from astropy.time import Time
from fastapi import HTTPException, Request

from .astro_utils import AstroCalculator
from .data_manager import CatalogManager
from .image_service import (
    download_image,
    get_cache_info,
    get_setup_hash,
    validate_image_source,
)
from .models import Camera, FOVRectangle, Location, Telescope

calculator = AstroCalculator()
catalogs = CatalogManager()


def compute_fov_and_setup(
    telescope: Telescope, camera: Camera, settings: dict
) -> dict:
    """Compute FOV geometry, download FOV, FOV rectangle, and image setup hash."""
    img_srv = settings.get("image_server", {})
    img_res = int(settings.get("image_resolution", img_srv.get("resolution", 1024)))
    img_source = validate_image_source(
        settings.get("image_source", img_srv.get("source", "dss2r"))
    )
    img_padding = float(settings.get("image_padding", 1.15))
    img_timeout = int(settings.get("image_timeout", img_srv.get("timeout", 120)))

    fov_w_min, fov_h_min = calculator.calculate_fov(telescope, camera)
    fov_w_deg = fov_w_min / 60.0
    fov_h_deg = fov_h_min / 60.0
    download_fov = max(max(fov_w_deg, fov_h_deg) * img_padding, 0.25)

    sensor_fov_data = {"w": fov_w_deg, "h": fov_h_deg}
    fov_rect = FOVRectangle(
        width_percent=(fov_w_deg / download_fov) * 100.0,
        height_percent=(fov_h_deg / download_fov) * 100.0,
    )
    setup_hash = get_setup_hash(
        fov_w_deg, fov_h_deg, img_padding, img_res, img_source
    )

    return {
        "fov_w_deg": fov_w_deg,
        "fov_h_deg": fov_h_deg,
        "download_fov": download_fov,
        "sensor_fov": sensor_fov_data,
        "fov_rect": fov_rect,
        "setup_hash": setup_hash,
        "img_res": img_res,
        "img_source": img_source,
        "img_padding": img_padding,
        "img_timeout": img_timeout,
    }


async def compute_object_detail_payload(
    obj: dict,
    location: Location,
    session_start: Time,
    session_end: Time,
    twilight: Dict[str, List[str]],
    min_altitude: float,
    fov_info: dict,
    download_ids: Optional[set] = None,
    include_aliases: bool = False,
) -> dict:
    """Shared async builder for an object's altitude graphs, FOV info, and cache status."""
    obj_id = obj["id"]
    setup_hash = fov_info["setup_hash"]
    url, filepath, _ = get_cache_info(obj_id, setup_hash)
    is_cached = os.path.exists(filepath)

    alt_data = await asyncio.to_thread(
        calculator.get_altitude_graph,
        obj["ra"],
        obj["dec"],
        location,
        60,
        session_start,
        session_end,
    )
    hours = calculator.calculate_time_above_altitude(
        alt_data["target"], min_altitude, twilight
    )

    if is_cached:
        status = "cached"
    elif download_ids is not None and obj_id in download_ids:
        status = "queued"
    else:
        status = "pending"

    detail = {
        "name": obj_id,
        "image_url": url if is_cached else "",
        "altitude_graph": [p.model_dump() for p in alt_data["target"]],
        "moon_graph": [p.model_dump() for p in alt_data["moon"]],
        "sun_graph": [p.model_dump() for p in alt_data.get("sun", [])],
        "fov_rectangle": fov_info["fov_rect"].model_dump(),
        "sensor_fov": fov_info["sensor_fov"],
        "image_fov": fov_info["download_fov"],
        "hours_above_min": hours,
        "setup_hash": setup_hash,
        "status": status,
    }
    if include_aliases:
        detail["aliases"] = obj.get("aliases", "")
    return detail


def get_sorted_objects(
    settings: dict,
    location: Location,
    telescope: Telescope,
    camera: Camera,
    catalog_manager: CatalogManager = catalogs,
) -> List[dict]:
    """Fetch, enrich with visibility/altitude metrics, and sort catalog objects."""
    raw_objects = catalog_manager.get_all_objects(settings.get("catalogs", []))
    if raw_objects.empty:
        return []

    observer = Observer(
        location=EarthLocation(
            lat=location.latitude * u.deg, lon=location.longitude * u.deg
        )
    )
    try:
        session_start, session_end = calculator.get_observing_session(
            observer, Time.now()
        )
        altaz_frame, night_duration = calculator.prepare_night_frame(
            location, session_start, session_end
        )
    except Exception:
        altaz_frame, night_duration = None, 0.0

    df = raw_objects.copy()
    all_decs = pd.to_numeric(df["dec"], errors="coerce").fillna(0).values
    df["max_altitude"] = calculator.batch_get_max_altitude(all_decs, location.latitude)

    if altaz_frame is not None:
        all_ras = pd.to_numeric(df["ra"], errors="coerce").fillna(0).values
        min_alt = settings.get("min_altitude", 30.0)
        df["hours_visible"] = calculator.batch_calculate_nightly_hours(
            all_ras, all_decs, altaz_frame, night_duration, min_alt
        )
    else:
        df["hours_visible"] = 0.0

    df["magnitude"] = pd.to_numeric(df["mag"], errors="coerce").fillna(99)
    df["size"] = pd.to_numeric(df["maj_ax"], errors="coerce").fillna(0)
    df = df.replace([np.nan, np.inf, -np.inf], None)

    objects = df.to_dict("records")
    sort_key = settings.get("sort_key", "time")
    sort_keys = sort_key.split(",") if sort_key else ["time"]

    def sort_func(x):
        res = []
        for k in sort_keys:
            if k == "brightness":
                res.append(x.get("magnitude", 99))
            elif k == "size":
                res.append(-(x.get("size") or 0))
            elif k in ("time", "altitude"):
                res.append(-(x.get("max_altitude") or 0))
            elif k == "hours_above":
                res.append(-(x.get("hours_visible") or 0))
        return tuple(res)

    objects.sort(key=sort_func)
    return objects


def apply_stream_query_overrides(
    settings: dict,
    request: Request,
    focal_length: float,
    sensor_width: float,
    sensor_height: float,
    latitude: float,
    longitude: float,
    catalogs_str: str,
    sort_key: str,
) -> dict:
    """Apply query parameter overrides onto loaded settings for a stream session."""
    settings.update(
        {
            "telescope": {"focal_length": focal_length},
            "camera": {"sensor_width": sensor_width, "sensor_height": sensor_height},
            "location": {"latitude": latitude, "longitude": longitude},
            "catalogs": catalogs_str.split(",") if catalogs_str else [],
            "sort_key": sort_key,
        }
    )

    qp = request.query_params
    img_srv = settings.setdefault("image_server", {})
    client_cfg = settings.setdefault("client_settings", {})

    if "min_altitude" in qp:
        settings["min_altitude"] = float(qp["min_altitude"])
    if "min_hours" in qp:
        val = float(qp["min_hours"])
        settings["min_hours"] = val
        client_cfg["min_hours"] = val
    if "image_resolution" in qp:
        val = int(qp["image_resolution"])
        settings["image_resolution"] = val
        img_srv["resolution"] = val
    if "max_magnitude" in qp:
        val = float(qp["max_magnitude"])
        settings["max_magnitude"] = val
        client_cfg["max_magnitude"] = val
    if "min_size" in qp:
        val = float(qp["min_size"])
        settings["min_size"] = val
        client_cfg["min_size"] = val
    if "image_padding" in qp:
        settings["image_padding"] = float(qp["image_padding"])
    if "image_timeout" in qp:
        val = int(qp["image_timeout"])
        settings["image_timeout"] = val
        img_srv["timeout"] = val
    if "download_mode" in qp:
        settings["download_mode"] = qp["download_mode"]
    if "image_source" in qp:
        try:
            val = validate_image_source(qp["image_source"])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        settings["image_source"] = val
        img_srv["source"] = val

    for flag_key in (
        "filter_active_alt",
        "filter_na_alt",
        "filter_active_hours",
        "filter_na_hours",
        "filter_active_mag",
        "filter_na_mag",
        "filter_active_size",
        "filter_na_size",
    ):
        if flag_key in qp:
            bool_val = qp[flag_key].lower() == "true"
            settings[flag_key] = bool_val
            client_cfg[flag_key] = bool_val

    if "selected_types" in qp and qp["selected_types"]:
        types_list = qp["selected_types"].split(",")
        settings["selected_types"] = types_list
        client_cfg["selected_types"] = types_list

    return settings


async def build_single_object_details(
    obj_id: str,
    settings: dict,
    catalog_manager: CatalogManager = catalogs,
) -> dict:
    """Compute full object detail payload (graphs, FOV, cache status) for a single object."""
    location = Location(**settings.get("location", {"latitude": 0, "longitude": 0}))
    telescope = Telescope(**settings.get("telescope", {"focal_length": 400}))
    camera = Camera(
        **settings.get("camera", {"sensor_width": 23.5, "sensor_height": 15.6})
    )

    all_objects = get_sorted_objects(
        settings, location, telescope, camera, catalog_manager=catalog_manager
    )
    obj = next((o for o in all_objects if o["id"] == obj_id), None)
    if not obj:
        raise HTTPException(status_code=404, detail="Object not found")

    fov_info = compute_fov_and_setup(telescope, camera, settings)

    observer = Observer(
        location=EarthLocation(
            lat=location.latitude * u.deg, lon=location.longitude * u.deg
        )
    )
    session_start, session_end = calculator.get_observing_session(observer, Time.now())
    twilight = calculator.get_twilight_periods(location, session_start)

    return await compute_object_detail_payload(
        obj=obj,
        location=location,
        session_start=session_start,
        session_end=session_end,
        twilight=twilight,
        min_altitude=settings.get("min_altitude", 30.0),
        fov_info=fov_info,
        include_aliases=True,
    )


class StreamSession:
    def __init__(self, request: Request, settings: dict):
        self.request = request
        self.settings = settings
        self.telescope = Telescope(**settings["telescope"])
        self.camera = Camera(**settings["camera"])
        self.location = Location(**settings["location"])

        self.fov_info = compute_fov_and_setup(
            self.telescope, self.camera, self.settings
        )
        self.img_res = self.fov_info["img_res"]
        self.img_source = self.fov_info["img_source"]
        self.img_padding = self.fov_info["img_padding"]
        self.img_timeout = self.fov_info["img_timeout"]
        self.fov_w_deg = self.fov_info["fov_w_deg"]
        self.fov_h_deg = self.fov_info["fov_h_deg"]
        self.download_fov = self.fov_info["download_fov"]
        self.sensor_fov_data = self.fov_info["sensor_fov"]
        self.fov_rect = self.fov_info["fov_rect"]
        self.setup_hash = self.fov_info["setup_hash"]

        self.all_objects = []
        self.top_objects = []
        self.download_list = []

        observer = Observer(
            location=EarthLocation(
                lat=self.location.latitude * u.deg, lon=self.location.longitude * u.deg
            )
        )
        self.session_start, self.session_end = calculator.get_observing_session(
            observer, Time.now()
        )
        self.twilight = calculator.get_twilight_periods(
            self.location, self.session_start
        )

    async def generate_stream(self):
        try:
            self.all_objects = await asyncio.to_thread(
                get_sorted_objects,
                self.settings,
                self.location,
                self.telescope,
                self.camera,
            )

            if not self.all_objects:
                yield "event: close\ndata: No objects found\n\n"
                return

            if await self.request.is_disconnected():
                return

            # Initial Metadata
            yield f"event: total\ndata: {len(self.all_objects)}\n\n"
            yield f"event: night_times\ndata: {json.dumps(self.twilight)}\n\n"

            if await self.request.is_disconnected():
                return
            yield self.get_initial_metadata_event()

            # Details loop
            self.prioritize_top_objects()
            async for item in self.stream_details():
                if await self.request.is_disconnected():
                    return
                yield item

            # Downloads loop
            async for item in self.stream_downloads():
                if await self.request.is_disconnected():
                    return
                yield item

            if not await self.request.is_disconnected():
                yield "event: close\ndata: Stream complete\n\n"

        except Exception as e:
            if "Disconnect" not in str(e):
                traceback.print_exc()
                try:
                    if not await self.request.is_disconnected():
                        yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"
                except Exception:
                    pass

    def get_initial_metadata_event(self):
        metadata = []
        for obj in self.all_objects:
            maj = obj.get("maj_ax", 0)
            if isinstance(maj, (int, float)):
                maj = round(maj, 2)

            size_str = f"{maj}'"
            if obj.get("min_ax", 0) > 0:
                min_ax = obj["min_ax"]
                if isinstance(min_ax, (int, float)):
                    min_ax = round(min_ax, 2)
                size_str += f" x {min_ax}'"

            mag = obj.get("magnitude", 99)
            if isinstance(mag, (int, float)):
                mag = round(mag, 2)

            metadata.append(
                {
                    "name": obj["id"],
                    "common_name": obj.get("name", "N/A"),
                    "type": obj.get("type", ""),
                    "constellation": obj.get("constellation", ""),
                    "other_id": obj.get("other_id", ""),
                    "aliases": obj.get("aliases", ""),
                    "ra": obj["ra"],
                    "dec": obj["dec"],
                    "mag": mag,
                    "size": size_str,
                    "maj_ax": maj,
                    "max_altitude": obj.get("max_altitude", 0),
                    "hours_visible": obj.get("hours_visible", 0),
                    "status": "pending",
                    "sensor_fov": self.sensor_fov_data,
                    "image_fov": self.download_fov,
                }
            )
        return f"event: catalog_metadata\ndata: {json.dumps(metadata)}\n\n"

    def prioritize_top_objects(self):
        mode = self.settings.get("download_mode", "selected")

        client_settings = self.settings.get("client_settings", {})
        max_mag = self.settings.get(
            "max_magnitude", client_settings.get("max_magnitude", 12)
        )
        min_size = self.settings.get("min_size", client_settings.get("min_size", 0))
        sel_types = self.settings.get(
            "selected_types", client_settings.get("selected_types", [])
        )
        min_hrs = self.settings.get("min_hours", client_settings.get("min_hours", 0))
        min_alt = self.settings.get("min_altitude", 30.0)

        filter_active_alt = self.settings.get(
            "filter_active_alt", client_settings.get("filter_active_alt", True)
        )
        filter_na_alt = self.settings.get(
            "filter_na_alt", client_settings.get("filter_na_alt", True)
        )
        filter_active_mag = self.settings.get(
            "filter_active_mag", client_settings.get("filter_active_mag", True)
        )
        filter_na_mag = self.settings.get(
            "filter_na_mag", client_settings.get("filter_na_mag", True)
        )
        filter_active_size = self.settings.get(
            "filter_active_size", client_settings.get("filter_active_size", True)
        )
        filter_na_size = self.settings.get(
            "filter_na_size", client_settings.get("filter_na_size", True)
        )
        filter_active_hours = self.settings.get(
            "filter_active_hours", client_settings.get("filter_active_hours", True)
        )
        filter_na_hours = self.settings.get(
            "filter_na_hours", client_settings.get("filter_na_hours", True)
        )

        sel_types = [t.lower().strip() for t in sel_types]

        def check_obj(o):
            if filter_active_mag:
                mag = o.get("magnitude", 99)
                is_na = mag >= 99
                if is_na and not filter_na_mag:
                    return False
                elif not is_na and mag > max_mag:
                    return False

            if filter_active_hours:
                hrs = o.get("hours_visible")
                is_na = hrs is None
                hrs = hrs or 0
                if is_na and not filter_na_hours:
                    return False
                elif not is_na and hrs < min_hrs:
                    return False

            if filter_active_size:
                size = o.get("maj_ax", 0)
                is_na = size == 0
                if is_na and not filter_na_size:
                    return False
                elif not is_na and size < min_size:
                    return False

            if filter_active_alt:
                alt = o.get("max_altitude")
                is_na = alt is None
                alt = alt or 0
                if is_na and not filter_na_alt:
                    return False
                elif not is_na and alt < min_alt:
                    return False

            if sel_types:
                o_type = str(o.get("type", "")).strip().lower()
                if o_type not in sel_types:
                    return False
            return True

        filtered_candidates = [o for o in self.all_objects if check_obj(o)]
        self.top_objects = filtered_candidates[:500]

        if mode == "all":
            self.download_list = self.all_objects
        elif mode == "filtered":
            self.download_list = filtered_candidates
        else:
            self.download_list = []

    async def stream_details(self):
        download_ids = set(o["id"] for o in self.download_list)
        min_alt = self.settings.get("min_altitude", 30.0)

        for obj in self.top_objects:
            if await self.request.is_disconnected():
                return

            detail = await compute_object_detail_payload(
                obj=obj,
                location=self.location,
                session_start=self.session_start,
                session_end=self.session_end,
                twilight=self.twilight,
                min_altitude=min_alt,
                fov_info=self.fov_info,
                download_ids=download_ids,
                include_aliases=False,
            )
            yield f"event: object_details\ndata: {json.dumps(detail)}\n\n"

    async def stream_downloads(self):
        to_download = []
        for obj in self.download_list:
            _, filepath, _ = get_cache_info(obj["id"], self.setup_hash)
            if not os.path.exists(filepath):
                to_download.append(obj)

        total = len(to_download)
        if total == 0:
            return

        yield f"event: download_progress\ndata: {json.dumps({'current': 0, 'total': total})}\n\n"

        queue = asyncio.Queue()
        semaphore = asyncio.Semaphore(5)
        completed = 0

        async def worker(obj):
            nonlocal completed
            async with semaphore:
                if await self.request.is_disconnected():
                    return
                obj_id = obj["id"]
                await queue.put({"name": obj_id, "status": "downloading"})

                try:
                    url = await download_image(
                        obj["ra"],
                        obj["dec"],
                        self.download_fov,
                        obj_id,
                        self.setup_hash,
                        self.img_res,
                        self.img_source,
                        self.img_timeout,
                    )
                    await queue.put(
                        {
                            "name": obj_id,
                            "status": "cached",
                            "url": url,
                            "image_fov": self.download_fov,
                        }
                    )
                except Exception:
                    await queue.put({"name": obj_id, "status": "error"})
                finally:
                    completed += 1
                    await queue.put(
                        {"progress": True, "current": completed, "total": total}
                    )

        tasks = [asyncio.create_task(worker(o)) for o in to_download]

        async def monitor():
            if tasks:
                await asyncio.gather(*tasks)
            await queue.put(None)

        asyncio.create_task(monitor())

        while True:
            msg = await queue.get()
            if msg is None:
                break
            if "progress" in msg:
                yield f"event: download_progress\ndata: {json.dumps(msg)}\n\n"
            else:
                yield f"event: image_status\ndata: {json.dumps(msg)}\n\n"
