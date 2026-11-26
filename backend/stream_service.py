import asyncio
import json
import math
import os
import re
import time
import traceback
from typing import Dict, List, Optional, Tuple

import astropy.units as u
import numpy as np
import pandas as pd
from astroplan import Observer
from astropy.coordinates import AltAz, EarthLocation, SkyCoord, get_body, get_constellation, get_sun
from astropy.time import Time
from fastapi import HTTPException, Request

from .astro_utils import AstroCalculator
from .data_manager import CatalogManager
from .image_service import (
    download_image,
    get_cache_info,
    get_inflight_telemetry,
    get_setup_hash,
    validate_image_source,
)
from .models import Camera, FOVRectangle, Location, Telescope

calculator = AstroCalculator()
catalogs = CatalogManager()


def compute_fov_and_setup(
    telescope: Telescope,
    camera: Camera,
    settings: dict,
    override_fov: Optional[float] = None,
    override_resolution: Optional[int] = None,
) -> dict:
    """Compute FOV geometry, download FOV, FOV rectangle, and image setup hash."""
    img_srv = settings.get("image_server") if isinstance(settings.get("image_server"), dict) else {}
    img_res = int(img_srv.get("resolution", settings.get("image_resolution", 1024)))
    if override_resolution is not None:
        try:
            parsed_res = int(override_resolution)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid resolution override: {override_resolution}") from exc
        if parsed_res < 64 or parsed_res > 4096:
            raise ValueError(f"Resolution must be between 64 and 4096 (got {parsed_res})")
        img_res = parsed_res
    img_source = validate_image_source(
        img_srv.get("source", settings.get("image_source", "dss2r"))
    )
    img_padding = float(settings.get("image_padding", 1.15))
    img_timeout = int(img_srv.get("timeout", settings.get("image_timeout", 120)))

    fov_w_min, fov_h_min = calculator.calculate_fov(telescope, camera)
    fov_w_deg = fov_w_min / 60.0
    fov_h_deg = fov_h_min / 60.0
    default_download_fov = max(max(fov_w_deg, fov_h_deg) * img_padding, 0.25)

    raw_override = override_fov if override_fov is not None else settings.get("fov")
    parsed_override = None
    if raw_override is not None:
        try:
            candidate = float(raw_override)
            if candidate > 0:
                parsed_override = max(candidate, 0.25)
        except (TypeError, ValueError):
            parsed_override = None

    download_fov = (
        parsed_override if parsed_override is not None else default_download_fov
    )

    sensor_fov_data = {"w": fov_w_deg, "h": fov_h_deg}
    fov_rect = FOVRectangle(
        width_percent=(fov_w_deg / download_fov) * 100.0,
        height_percent=(fov_h_deg / download_fov) * 100.0,
    )
    setup_hash = get_setup_hash(
        fov_w_deg,
        fov_h_deg,
        img_padding,
        img_res,
        img_source,
        override_fov=parsed_override,
    )

    return {
        "fov_w_deg": fov_w_deg,
        "fov_h_deg": fov_h_deg,
        "download_fov": download_fov,
        "default_download_fov": default_download_fov,
        "sensor_fov": sensor_fov_data,
        "fov_rect": fov_rect,
        "setup_hash": setup_hash,
        "img_res": img_res,
        "img_source": img_source,
        "img_padding": img_padding,
        "img_timeout": img_timeout,
    }


def _extract_min_hours_altitude(settings: dict) -> float:
    """Extract the lower altitude limit (°) for what counts as visible hours."""
    client_cfg = settings.get("client_settings", {}) or {}
    raw_val = settings.get(
        "min_hours_altitude",
        client_cfg.get("min_hours_altitude", settings.get("min_altitude", 30.0)),
    )
    try:
        return float(raw_val)
    except (TypeError, ValueError):
        return float(settings.get("min_altitude", 30.0))


def _extract_time_window_settings(settings: dict) -> dict:
    """Extract time window filter configuration from settings and client_settings."""
    client_cfg = settings.get("client_settings", {}) or {}
    active = bool(
        settings.get(
            "filter_active_time_window",
            client_cfg.get("filter_active_time_window", False),
        )
    )
    start_str = settings.get(
        "time_window_start", client_cfg.get("time_window_start", "")
    )
    end_str = settings.get(
        "time_window_end", client_cfg.get("time_window_end", "")
    )
    tz_offset = settings.get("tz_offset", client_cfg.get("tz_offset", None))
    parsed_tz = None
    if tz_offset is not None and tz_offset != "":
        try:
            parsed_tz = float(tz_offset)
        except (TypeError, ValueError):
            parsed_tz = None
    return {
        "active": active,
        "start": str(start_str).strip() if start_str is not None else "",
        "end": str(end_str).strip() if end_str is not None else "",
        "tz_offset": parsed_tz,
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
    time_window_active: bool = False,
    time_window_start: Optional[str] = None,
    time_window_end: Optional[str] = None,
    tz_offset_minutes: Optional[float] = None,
) -> dict:
    """Shared async builder for an object's altitude graphs, FOV info, and cache status."""
    obj_id = obj["id"]
    is_solar = bool(obj.get("is_solar_system"))
    setup_hash = fov_info["setup_hash"]
    if is_solar:
        url = ""
        is_cached = False
    else:
        url, filepath, _ = get_cache_info(obj_id, setup_hash)
        is_cached = os.path.exists(filepath)

    if is_solar and obj.get("solar_id") in ("sun", "moon"):
        def _calc_moving_solar_graph():
            ephem = calculator.prepare_session_ephemeris(
                location=location,
                start_time=session_start,
                end_time=session_end,
                num_points=60,
            )
            if obj.get("solar_id") == "sun":
                return {
                    "target": ephem["sun_points"],
                    "moon": ephem["moon_points"],
                    "sun": ephem["sun_points"],
                }
            return {
                "target": ephem["moon_points"],
                "moon": ephem["moon_points"],
                "sun": ephem["sun_points"],
            }
        alt_data = await asyncio.to_thread(_calc_moving_solar_graph)
    else:
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
        alt_data["target"],
        min_altitude,
        None if (is_solar and obj.get("solar_id") == "sun") else twilight,
        time_window_active=False if (is_solar and obj.get("solar_id") == "sun") else time_window_active,
        time_window_start=time_window_start,
        time_window_end=time_window_end,
        tz_offset_minutes=tz_offset_minutes,
    )

    if is_solar:
        status = "live"
    elif is_cached:
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
        detail["sub_objects"] = obj.get("sub_objects", [])
    return detail


def get_sorted_objects(
    settings: dict,
    location: Location,
    telescope: Telescope,
    camera: Camera,
    catalog_manager: CatalogManager = catalogs,
) -> List[dict]:
    """Fetch, enrich with visibility/altitude metrics, and sort catalog objects."""
    raw_objects = catalog_manager.get_all_objects(
        settings.get("catalogs") or CatalogManager.get_available_catalogs()
    )
    if raw_objects.empty:
        return []

    observer = Observer(
        location=EarthLocation(
            lat=location.latitude * u.deg, lon=location.longitude * u.deg
        )
    )
    try:
        session_start, session_end = calculator.get_observing_session_for_date(
            observer, settings.get("obs_date")
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
        min_hours_alt = _extract_min_hours_altitude(settings)
        tw_cfg = _extract_time_window_settings(settings)
        time_mask = None
        if tw_cfg["active"]:
            session_mid = session_start + 0.5 * (session_end - session_start)
            twilight = (
                calculator.get_twilight_periods(location, session_mid)
                if not tw_cfg["start"] or not tw_cfg["end"]
                else None
            )
            time_mask = calculator.build_time_window_mask(
                times=altaz_frame.obstime,
                start_str=tw_cfg["start"],
                end_str=tw_cfg["end"],
                tz_offset_minutes=tw_cfg["tz_offset"],
                twilight_periods=twilight,
            )
        df["hours_visible"] = calculator.batch_calculate_nightly_hours(
            all_ras,
            all_decs,
            altaz_frame,
            night_duration,
            min_hours_alt,
            time_mask=time_mask,
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
    catalogs_str: str = "",
    sort_key: str = "time",
) -> dict:
    """Apply query parameter overrides onto loaded settings for a stream session."""
    effective_catalogs = (
        [c.strip() for c in catalogs_str.split(",") if c.strip()]
        if catalogs_str and catalogs_str.strip()
        else CatalogManager.get_available_catalogs()
    )
    settings.update(
        {
            "telescope": {"focal_length": focal_length},
            "camera": {"sensor_width": sensor_width, "sensor_height": sensor_height},
            "location": {"latitude": latitude, "longitude": longitude},
            "catalogs": effective_catalogs,
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
    if "min_hours_altitude" in qp:
        val = float(qp["min_hours_altitude"])
        settings["min_hours_altitude"] = val
        client_cfg["min_hours_altitude"] = val
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
    if "fov" in qp and qp["fov"]:
        try:
            settings["fov"] = float(qp["fov"])
        except ValueError:
            pass
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
        "filter_active_time_window",
        "filter_favorites_only",
    ):
        if flag_key in qp:
            bool_val = qp[flag_key].lower() == "true"
            settings[flag_key] = bool_val
            client_cfg[flag_key] = bool_val

    if "favorites" in qp:
        raw_favs = qp["favorites"].strip()
        fav_list = [f.strip() for f in raw_favs.split(",") if f.strip()] if raw_favs else []
        settings["favorites"] = fav_list
        client_cfg["favorites"] = fav_list

    if "time_window_start" in qp:
        val = qp["time_window_start"].strip()
        settings["time_window_start"] = val
        client_cfg["time_window_start"] = val
    if "time_window_end" in qp:
        val = qp["time_window_end"].strip()
        settings["time_window_end"] = val
        client_cfg["time_window_end"] = val
    if "tz_offset" in qp and qp["tz_offset"].strip():
        try:
            val = float(qp["tz_offset"])
            settings["tz_offset"] = val
            client_cfg["tz_offset"] = val
        except ValueError:
            pass

    if "selected_types" in qp:
        raw_types = qp["selected_types"].strip()
        types_list = [t for t in raw_types.split(",") if t.strip()] if raw_types else []
        settings["selected_types"] = types_list
        client_cfg["selected_types"] = types_list

    if "obs_date" in qp and qp["obs_date"].strip():
        settings["obs_date"] = qp["obs_date"].strip()

    return settings


_SOLAR_EQ_ARCSEC_1AU = {
    "sun": 1919.26,
    "mercury": 6.74,
    "venus": 16.92,
    "mars": 9.36,
    "jupiter": 196.74,
    "saturn": 165.60,
    "uranus": 70.56,
    "neptune": 68.28,
    "pluto": 3.28,
}


def _resolve_solar_system_object_sync(
    obj_id: str,
    settings: dict,
    location: Location,
) -> Optional[Tuple[dict, Time, Time, Dict[str, List[str]]]]:
    """Resolve Sun, Moon, or Planet single-object details with Astropy topocentric coordinates."""
    key = str(obj_id or "").strip().lower()
    if key not in (
        "sun",
        "moon",
        "mercury",
        "venus",
        "mars",
        "jupiter",
        "saturn",
        "uranus",
        "neptune",
        "pluto",
    ):
        return None

    earth_loc = EarthLocation(
        lat=location.latitude * u.deg, lon=location.longitude * u.deg
    )
    observer = Observer(location=earth_loc)
    night_start, night_end = calculator.get_observing_session_for_date(
        observer, settings.get("obs_date")
    )
    night_mid = night_start + 0.5 * (night_end - night_start)
    twilight = calculator.get_twilight_periods(location, night_mid)

    if key == "sun":
        # Center daytime solar window around local solar noon of the active date
        obs_date_str = settings.get("obs_date")
        if obs_date_str and isinstance(obs_date_str, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", obs_date_str.strip()):
            t_noon = (
                Time(f"{obs_date_str.strip()}T12:00:00", format="isot", scale="utc")
                - (float(location.longitude) / 15.0) * u.hour
            )
        else:
            t_noon = night_start - 6.0 * u.hour
        rises, sets = calculator._find_solar_crossings(
            earth_loc, t_noon, horizon_deg=0.0, window_before_h=14.0, window_after_h=14.0
        )
        past_rises = [t for t in rises if t <= t_noon + 2.0 * u.hour]
        future_sets = [t for t in sets if t >= t_noon - 2.0 * u.hour]
        if past_rises and future_sets and future_sets[0] > past_rises[-1]:
            session_start = past_rises[-1] - 0.5 * u.hour
            session_end = future_sets[0] + 0.5 * u.hour
        else:
            session_start = t_noon - 12.0 * u.hour
            session_end = t_noon + 12.0 * u.hour
        eval_time = session_start + 0.5 * (session_end - session_start)
    else:
        session_start = night_start
        session_end = night_end
        eval_time = night_mid

    positions = calculator.get_solar_system_positions(
        lat=location.latitude,
        lon=location.longitude,
        elevation=0.0,
        time_str=eval_time.isot,
    )
    matched = next((p for p in positions if p.get("id") == key), None)
    if not matched:
        return None

    ra_val = float(matched["ra"])
    dec_val = float(matched["dec"])
    dist_au = float(matched.get("distance_au") or 1.0)
    mag_val = float(matched.get("mag") if matched.get("mag") is not None else 99.0)

    if key == "moon":
        topo_km = max(340000.0, dist_au * 149597870.7)
        diam_deg = math.degrees(3474.8 / topo_km)
    else:
        arcsec_1au = _SOLAR_EQ_ARCSEC_1AU.get(key, 0.0)
        diam_deg = arcsec_1au / (max(0.01, dist_au) * 3600.0)

    maj_arcmin = round(diam_deg * 60.0, 4)
    if maj_arcmin >= 1.0:
        size_str = f"{maj_arcmin:.1f}'"
    elif diam_deg > 0:
        arcsec = diam_deg * 3600.0
        size_str = f'{arcsec:.1f}"' if arcsec >= 1.0 else f'{arcsec:.2f}"'
    else:
        size_str = "—"

    try:
        const_code = get_constellation(
            SkyCoord(ra=ra_val * u.deg, dec=dec_val * u.deg), short_name=True
        ).upper()
    except Exception:
        const_code = ""

    if key == "sun":
        obj_type = "Sun"
        common = f"Daytime Star · {dist_au:.3f} AU"
    elif key == "moon":
        obj_type = "Moon"
        p_name = matched.get("phase_name", "Moon")
        p_pct = matched.get("phase_percent", 50)
        common = f"{p_name} ({round(p_pct)}%)"
    elif key == "pluto":
        obj_type = "Dwarf Planet"
        common = f"Dwarf Planet · {dist_au:.1f} AU"
    else:
        obj_type = "Planet"
        common = f"Solar System Planet · {dist_au:.2f} AU"

    max_alt_arr = calculator.batch_get_max_altitude(
        np.array([dec_val], dtype=float), location.latitude
    )
    peak_alt = float(max_alt_arr[0]) if len(max_alt_arr) > 0 else 0.0

    obj = {
        "id": matched["name"],
        "name": common,
        "type": obj_type,
        "constellation": const_code,
        "other_id": key,
        "aliases": "",
        "sub_objects": [],
        "ra": ra_val,
        "dec": dec_val,
        "mag": mag_val,
        "magnitude": mag_val,
        "maj_ax": maj_arcmin,
        "size_formatted": size_str,
        "max_altitude": peak_alt,
        "is_solar_system": True,
        "solar_id": key,
        "symbol": matched.get("symbol", ""),
        "distance_au": dist_au,
    }
    return obj, session_start, session_end, twilight


def _resolve_single_object_sync(
    obj_id: str,
    settings: dict,
    location: Location,
    catalog_manager: CatalogManager = catalogs,
) -> Optional[Tuple[dict, Time, Time, Dict[str, List[str]]]]:
    """Fast single-object catalog lookup + session/twilight preparation without sorting all 14k rows."""
    solar_match = _resolve_solar_system_object_sync(obj_id, settings, location)
    if solar_match is not None:
        return solar_match

    raw_objects = catalog_manager.get_all_objects(
        settings.get("catalogs") or CatalogManager.get_available_catalogs()
    )
    if raw_objects.empty:
        return None

    norm_req = CatalogManager.normalize_designation(obj_id)
    compact_req = norm_req.replace(" ", "")
    matched_row = None

    # 1. Exact ID match first (O(N) C-level Series comparison, <0.5ms)
    exact_mask = raw_objects["id"] == obj_id
    if exact_mask.any():
        matched_row = raw_objects[exact_mask].iloc[0]
    elif norm_req:
        # 2. Normalized designation or space-insensitive alias match
        for _, row in raw_objects.iterrows():
            rid = str(row.get("id", ""))
            norm_rid = CatalogManager.normalize_designation(rid)
            if norm_rid == norm_req or norm_rid.replace(" ", "") == compact_req:
                matched_row = row
                break
            aliases_raw = str(row.get("aliases", "") or "")
            if aliases_raw:
                for a in aliases_raw.split(","):
                    if not a.strip():
                        continue
                    norm_a = CatalogManager.normalize_designation(a)
                    if norm_a == norm_req or norm_a.replace(" ", "") == compact_req:
                        matched_row = row
                        break
                if matched_row is not None:
                    break

    if matched_row is None:
        return None

    clean_series = matched_row.replace([np.nan, np.inf, -np.inf], None)
    obj = clean_series.to_dict()

    ra_val = float(obj.get("ra") or 0.0)
    dec_val = float(obj.get("dec") or 0.0)
    obj["ra"] = ra_val
    obj["dec"] = dec_val

    max_alt_arr = calculator.batch_get_max_altitude(
        np.array([dec_val], dtype=float), location.latitude
    )
    obj["max_altitude"] = float(max_alt_arr[0]) if len(max_alt_arr) > 0 else 0.0

    raw_mag = obj.get("mag")
    try:
        mag_val = round(float(raw_mag), 2) if raw_mag is not None else 99
    except (TypeError, ValueError):
        mag_val = 99
    obj["magnitude"] = mag_val

    raw_maj = obj.get("maj_ax") or 0
    try:
        maj_val = round(float(raw_maj), 2)
    except (TypeError, ValueError):
        maj_val = 0
    size_str = f"{maj_val}'"
    raw_min = obj.get("min_ax") or 0
    try:
        min_val = round(float(raw_min), 2)
    except (TypeError, ValueError):
        min_val = 0
    if min_val > 0:
        size_str += f" x {min_val}'"
    obj["maj_ax"] = maj_val
    obj["size_formatted"] = size_str

    observer = Observer(
        location=EarthLocation(
            lat=location.latitude * u.deg, lon=location.longitude * u.deg
        )
    )
    session_start, session_end = calculator.get_observing_session_for_date(
        observer, settings.get("obs_date")
    )
    session_mid = session_start + 0.5 * (session_end - session_start)
    twilight = calculator.get_twilight_periods(location, session_mid)
    return obj, session_start, session_end, twilight


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

    resolved = await asyncio.to_thread(
        _resolve_single_object_sync, obj_id, settings, location, catalog_manager
    )
    if not resolved:
        raise HTTPException(status_code=404, detail="Object not found")

    obj, session_start, session_end, twilight = resolved
    fov_info = compute_fov_and_setup(telescope, camera, settings)
    tw_cfg = _extract_time_window_settings(settings)
    min_hours_alt = _extract_min_hours_altitude(settings)

    detail = await compute_object_detail_payload(
        obj=obj,
        location=location,
        session_start=session_start,
        session_end=session_end,
        twilight=twilight,
        min_altitude=min_hours_alt,
        fov_info=fov_info,
        include_aliases=True,
        time_window_active=tw_cfg["active"],
        time_window_start=tw_cfg["start"],
        time_window_end=tw_cfg["end"],
        tz_offset_minutes=tw_cfg["tz_offset"],
    )
    detail.setdefault("common_name", obj.get("name") or "N/A")
    detail.setdefault("type", obj.get("type") or "")
    detail.setdefault("constellation", obj.get("constellation") or "")
    detail.setdefault("other_id", obj.get("other_id") or "")
    detail.setdefault("ra", obj["ra"])
    detail.setdefault("dec", obj["dec"])
    detail.setdefault("mag", obj.get("magnitude", 99))
    detail.setdefault("size", obj.get("size_formatted", "0'"))
    detail.setdefault("maj_ax", obj.get("maj_ax", 0))
    detail.setdefault("max_altitude", round(float(obj.get("max_altitude") or 0.0), 1))
    detail["hours_visible"] = detail.get("hours_above_min", 0.0)
    detail["night_times"] = twilight
    if obj.get("is_solar_system"):
        detail["is_solar_system"] = True
        detail["solar_id"] = obj.get("solar_id", "")
        detail["symbol"] = obj.get("symbol", "")
        detail["distance_au"] = obj.get("distance_au")
    return detail


def _filter_objects_by_settings(all_objects: List[dict], settings: dict):
    client_settings = settings.get("client_settings", {})
    max_mag = settings.get(
        "max_magnitude", client_settings.get("max_magnitude", 12)
    )
    min_size = settings.get("min_size", client_settings.get("min_size", 0))
    sel_types = settings.get(
        "selected_types", client_settings.get("selected_types", [])
    )
    min_hrs = settings.get("min_hours", client_settings.get("min_hours", 0))
    min_alt = settings.get("min_altitude", 30.0)

    filter_active_alt = settings.get(
        "filter_active_alt", client_settings.get("filter_active_alt", True)
    )
    filter_na_alt = settings.get(
        "filter_na_alt", client_settings.get("filter_na_alt", True)
    )
    filter_active_mag = settings.get(
        "filter_active_mag", client_settings.get("filter_active_mag", True)
    )
    filter_na_mag = settings.get(
        "filter_na_mag", client_settings.get("filter_na_mag", True)
    )
    filter_active_size = settings.get(
        "filter_active_size", client_settings.get("filter_active_size", True)
    )
    filter_na_size = settings.get(
        "filter_na_size", client_settings.get("filter_na_size", True)
    )
    filter_active_hours = settings.get(
        "filter_active_hours", client_settings.get("filter_active_hours", True)
    )
    filter_na_hours = settings.get(
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
            if "__none__" in sel_types:
                return False
            o_type = str(o.get("type", "")).strip().lower()
            if o_type not in sel_types:
                return False
        return True

    filtered_candidates = [o for o in all_objects if check_obj(o)]
    filter_favs_only = bool(
        settings.get(
            "filter_favorites_only",
            client_settings.get("filter_favorites_only", False),
        )
    )
    fav_names = set(
        settings.get("favorites", client_settings.get("favorites", [])) or []
    )
    return filtered_candidates, fav_names, filter_favs_only


def _normalize_fav_token(val: object) -> str:
    return re.sub(r"\s+", "", str(val or "").strip()).lower()


def _build_fav_lookup(fav_names: set) -> Tuple[set, set]:
    exact = {str(f).strip() for f in fav_names if str(f).strip()}
    compact = {_normalize_fav_token(f) for f in exact if _normalize_fav_token(f)}
    return exact, compact


def _is_favorite_object(obj: dict, fav_exact: set, fav_compact: set) -> bool:
    if not fav_exact:
        return False
    obj_id = str(obj.get("id") or "").strip()
    obj_name = str(obj.get("name") or "").strip()
    if obj_id in fav_exact or obj_name in fav_exact:
        return True
    if obj_id and _normalize_fav_token(obj_id) in fav_compact:
        return True
    if obj_name and obj_name != "N/A" and _normalize_fav_token(obj_name) in fav_compact:
        return True
    return False


def _select_download_list(
    mode: str,
    all_objects: List[dict],
    filtered_candidates: List[dict],
    fav_names: set,
    filter_favs_only: bool,
) -> List[dict]:
    if mode == "all":
        return list(all_objects)
    elif mode == "filtered":
        if filter_favs_only:
            fav_exact, fav_compact = _build_fav_lookup(fav_names)
            return [
                o
                for o in filtered_candidates
                if _is_favorite_object(o, fav_exact, fav_compact)
            ]
        return list(filtered_candidates)
    return []


class BackgroundDownloadManager:
    """Manages batch image downloads in background asyncio tasks decoupled from HTTP/SSE connections."""

    def __init__(self):
        self.active: bool = False
        self.mode: str = "selected"
        self.current: int = 0
        self.total: int = 0
        self.succeeded: int = 0
        self.failed: int = 0
        self.already_cached: int = 0
        self.total_candidates: int = 0
        self.setup_hash: str = ""
        self.image_fov: float = 1.0
        self.image_source: str = "dss2r"
        self.image_resolution: int = 1024
        self.started_monotonic: Optional[float] = None
        self.active_workers: Dict[str, float] = {}
        self.last_error: str = ""
        self.last_error_target: str = ""
        self.last_summary: str = ""
        self.queued_ids: set = set()
        self.object_updates: Dict[str, dict] = {}
        self._subscribers: List[asyncio.Queue] = []
        self._runner_task: Optional[asyncio.Task] = None
        self._worker_tasks: List[asyncio.Task] = []
        self._stop_event: asyncio.Event = asyncio.Event()
        self._lock: asyncio.Lock = asyncio.Lock()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        if q in self._subscribers:
            self._subscribers.remove(q)

    def _broadcast(self, msg: Optional[dict]):
        for q in list(self._subscribers):
            try:
                q.put_nowait(msg)
            except Exception:
                pass

    def _snapshot_active_targets(self) -> List[dict]:
        now_mono = time.monotonic()
        active_list = []
        for obj_id, started_mono in list(self.active_workers.items()):
            elapsed = round(max(0.0, now_mono - started_mono), 1)
            telem = get_inflight_telemetry(obj_id, self.setup_hash)
            active_list.append(
                {
                    "name": obj_id,
                    "elapsed_s": (
                        telem["elapsed_s"]
                        if telem and "elapsed_s" in telem
                        else elapsed
                    ),
                    "stage": telem.get("stage", "fetching") if telem else "fetching",
                    "survey": (
                        telem.get("survey", self.image_source)
                        if telem
                        else self.image_source
                    ),
                    "attempt": telem.get("attempt", 1) if telem else 1,
                    "max_attempts": telem.get("max_attempts", 4) if telem else 4,
                    "detail": telem.get("detail", "") if telem else "",
                }
            )
        return active_list

    def get_status(self) -> dict:
        elapsed_s = (
            round(max(0.0, time.monotonic() - self.started_monotonic), 1)
            if self.active and self.started_monotonic is not None
            else 0.0
        )
        return {
            "active": self.active,
            "mode": self.mode,
            "current": self.current,
            "total": self.total,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "already_cached": self.already_cached,
            "total_candidates": self.total_candidates,
            "setup_hash": self.setup_hash,
            "image_fov": self.image_fov,
            "image_source": self.image_source,
            "image_resolution": self.image_resolution,
            "elapsed_s": elapsed_s,
            "active_targets": self._snapshot_active_targets() if self.active else [],
            "last_error": self.last_error,
            "last_error_target": self.last_error_target,
            "last_summary": self.last_summary,
            "object_updates": list(self.object_updates.values()),
        }

    async def stop(self) -> dict:
        async with self._lock:
            await self._stop_unlocked()
            return self.get_status()

    async def _stop_unlocked(self):
        was_active = self.active
        self._stop_event.set()
        for t in list(self._worker_tasks):
            if not t.done():
                t.cancel()
        if self._runner_task and not self._runner_task.done():
            self._runner_task.cancel()
            try:
                await self._runner_task
            except (asyncio.CancelledError, Exception):
                pass
        self._worker_tasks = []
        self._runner_task = None
        self.active_workers.clear()
        for obj_id, info in list(self.object_updates.items()):
            if info.get("status") in ("queued", "downloading"):
                updated = {"name": obj_id, "status": "pending"}
                self.object_updates[obj_id] = updated
                self._broadcast(updated)
        self.queued_ids.clear()
        if was_active:
            self.active = False
            self.last_summary = (
                f"Download stopped ({self.succeeded} of {self.total} saved)."
            )
            self._broadcast(None)

    async def start_from_settings(self, settings: dict) -> dict:
        mode = settings.get("download_mode", "filtered")
        if mode not in ("filtered", "all"):
            mode = "filtered"
        telescope = Telescope(**settings["telescope"])
        camera = Camera(**settings["camera"])
        location = Location(**settings["location"])
        fov_info = compute_fov_and_setup(telescope, camera, settings)
        all_objects = await asyncio.to_thread(
            get_sorted_objects, settings, location, telescope, camera
        )
        filtered_candidates, fav_names, filter_favs_only = _filter_objects_by_settings(
            all_objects, settings
        )
        download_list = _select_download_list(
            mode, all_objects, filtered_candidates, fav_names, filter_favs_only
        )
        return await self.start_job(
            download_list=download_list,
            fov_info=fov_info,
            mode=mode,
        )

    async def start_job(
        self,
        download_list: List[dict],
        fov_info: dict,
        mode: str = "filtered",
    ) -> dict:
        async with self._lock:
            await self._stop_unlocked()

            setup_hash = fov_info["setup_hash"]
            download_fov = fov_info["download_fov"]
            img_res = fov_info["img_res"]
            img_source = fov_info["img_source"]
            img_timeout = fov_info["img_timeout"]

            to_download = []
            for obj in download_list:
                _, filepath, _ = get_cache_info(obj["id"], setup_hash)
                if not os.path.exists(filepath):
                    to_download.append(obj)

            total = len(to_download)
            total_candidates = len(download_list)
            already_cached = max(0, total_candidates - total)

            self.mode = mode
            self.current = 0
            self.total = total
            self.succeeded = 0
            self.failed = 0
            self.already_cached = already_cached
            self.total_candidates = total_candidates
            self.setup_hash = setup_hash
            self.image_fov = download_fov
            self.image_source = img_source
            self.image_resolution = img_res
            self.active_workers = {}
            self.last_error = ""
            self.last_error_target = ""
            self.last_summary = ""
            self.object_updates = {}
            self.queued_ids = set()

            if total == 0:
                self.active = False
                self.started_monotonic = None
                if total_candidates == 0:
                    self.last_summary = (
                        "No targets matched the current filters for download."
                    )
                else:
                    self.last_summary = (
                        f"All {already_cached} matching targets are already cached "
                        f"for this setup ({download_fov:.2f}° FOV)."
                    )
                return self.get_status()

            self._stop_event = asyncio.Event()
            self.active = True
            self.started_monotonic = time.monotonic()
            for obj in to_download:
                obj_id = obj["id"]
                self.queued_ids.add(obj_id)
                self.object_updates[obj_id] = {"name": obj_id, "status": "queued"}

            self._runner_task = asyncio.create_task(
                self._run_downloads(
                    to_download=to_download,
                    download_fov=download_fov,
                    setup_hash=setup_hash,
                    img_res=img_res,
                    img_source=img_source,
                    img_timeout=img_timeout,
                    total=total,
                    already_cached=already_cached,
                    total_candidates=total_candidates,
                    stop_event=self._stop_event,
                )
            )
            return self.get_status()

    async def _run_downloads(
        self,
        to_download: List[dict],
        download_fov: float,
        setup_hash: str,
        img_res: int,
        img_source: str,
        img_timeout: int,
        total: int,
        already_cached: int,
        total_candidates: int,
        stop_event: asyncio.Event,
    ):
        semaphore = asyncio.Semaphore(2)
        completed = 0

        async def worker(obj: dict):
            nonlocal completed
            obj_id = obj["id"]
            if stop_event.is_set():
                return
            async with semaphore:
                if stop_event.is_set():
                    return
                self.active_workers[obj_id] = time.monotonic()
                downloading_msg = {"name": obj_id, "status": "downloading"}
                self.object_updates[obj_id] = downloading_msg
                self._broadcast(downloading_msg)
                self._broadcast(
                    {
                        "progress": True,
                        "current": self.current,
                        "total": total,
                        "succeeded": self.succeeded,
                        "failed": self.failed,
                        "already_cached": already_cached,
                        "total_candidates": total_candidates,
                        "active_targets": self._snapshot_active_targets(),
                        "last_error": self.last_error,
                        "last_error_target": self.last_error_target,
                        "image_fov": download_fov,
                        "image_source": img_source,
                    }
                )

                try:
                    url = await download_image(
                        obj["ra"],
                        obj["dec"],
                        download_fov,
                        obj_id,
                        setup_hash,
                        img_res,
                        img_source,
                        img_timeout,
                    )
                    self.succeeded += 1
                    cached_msg = {
                        "name": obj_id,
                        "status": "cached",
                        "url": url,
                        "image_fov": download_fov,
                    }
                    self.object_updates[obj_id] = cached_msg
                    self.queued_ids.discard(obj_id)
                    self._broadcast(cached_msg)
                except asyncio.CancelledError:
                    _, filepath, _ = get_cache_info(obj_id, setup_hash)
                    if not os.path.exists(filepath):
                        pending_msg = {"name": obj_id, "status": "pending"}
                        self.object_updates[obj_id] = pending_msg
                        self._broadcast(pending_msg)
                    self.queued_ids.discard(obj_id)
                    raise
                except Exception as exc:
                    err_detail = str(exc).strip() or exc.__class__.__name__
                    self.failed += 1
                    self.last_error = err_detail
                    self.last_error_target = obj_id
                    err_msg = {
                        "name": obj_id,
                        "status": "error",
                        "error": err_detail,
                    }
                    self.object_updates[obj_id] = err_msg
                    self.queued_ids.discard(obj_id)
                    self._broadcast(err_msg)
                finally:
                    self.active_workers.pop(obj_id, None)
                    if not stop_event.is_set():
                        completed += 1
                        self.current = completed
                        self._broadcast(
                            {
                                "progress": True,
                                "current": completed,
                                "total": total,
                                "succeeded": self.succeeded,
                                "failed": self.failed,
                                "already_cached": already_cached,
                                "total_candidates": total_candidates,
                                "active_targets": self._snapshot_active_targets(),
                                "last_error": self.last_error,
                                "last_error_target": self.last_error_target,
                                "image_fov": download_fov,
                                "image_source": img_source,
                            }
                        )

        self._worker_tasks = [asyncio.create_task(worker(o)) for o in to_download]
        try:
            if self._worker_tasks:
                await asyncio.gather(*self._worker_tasks, return_exceptions=True)
        finally:
            self.active_workers.clear()
            if not stop_event.is_set():
                self.active = False
                self.queued_ids.clear()
                if self.failed == 0:
                    self.last_summary = (
                        f"Downloaded {self.succeeded} of {total} images "
                        f"({download_fov:.2f}° FOV)."
                    )
                elif self.succeeded > 0:
                    self.last_summary = (
                        f"Finished: {self.succeeded} saved, {self.failed} failed "
                        f"({self.last_error_target}: {self.last_error})"
                    )
                else:
                    self.last_summary = (
                        f"Failed ({self.failed}/{total}): "
                        f"{self.last_error_target}: {self.last_error}"
                    )
                self._broadcast(None)


background_download_manager = BackgroundDownloadManager()


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
        self.session_start = None
        self.session_end = None
        self.twilight = {}

    def _init_session_ephemeris_sync(self):
        observer = Observer(
            location=EarthLocation(
                lat=self.location.latitude * u.deg, lon=self.location.longitude * u.deg
            )
        )
        session_start, session_end = calculator.get_observing_session_for_date(
            observer, self.settings.get("obs_date")
        )
        session_mid = session_start + 0.5 * (session_end - session_start)
        twilight = calculator.get_twilight_periods(self.location, session_mid)
        return session_start, session_end, twilight

    async def generate_stream(self):
        try:
            (session_start, session_end, twilight), self.all_objects = await asyncio.gather(
                asyncio.to_thread(self._init_session_ephemeris_sync),
                asyncio.to_thread(
                    get_sorted_objects,
                    self.settings,
                    self.location,
                    self.telescope,
                    self.camera,
                ),
            )
            self.session_start = session_start
            self.session_end = session_end
            self.twilight = twilight

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
        cached_files = set()
        if self.all_objects:
            _, _, setup_dir = get_cache_info(self.all_objects[0]["id"], self.setup_hash)
            try:
                if os.path.isdir(setup_dir):
                    cached_files = set(os.listdir(setup_dir))
            except OSError:
                cached_files = set()

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

            obj_id = obj["id"]
            url, filepath, _ = get_cache_info(obj_id, self.setup_hash)
            is_cached = os.path.basename(filepath) in cached_files

            metadata.append(
                {
                    "name": obj_id,
                    "common_name": obj.get("name", "N/A"),
                    "type": obj.get("type", ""),
                    "constellation": obj.get("constellation", ""),
                    "other_id": obj.get("other_id", ""),
                    "aliases": obj.get("aliases", ""),
                    "sub_objects": obj.get("sub_objects", []),
                    "ra": obj["ra"],
                    "dec": obj["dec"],
                    "mag": mag,
                    "size": size_str,
                    "maj_ax": maj,
                    "max_altitude": obj.get("max_altitude", 0),
                    "hours_visible": obj.get("hours_visible", 0),
                    "status": "cached" if is_cached else "pending",
                    "image_url": url if is_cached else "",
                    "sensor_fov": self.sensor_fov_data,
                    "image_fov": self.download_fov,
                    "setup_hash": self.setup_hash,
                }
            )
        return f"event: catalog_metadata\ndata: {json.dumps(metadata)}\n\n"

    def prioritize_top_objects(self):
        mode = self.settings.get("download_mode", "selected")
        filtered_candidates, fav_names, filter_favs_only = _filter_objects_by_settings(
            self.all_objects, self.settings
        )

        # Ensure any bookmarked objects are included in top_objects so their altitude graphs are precomputed
        if fav_names:
            fav_exact, fav_compact = _build_fav_lookup(fav_names)
            fav_objs = [
                o
                for o in filtered_candidates
                if _is_favorite_object(o, fav_exact, fav_compact)
            ]
            fav_ids = {o.get("id") for o in fav_objs}
            non_fav_objs = [
                o for o in filtered_candidates if o.get("id") not in fav_ids
            ]
            self.top_objects = (fav_objs + non_fav_objs)[:500]
        else:
            self.top_objects = filtered_candidates[:500]

        self.download_list = _select_download_list(
            mode, self.all_objects, filtered_candidates, fav_names, filter_favs_only
        )

    def _build_batch_details_sync(self, download_ids: set, min_alt: float) -> Optional[dict]:
        if not self.top_objects:
            return None

        tw_cfg = _extract_time_window_settings(self.settings)
        ephemeris = calculator.prepare_session_ephemeris(
            location=self.location,
            start_time=self.session_start,
            end_time=self.session_end,
            num_points=60,
            twilight_periods=self.twilight,
            time_window_active=tw_cfg["active"],
            time_window_start=tw_cfg["start"],
            time_window_end=tw_cfg["end"],
            tz_offset_minutes=tw_cfg["tz_offset"],
        )

        ra_array = np.array(
            [float(o.get("ra") or 0.0) for o in self.top_objects], dtype=float
        )
        dec_array = np.array(
            [float(o.get("dec") or 0.0) for o in self.top_objects], dtype=float
        )
        graphs_list, hours_list = calculator.batch_compute_altitude_graphs(
            ra_array=ra_array,
            dec_array=dec_array,
            ephemeris=ephemeris,
            min_altitude=min_alt,
        )

        fov_rect_dict = self.fov_info["fov_rect"].model_dump()
        sensor_fov = self.fov_info["sensor_fov"]
        image_fov = self.fov_info["download_fov"]
        setup_hash = self.fov_info["setup_hash"]
        moon_dicts = ephemeris["moon_dicts"]
        sun_dicts = ephemeris["sun_dicts"]

        results = []
        for obj, alt_graph, hours in zip(self.top_objects, graphs_list, hours_list):
            obj_id = obj["id"]
            url, filepath, _ = get_cache_info(obj_id, setup_hash)
            is_cached = os.path.exists(filepath)

            if is_cached:
                status = "cached"
            elif obj_id in download_ids:
                status = "queued"
            else:
                status = "pending"

            results.append(
                {
                    "name": obj_id,
                    "image_url": url if is_cached else "",
                    "altitude_graph": alt_graph,
                    "fov_rectangle": fov_rect_dict,
                    "sensor_fov": sensor_fov,
                    "image_fov": image_fov,
                    "hours_above_min": hours,
                    "setup_hash": setup_hash,
                    "status": status,
                }
            )
        return {
            "moon_graph": moon_dicts,
            "sun_graph": sun_dicts,
            "items": results,
        }

    async def stream_details(self):
        if not self.top_objects:
            return

        download_ids = set(o["id"] for o in self.download_list)
        if background_download_manager.active:
            download_ids = download_ids | set(background_download_manager.queued_ids)
        min_hours_alt = _extract_min_hours_altitude(self.settings)

        batch_payload = await asyncio.to_thread(
            self._build_batch_details_sync, download_ids, min_hours_alt
        )
        if not batch_payload or await self.request.is_disconnected():
            return

        yield f"event: object_details_batch\ndata: {json.dumps(batch_payload)}\n\n"

    async def stream_downloads(self):
        sub_queue = background_download_manager.subscribe()
        try:
            if self.download_list:
                mode = self.settings.get("download_mode", "filtered")
                status = await background_download_manager.start_job(
                    download_list=self.download_list,
                    fov_info=self.fov_info,
                    mode=mode if mode in ("filtered", "all") else "filtered",
                )
                if status["total"] == 0:
                    return
                yield (
                    f"event: download_progress\ndata: "
                    f"{json.dumps({'current': 0, 'total': status['total'], 'succeeded': status.get('succeeded', 0), 'failed': status.get('failed', 0), 'already_cached': status['already_cached'], 'total_candidates': status['total_candidates'], 'active_targets': status.get('active_targets', []), 'image_fov': status.get('image_fov'), 'image_source': status.get('image_source')})}\n\n"
                )
            else:
                if not background_download_manager.active:
                    return
                status = background_download_manager.get_status()
                yield (
                    f"event: download_progress\ndata: "
                    f"{json.dumps({'current': status['current'], 'total': status['total'], 'succeeded': status.get('succeeded', 0), 'failed': status.get('failed', 0), 'already_cached': status['already_cached'], 'total_candidates': status['total_candidates'], 'active_targets': status.get('active_targets', []), 'last_error': status.get('last_error', ''), 'last_error_target': status.get('last_error_target', ''), 'image_fov': status.get('image_fov'), 'image_source': status.get('image_source')})}\n\n"
                )

            while True:
                if await self.request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(sub_queue.get(), timeout=0.25)
                except asyncio.TimeoutError:
                    if not background_download_manager.active and sub_queue.empty():
                        break
                    continue

                if msg is None:
                    break
                if "progress" in msg:
                    yield f"event: download_progress\ndata: {json.dumps(msg)}\n\n"
                else:
                    yield f"event: image_status\ndata: {json.dumps(msg)}\n\n"
        finally:
            background_download_manager.unsubscribe(sub_queue)
