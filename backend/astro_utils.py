import csv
import io
import math
import re
import uuid
import warnings
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import astropy.units as u
import httpx
import numpy as np
from astroplan import Observer
from astropy.coordinates import AltAz, EarthLocation, SkyCoord, get_body, get_sun
from astropy.time import Time
from astropy.utils import iers
from PIL import Image

from .models import AltitudePoint, Camera, Location, Telescope

# Prevent Astropy from blocking on remote IERS-A downloads or raising ValueError
# when offline at a dark-sky site or planning future observing dates (>30 days out).
iers.conf.auto_max_age = None
iers.conf.auto_download = False


class AstroCalculator:
    """Performs astronomical calculations."""

    def __init__(self):
        self._solar_system_cache = {}

    def calculate_fov(
        self, telescope: Telescope, camera: Camera
    ) -> Tuple[float, float]:
        """Calculates the field of view in arcminutes using exact formula."""
        fov_width_rad = 2 * math.atan(camera.sensor_width / (2 * telescope.focal_length))
        fov_height_rad = 2 * math.atan(
            camera.sensor_height / (2 * telescope.focal_length)
        )

        fov_width = fov_width_rad * (180 / math.pi) * 60
        fov_height = fov_height_rad * (180 / math.pi) * 60
        return fov_width, fov_height

    def _find_solar_crossings(
        self,
        earth_location: EarthLocation,
        ref_time: Time,
        horizon_deg: float = 0.0,
        window_before_h: float = 30.0,
        window_after_h: float = 30.0,
        step_min: float = 10.0,
    ) -> Tuple[List[Time], List[Time]]:
        """
        Finds upward (rise) and downward (set) solar altitude crossings of `horizon_deg`
        over `[ref_time - window_before_h, ref_time + window_after_h]` using a padded
        vectorized grid + bracketed secant refinement (immune to astroplan boundary bugs).
        """
        total_h = window_before_h + window_after_h
        n_pts = max(int(round(total_h * 60.0 / step_min)) + 1, 8)
        offsets_h = np.linspace(-window_before_h, window_after_h, n_pts)
        grid = ref_time + offsets_h * u.hour
        alts = (
            get_sun(grid)
            .transform_to(AltAz(obstime=grid, location=earth_location))
            .alt.deg
            - horizon_deg
        )

        diff_sign = (alts[:-1] <= 0.0) != (alts[1:] <= 0.0)
        idxs = np.where(diff_sign)[0]
        if len(idxs) == 0:
            return [], []

        t_a = offsets_h[idxs].astype(float)
        t_b = offsets_h[idxs + 1].astype(float)
        y_a = alts[idxs].astype(float)
        y_b = alts[idxs + 1].astype(float)
        is_rise = y_b > y_a

        for _ in range(2):
            denom = np.where(np.abs(y_b - y_a) > 1e-12, y_b - y_a, 1e-12)
            t_m = np.clip(
                t_a - y_a * (t_b - t_a) / denom,
                np.minimum(t_a, t_b),
                np.maximum(t_a, t_b),
            )
            t_mid = ref_time + t_m * u.hour
            y_m = (
                get_sun(t_mid)
                .transform_to(AltAz(obstime=t_mid, location=earth_location))
                .alt.deg
                - horizon_deg
            )
            same_as_a = (y_m <= 0.0) == (y_a <= 0.0)
            t_a = np.where(same_as_a, t_m, t_a)
            y_a = np.where(same_as_a, y_m, y_a)
            t_b = np.where(~same_as_a, t_m, t_b)
            y_b = np.where(~same_as_a, y_m, y_b)

        denom = np.where(np.abs(y_b - y_a) > 1e-12, y_b - y_a, 1e-12)
        t_root = np.clip(
            t_a - y_a * (t_b - t_a) / denom,
            np.minimum(t_a, t_b),
            np.maximum(t_a, t_b),
        )
        roots = ref_time + t_root * u.hour
        rises = [roots[i] for i in range(len(idxs)) if is_rise[i]]
        sets = [roots[i] for i in range(len(idxs)) if not is_rise[i]]
        return rises, sets

    def _resolve_observing_night(
        self, earth_location: EarthLocation, ref_time: Time
    ) -> Optional[Tuple[Time, Time]]:
        """
        Returns `(sunset, sunrise)` for the active night (if Sun <= 0°) or upcoming night
        (if Sun > 0°), guaranteed to span a single night (`0 < sunrise - sunset <= 24.5h`).
        Returns `None` in continuous polar day or continuous polar night.
        """
        sun_alt_now = float(
            get_sun(ref_time)
            .transform_to(AltAz(obstime=ref_time, location=earth_location))
            .alt.deg
        )
        rises, sets = self._find_solar_crossings(
            earth_location,
            ref_time,
            horizon_deg=0.0,
            window_before_h=30.0,
            window_after_h=30.0,
            step_min=10.0,
        )
        if not rises or not sets:
            return None

        tol = 5.0 * u.second
        min_gap = 1.0 * u.minute

        if sun_alt_now <= 0.0:
            past_sets = [t for t in sets if t <= ref_time + tol]
            if not past_sets:
                return None
            sunset = past_sets[-1]
            next_rises = [
                t for t in rises if t >= ref_time - tol and t > sunset + min_gap
            ]
            if not next_rises:
                return None
            sunrise = next_rises[0]
        else:
            future_sets = [t for t in sets if t >= ref_time - tol]
            if not future_sets:
                return None
            sunset = future_sets[0]
            next_rises = [t for t in rises if t > sunset + min_gap]
            if not next_rises:
                return None
            sunrise = next_rises[0]

        night_hours = (sunrise - sunset).to(u.hour).value
        if night_hours <= 0.0 or night_hours > 24.5:
            return None
        return sunset, sunrise

    def get_observing_session(self, observer: Observer, now: Time) -> Tuple[Time, Time]:
        """
        Determines the observing session (Sunset - 0.5h to Sunrise + 0.5h).
        If currently night, returns (previous_sunset - 0.5h, next_sunrise + 0.5h).
        If currently day, returns (next_sunset - 0.5h, next_sunrise + 0.5h).
        """
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                bounds = self._resolve_observing_night(observer.location, now)
                if bounds is None:
                    raise ValueError("Polar day or night without horizon crossing")
                sunset, sunrise = bounds
                return sunset - 0.5 * u.hour, sunrise + 0.5 * u.hour
        except Exception:
            return now - 12 * u.hour, now + 12 * u.hour

    def get_observing_session_for_date(
        self,
        observer: Observer,
        obs_date: Optional[str] = None,
        now: Optional[Time] = None,
    ) -> Tuple[Time, Time]:
        """
        Resolves the single-night observing session for an explicit evening date `YYYY-MM-DD`
        (evening of `obs_date` -> morning of `obs_date + 1 day`), or falls back to `now` / `Time.now()`.
        """
        if obs_date and isinstance(obs_date, str):
            cleaned = obs_date.strip()
            if re.match(r"^\d{4}-\d{2}-\d{2}$", cleaned):
                try:
                    datetime.strptime(cleaned, "%Y-%m-%d")
                    lon_deg = float(observer.location.lon.deg)
                    t_noon = (
                        Time(f"{cleaned}T12:00:00", format="isot", scale="utc")
                        - (lon_deg / 15.0) * u.hour
                    )
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore")
                        bounds = self._resolve_observing_night(
                            observer.location, t_noon
                        )
                        if bounds is not None:
                            sunset, sunrise = bounds
                            return sunset - 0.5 * u.hour, sunrise + 0.5 * u.hour
                    return t_noon, t_noon + 24.0 * u.hour
                except Exception:
                    pass
        return self.get_observing_session(observer, now if now is not None else Time.now())

    def batch_get_max_altitude(
        self, dec_array: np.ndarray, latitude: float
    ) -> np.ndarray:
        """
        Vectorized calculation of max altitude for an array of declinations.
        """
        try:
            return np.maximum(0, 90 - np.abs(latitude - dec_array))
        except Exception as e:
            print(f"Error in batch_get_max_altitude: {e}")
            return np.zeros_like(dec_array)

    def parse_hhmm_to_minutes(self, time_str: Optional[str]) -> Optional[int]:
        """Parses a 'HH:MM' string into minutes since midnight [0, 1440)."""
        if not time_str or not isinstance(time_str, str):
            return None
        parts = time_str.strip().split(":")
        if len(parts) < 2:
            return None
        try:
            hours = int(parts[0])
            minutes = int(parts[1])
            if not (0 <= hours <= 24 and 0 <= minutes < 60):
                return None
            if hours == 24 and minutes == 0:
                return 0
            if hours >= 24:
                return None
            return hours * 60 + minutes
        except ValueError:
            return None

    def format_iso_to_hhmm(
        self, iso_str: str, tz_offset_minutes: Optional[float] = None
    ) -> Optional[str]:
        """Formats an ISO UTC timestamp string into local 'HH:MM'."""
        if not iso_str:
            return None
        try:
            dt_utc = Time(iso_str).to_datetime(timezone=timezone.utc)
            if tz_offset_minutes is not None:
                local_dt = dt_utc - timedelta(minutes=float(tz_offset_minutes))
            else:
                local_dt = dt_utc.astimezone()
            return f"{local_dt.hour:02d}:{local_dt.minute:02d}"
        except Exception:
            return None

    def build_time_window_mask(
        self,
        times: Time,
        start_str: Optional[str] = None,
        end_str: Optional[str] = None,
        tz_offset_minutes: Optional[float] = None,
        twilight_periods: Optional[Dict[str, List[str]]] = None,
    ) -> np.ndarray:
        """
        Builds a boolean mask for an astropy Time array matching a local 'HH:MM' time window.
        Falls back to astronomical night start/end from twilight_periods if start_str or end_str is empty.
        """
        n = len(times) if hasattr(times, "__len__") else 1
        if (not start_str or not start_str.strip()) and twilight_periods and "night" in twilight_periods:
            start_str = self.format_iso_to_hhmm(
                twilight_periods["night"][0], tz_offset_minutes
            )
        if (not end_str or not end_str.strip()) and twilight_periods and "night" in twilight_periods:
            end_str = self.format_iso_to_hhmm(
                twilight_periods["night"][1], tz_offset_minutes
            )

        start_min = self.parse_hhmm_to_minutes(start_str)
        end_min = self.parse_hhmm_to_minutes(end_str)
        if start_min is None or end_min is None:
            return np.ones(n, dtype=bool)

        try:
            dts = times.to_datetime(timezone=timezone.utc)
            if not isinstance(dts, (list, tuple, np.ndarray)):
                dts = [dts]
            if tz_offset_minutes is not None:
                offset_td = timedelta(minutes=float(tz_offset_minutes))
                local_dts = [dt - offset_td for dt in dts]
            else:
                local_dts = [dt.astimezone() for dt in dts]

            mins = np.array([dt.hour * 60 + dt.minute for dt in local_dts])
            if start_min <= end_min:
                return (mins >= start_min) & (mins <= end_min)
            else:
                return (mins >= start_min) | (mins <= end_min)
        except Exception as e:
            print(f"Error building time window mask: {e}")
            return np.ones(n, dtype=bool)

    def prepare_session_ephemeris(
        self,
        location: Location,
        start_time: Optional[Time] = None,
        end_time: Optional[Time] = None,
        num_points: int = 60,
        twilight_periods: Optional[Dict[str, List[str]]] = None,
        time_window_active: bool = False,
        time_window_start: Optional[str] = None,
        time_window_end: Optional[str] = None,
        tz_offset_minutes: Optional[float] = None,
    ) -> dict:
        """
        Precomputes the session AltAz time grid, Moon curve, Sun curve, and night mask once
        so multiple objects can be evaluated without redundant ephemeris calculations.
        """
        observer_location = EarthLocation(
            lat=location.latitude * u.deg, lon=location.longitude * u.deg
        )
        observer = Observer(location=observer_location)

        if start_time is None or end_time is None:
            start_time, end_time = self.get_observing_session(observer, Time.now())

        duration = (end_time - start_time).to(u.hour).value
        if duration < 1:
            duration = 24.0

        times = start_time + np.linspace(0, duration, num_points) * u.hour
        time_strings = [t.isot + "Z" for t in times]
        altaz_frame = AltAz(obstime=times, location=observer_location)

        try:
            moon_coords = get_body("moon", times, location=observer_location)
            moon_altaz = moon_coords.transform_to(altaz_frame)
            moon_alts = np.round(moon_altaz.alt.deg, 2)
            moon_points = [
                AltitudePoint(time=t_str, altitude=float(alt))
                for t_str, alt in zip(time_strings, moon_alts)
            ]
            moon_dicts = [
                {"time": t_str, "altitude": float(alt)}
                for t_str, alt in zip(time_strings, moon_alts)
            ]
        except Exception as e:
            print(f"Error calculating moon altitude: {e}")
            moon_points = []
            moon_dicts = []

        try:
            sun_coords = get_sun(times)
            sun_altaz = sun_coords.transform_to(altaz_frame)
            sun_alts = np.round(sun_altaz.alt.deg, 2)
            sun_points = [
                AltitudePoint(time=t_str, altitude=float(alt))
                for t_str, alt in zip(time_strings, sun_alts)
            ]
            sun_dicts = [
                {"time": t_str, "altitude": float(alt)}
                for t_str, alt in zip(time_strings, sun_alts)
            ]
        except Exception as e:
            print(f"Error calculating sun altitude: {e}")
            sun_points = []
            sun_dicts = []

        night_mask = np.ones(len(times), dtype=bool)
        if twilight_periods and "night" in twilight_periods:
            try:
                night_start = Time(twilight_periods["night"][0])
                night_end = Time(twilight_periods["night"][1])
                night_mask = (times >= night_start) & (times <= night_end)
            except Exception:
                pass

        if time_window_active:
            window_mask = self.build_time_window_mask(
                times=times,
                start_str=time_window_start,
                end_str=time_window_end,
                tz_offset_minutes=tz_offset_minutes,
                twilight_periods=twilight_periods,
            )
            night_mask = night_mask & window_mask

        time_step_hours = duration / (num_points - 1) if num_points > 1 else 0.0

        return {
            "times": times,
            "time_strings": time_strings,
            "altaz_frame": altaz_frame,
            "moon_points": moon_points,
            "sun_points": sun_points,
            "moon_dicts": moon_dicts,
            "sun_dicts": sun_dicts,
            "night_mask": night_mask,
            "time_step_hours": time_step_hours,
        }

    def batch_compute_altitude_graphs(
        self,
        ra_array: np.ndarray,
        dec_array: np.ndarray,
        ephemeris: dict,
        min_altitude: float,
    ) -> Tuple[List[List[dict]], List[float]]:
        """
        Vectorized computation of altitude graph dicts and hours_above_min for an array of objects.
        """
        if len(ra_array) == 0:
            return [], []

        altaz_frame = ephemeris["altaz_frame"]
        time_strings = ephemeris["time_strings"]
        night_mask = ephemeris["night_mask"]
        time_step_hours = ephemeris["time_step_hours"]

        targets = SkyCoord(ra_array, dec_array, unit=(u.deg, u.deg))[:, np.newaxis]
        target_altaz = targets.transform_to(altaz_frame)
        altitudes = np.round(target_altaz.alt.deg, 2)

        above_mask = (altitudes >= min_altitude) & night_mask[np.newaxis, :]
        valid_counts = np.sum(above_mask, axis=1)
        hours_list = [round(int(c) * time_step_hours, 1) for c in valid_counts]

        graphs_list = [
            [
                {"time": t_str, "altitude": float(alt)}
                for t_str, alt in zip(time_strings, row_alts)
            ]
            for row_alts in altitudes
        ]

        return graphs_list, hours_list

    def get_altitude_graph(
        self,
        ra: str,
        dec: str,
        location: Location,
        num_points: int = 60,
        start_time: Optional[Time] = None,
        end_time: Optional[Time] = None,
        ephemeris: Optional[dict] = None,
    ) -> Dict[str, List[AltitudePoint]]:
        """
        Generates altitude data for an object, the Moon, and the Sun.
        If start_time/end_time are not provided, calculates them for the current observing session.
        """
        if isinstance(ra, (float, int)) and isinstance(dec, (float, int)):
            target_coords = SkyCoord(ra, dec, unit=(u.deg, u.deg))
        else:
            ra_formatted = re.sub(r"[^\d.\-]", " ", str(ra)).strip()
            dec_formatted = re.sub(r"[^\d.\-]", " ", str(dec)).strip()
            target_coords = SkyCoord(
                ra_formatted, dec_formatted, unit=(u.hourangle, u.deg)
            )

        if ephemeris is None:
            ephemeris = self.prepare_session_ephemeris(
                location=location,
                start_time=start_time,
                end_time=end_time,
                num_points=num_points,
            )

        altaz_frame = ephemeris["altaz_frame"]
        time_strings = ephemeris["time_strings"]

        target_altaz = target_coords.transform_to(altaz_frame)
        target_alts = np.round(target_altaz.alt.deg, 2)
        target_points = [
            AltitudePoint(time=t_str, altitude=float(alt))
            for t_str, alt in zip(time_strings, target_alts)
        ]

        return {
            "target": target_points,
            "moon": ephemeris["moon_points"],
            "sun": ephemeris["sun_points"],
        }

    def prepare_night_frame(
        self, location: Location, night_start: Time, night_end: Time
    ) -> Optional[Tuple[AltAz, float]]:
        """
        Pre-calculates the AltAz frame for the night period to speed up batch calculations.
        Returns (AltAz_Frame, duration_in_hours).
        """
        try:
            duration = (night_end - night_start).to(u.hour).value
            if duration <= 0:
                return None

            num_points = max(int(duration * 4), 2)
            times = night_start + np.linspace(0, duration, num_points) * u.hour
            observer_location = EarthLocation(
                lat=location.latitude * u.deg, lon=location.longitude * u.deg
            )
            altaz_frame = AltAz(obstime=times, location=observer_location)

            return altaz_frame, duration
        except Exception:
            return None

    def batch_calculate_nightly_hours(
        self,
        ra_array: np.ndarray,
        dec_array: np.ndarray,
        altaz_frame: AltAz,
        duration: float,
        min_altitude: float,
        time_mask: Optional[np.ndarray] = None,
    ) -> np.ndarray:
        """
        Vectorized calculation of nightly hours visible.
        ra_array, dec_array: numpy arrays of float degrees.
        """
        try:
            targets = SkyCoord(ra_array, dec_array, unit=(u.deg, u.deg))[:, np.newaxis]
            target_altaz = targets.transform_to(altaz_frame)
            altitudes = target_altaz.alt.deg

            above_mask = altitudes >= min_altitude
            if time_mask is not None and len(time_mask) == altitudes.shape[1]:
                above_mask = above_mask & time_mask[np.newaxis, :]

            count_above = np.sum(above_mask, axis=1)
            num_times = altitudes.shape[1]
            if num_times == 0:
                return np.zeros_like(ra_array)

            hours = (count_above / num_times) * duration
            return np.round(hours, 1)

        except Exception as e:
            print(f"Error in batch_calculate_nightly_hours: {e}")
            return np.zeros_like(ra_array)

    def calculate_time_above_altitude(
        self,
        altitude_points: List[AltitudePoint],
        min_altitude: float,
        twilight_periods: Optional[Dict[str, List[str]]] = None,
        time_window_active: bool = False,
        time_window_start: Optional[str] = None,
        time_window_end: Optional[str] = None,
        tz_offset_minutes: Optional[float] = None,
    ) -> float:
        """
        Estimates the total hours an object is above a minimum altitude based on the graph points.
        If twilight_periods is provided (and contains 'night'), only counts hours during the night.
        """
        if not altitude_points:
            return 0.0

        valid_points = []
        night_start = None
        night_end = None

        if twilight_periods and "night" in twilight_periods:
            try:
                night_start = Time(twilight_periods["night"][0])
                night_end = Time(twilight_periods["night"][1])
            except Exception:
                pass

        window_mask = None
        if time_window_active:
            try:
                pt_times = Time([p.time for p in altitude_points])
                window_mask = self.build_time_window_mask(
                    times=pt_times,
                    start_str=time_window_start,
                    end_str=time_window_end,
                    tz_offset_minutes=tz_offset_minutes,
                    twilight_periods=twilight_periods,
                )
            except Exception:
                window_mask = None

        for idx, p in enumerate(altitude_points):
            if p.altitude >= min_altitude:
                if window_mask is not None and not window_mask[idx]:
                    continue
                if night_start and night_end:
                    pt = Time(p.time)
                    if night_start <= pt <= night_end:
                        valid_points.append(p)
                else:
                    valid_points.append(p)

        if len(valid_points) < 1:
            return 0.0

        if len(altitude_points) > 1:
            try:
                span_hours = (
                    Time(altitude_points[-1].time) - Time(altitude_points[0].time)
                ).to(u.hour).value
                if span_hours <= 0:
                    span_hours = 24.0
            except Exception:
                span_hours = 24.0
            time_step_hours = span_hours / (len(altitude_points) - 1)
        else:
            time_step_hours = 0.0
        return round(len(valid_points) * time_step_hours, 1)

    def get_twilight_periods(
        self, location: Location, base_time: Optional[Time] = None
    ) -> Dict[str, List[str]]:
        """Calculates twilight periods for the active or upcoming observing night."""
        earth_loc = EarthLocation(
            lat=location.latitude * u.deg, lon=location.longitude * u.deg
        )
        now = base_time if base_time else Time.now()
        end_time = now + 24 * u.hour

        periods = {}

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            try:
                bounds = self._resolve_observing_night(earth_loc, now)
                if bounds is None:
                    sun_alt = (
                        get_sun(now)
                        .transform_to(AltAz(location=earth_loc, obstime=now))
                        .alt.deg
                    )
                    if sun_alt > 0:
                        return {"day": [now.isot + "Z", end_time.isot + "Z"]}
                    elif sun_alt > -6:
                        return {"civil": [now.isot + "Z", end_time.isot + "Z"]}
                    elif sun_alt > -12:
                        return {"nautical": [now.isot + "Z", end_time.isot + "Z"]}
                    elif sun_alt > -18:
                        return {"astronomical": [now.isot + "Z", end_time.isot + "Z"]}
                    else:
                        return {"night": [now.isot + "Z", end_time.isot + "Z"]}

                sunset, sunrise = bounds
                night_mid = sunset + 0.5 * (sunrise - sunset)
                half_span_h = 0.5 * (sunrise - sunset).to(u.hour).value + 0.25

                def get_twilight_pair(
                    horizon_deg: float,
                ) -> Tuple[Optional[Time], Optional[Time]]:
                    rises, sets = self._find_solar_crossings(
                        earth_loc,
                        night_mid,
                        horizon_deg=horizon_deg,
                        window_before_h=half_span_h,
                        window_after_h=half_span_h,
                        step_min=6.0,
                    )
                    eve_candidates = [t for t in sets if sunset <= t <= sunrise]
                    morn_candidates = [t for t in rises if sunset <= t <= sunrise]
                    eve_t = eve_candidates[0] if eve_candidates else None
                    morn_t = morn_candidates[-1] if morn_candidates else None
                    return eve_t, morn_t

                eve_civil, morn_civil = get_twilight_pair(-6.0)
                eve_naut, morn_naut = get_twilight_pair(-12.0)
                eve_astro, morn_astro = get_twilight_pair(-18.0)

                def add_p(name, start, end):
                    if start and end and start < end:
                        periods[name] = [start.isot + "Z", end.isot + "Z"]

                t1 = sunset
                if eve_civil:
                    add_p("civil", t1, eve_civil)
                    t1 = eve_civil
                if eve_naut:
                    add_p("nautical", t1, eve_naut)
                    t1 = eve_naut
                if eve_astro:
                    add_p("astronomical", t1, eve_astro)
                    t1 = eve_astro

                t2 = sunrise
                if morn_civil:
                    add_p("civil_morn", morn_civil, t2)
                    t2 = morn_civil
                if morn_naut:
                    add_p("nautical_morn", morn_naut, t2)
                    t2 = morn_naut
                if morn_astro:
                    add_p("astronomical_morn", morn_astro, t2)
                    t2 = morn_astro

                if t1 and t2 and t1 < t2:
                    midnight = t1 + (t2 - t1) / 2
                    midnight_alt = (
                        get_sun(midnight)
                        .transform_to(AltAz(location=earth_loc, obstime=midnight))
                        .alt.deg
                    )

                    if midnight_alt >= -6:
                        middle_phase = "civil"
                    elif midnight_alt >= -12:
                        middle_phase = "nautical"
                    elif midnight_alt >= -18:
                        middle_phase = "astronomical"
                    else:
                        middle_phase = "night"

                    add_p(middle_phase, t1, t2)

                return periods
            except Exception as e:
                print(f"Twilight calculation error: {e}")
                sun_alt = (
                    get_sun(now)
                    .transform_to(AltAz(location=earth_loc, obstime=now))
                    .alt.deg
                )
                return (
                    {"day": [now.isot + "Z", end_time.isot + "Z"]}
                    if sun_alt > -18
                    else {"night": [now.isot + "Z", end_time.isot + "Z"]}
                )

    def get_solar_system_positions(
        self,
        lat: float,
        lon: float,
        elevation: float = 0.0,
        time_str: Optional[str] = None,
    ) -> List[dict]:
        """Calculates topocentric positions for Moon, Sun, and planets."""
        t = Time(time_str) if time_str else Time.now()
        time_bucket = int(t.unix // 10) * 10
        cache_key = (round(float(lat), 3), round(float(lon), 3), round(float(elevation), 1), time_bucket)

        if hasattr(self, "_solar_system_cache") and cache_key in self._solar_system_cache:
            return self._solar_system_cache[cache_key]

        location = EarthLocation(
            lat=lat * u.deg, lon=lon * u.deg, height=elevation * u.m
        )
        altaz_frame = AltAz(obstime=t, location=location)

        sun_gcrs = get_sun(t)
        sun_altaz = sun_gcrs.transform_to(altaz_frame)

        moon_gcrs = get_body("moon", t, location=location)
        moon_altaz = moon_gcrs.transform_to(altaz_frame)
        sep_deg = float(sun_gcrs.separation(moon_gcrs).deg)
        phase_angle_deg = max(0.0, 180.0 - sep_deg)
        ill_frac = float(0.5 * (1.0 - np.cos(np.radians(sep_deg))))

        # Determine waxing vs waning from geocentric ecliptic longitude difference (0..360 deg)
        eps_rad = np.radians(23.439291)
        cos_eps, sin_eps = np.cos(eps_rad), np.sin(eps_rad)

        def _eq_to_ecl_lon_deg(sc) -> float:
            ra_r = np.radians(float(sc.ra.deg))
            dec_r = np.radians(float(sc.dec.deg))
            x = np.cos(dec_r) * np.cos(ra_r)
            y = cos_eps * np.cos(dec_r) * np.sin(ra_r) + sin_eps * np.sin(dec_r)
            return float(np.degrees(np.arctan2(y, x)) % 360.0)

        dlon_synodic = (_eq_to_ecl_lon_deg(moon_gcrs) - _eq_to_ecl_lon_deg(sun_gcrs)) % 360.0
        if dlon_synodic < 12.0 or dlon_synodic >= 348.0:
            phase_name = "New Moon"
        elif dlon_synodic < 78.0:
            phase_name = "Waxing Crescent"
        elif dlon_synodic < 102.0:
            phase_name = "First Quarter"
        elif dlon_synodic < 168.0:
            phase_name = "Waxing Gibbous"
        elif dlon_synodic < 192.0:
            phase_name = "Full Moon"
        elif dlon_synodic < 258.0:
            phase_name = "Waning Gibbous"
        elif dlon_synodic < 282.0:
            phase_name = "Third Quarter"
        else:
            phase_name = "Waning Crescent"

        moon_mag = min(
            -2.5,
            -12.73 + 0.026 * phase_angle_deg + 4e-9 * (phase_angle_deg ** 4),
        )

        bodies_config = [
            ("sun", "Sun", "☀️", "#FFD700", -26.74, sun_gcrs, sun_altaz),
            ("moon", "Moon", "🌙", "#E2E8F0", moon_mag, moon_gcrs, moon_altaz),
        ]

        results = []
        for b_id, name, sym, color, mag, gcrs, altaz in bodies_config:
            item = {
                "id": b_id,
                "name": name,
                "symbol": sym,
                "color": color,
                "ra": round(float(gcrs.ra.deg), 5),
                "dec": round(float(gcrs.dec.deg), 5),
                "alt": round(float(altaz.alt.deg), 2),
                "az": round(float(altaz.az.deg), 2),
                "mag": round(float(mag), 1),
                "distance_au": (
                    round(float(gcrs.distance.au), 6)
                    if hasattr(gcrs.distance, "au")
                    else None
                ),
            }
            if b_id == "moon":
                item["phase_percent"] = round(ill_frac * 100, 1)
                item["phase_name"] = phase_name
                item["elongation_deg"] = round(sep_deg, 1)
            results.append(item)

        planet_meta = [
            ("mercury", "Mercury", "⚪", "#A1A1AA", -0.4),
            ("venus", "Venus", "🌟", "#FDE047", -4.4),
            ("mars", "Mars", "🔴", "#EF4444", 0.5),
            ("jupiter", "Jupiter", "🪐", "#F59E0B", -2.2),
            ("saturn", "Saturn", "🪐", "#FCD34D", 0.5),
            ("uranus", "Uranus", "🔵", "#38BDF8", 5.7),
            ("neptune", "Neptune", "🔵", "#6366F1", 7.8),
        ]

        for b_id, name, sym, color, default_mag in planet_meta:
            try:
                p_gcrs = get_body(b_id, t, location=location)
                p_altaz = p_gcrs.transform_to(altaz_frame)
                results.append({
                    "id": b_id,
                    "name": name,
                    "symbol": sym,
                    "color": color,
                    "ra": round(float(p_gcrs.ra.deg), 5),
                    "dec": round(float(p_gcrs.dec.deg), 5),
                    "alt": round(float(p_altaz.alt.deg), 2),
                    "az": round(float(p_altaz.az.deg), 2),
                    "mag": round(float(default_mag), 1),
                    "distance_au": round(float(p_gcrs.distance.au), 4),
                })
            except Exception as e:
                print(f"Error calculating planet {b_id}: {e}")

        # Pluto GCRS calculation with Standish (1800-2050) Keplerian fallback
        try:
            p_gcrs = get_body("pluto", t, location=location)
            p_altaz = p_gcrs.transform_to(altaz_frame)
            results.append({
                "id": "pluto",
                "name": "Pluto",
                "symbol": "♇",
                "color": "#94A3B8",
                "ra": round(float(p_gcrs.ra.deg), 5),
                "dec": round(float(p_gcrs.dec.deg), 5),
                "alt": round(float(p_altaz.alt.deg), 2),
                "az": round(float(p_altaz.az.deg), 2),
                "mag": 14.4,
                "distance_au": round(float(p_gcrs.distance.au), 4),
            })
        except Exception:
            t_cent = (float(t.jd) - 2451545.0) / 36525.0
            a_p = 39.48211675 - 0.00031596 * t_cent
            e_p = 0.24882730 + 0.00005170 * t_cent
            i_p = math.radians(17.14001206 + 0.00004818 * t_cent)
            l_p = 238.92903833 + 145.20780515 * t_cent
            wbar_p = 224.06891629 - 0.04062942 * t_cent
            node_p = math.radians(110.30393684 - 0.01183482 * t_cent)
            arg_peri_p = math.radians((wbar_p - math.degrees(node_p)) % 360.0)
            m_rad = math.radians((l_p - wbar_p) % 360.0)
            ecc_anom = m_rad + e_p * math.sin(m_rad)
            for _ in range(4):
                ecc_anom -= (ecc_anom - e_p * math.sin(ecc_anom) - m_rad) / (
                    1.0 - e_p * math.cos(ecc_anom)
                )
            xv = a_p * (math.cos(ecc_anom) - e_p)
            yv = a_p * math.sqrt(1.0 - e_p * e_p) * math.sin(ecc_anom)
            r_helio = math.hypot(xv, yv)
            vw = math.atan2(yv, xv) + arg_peri_p
            xh = r_helio * (
                math.cos(node_p) * math.cos(vw)
                - math.sin(node_p) * math.sin(vw) * math.cos(i_p)
            )
            yh = r_helio * (
                math.sin(node_p) * math.cos(vw)
                + math.cos(node_p) * math.sin(vw) * math.cos(i_p)
            )
            zh = r_helio * (math.sin(vw) * math.sin(i_p))
            sun_cart = sun_gcrs.cartesian
            xe = xh + float(sun_cart.x.to_value(u.au))
            ye = yh * cos_eps - zh * sin_eps + float(sun_cart.y.to_value(u.au))
            ze = yh * sin_eps + zh * cos_eps + float(sun_cart.z.to_value(u.au))
            dist_au = math.sqrt(xe * xe + ye * ye + ze * ze)
            ra_p = math.degrees(math.atan2(ye, xe)) % 360.0
            dec_p = math.degrees(math.asin(max(-1.0, min(1.0, ze / dist_au))))
            pluto_target = SkyCoord(
                ra=ra_p * u.deg, dec=dec_p * u.deg, distance=dist_au * u.au
            )
            p_altaz = pluto_target.transform_to(altaz_frame)
            results.append({
                "id": "pluto",
                "name": "Pluto",
                "symbol": "♇",
                "color": "#94A3B8",
                "ra": round(ra_p, 5),
                "dec": round(dec_p, 5),
                "alt": round(float(p_altaz.alt.deg), 2),
                "az": round(float(p_altaz.az.deg), 2),
                "mag": round(-1.0 + 5.0 * math.log10(max(1.0, r_helio * dist_au)), 1),
                "distance_au": round(dist_au, 4),
            })

        if not hasattr(self, "_solar_system_cache"):
            self._solar_system_cache = {}

        if len(self._solar_system_cache) > 500:
            keys = list(self._solar_system_cache.keys())
            for k in keys[:250]:
                del self._solar_system_cache[k]

        self._solar_system_cache[cache_key] = results
        return results

    async def search_horizons_objects(self, query: str) -> List[dict]:
        """Searches NASA JPL Horizons Lookup API for comets, asteroids, planets, and spacecraft."""
        q = query.strip()
        if len(q) < 2:
            return []

        presets = [
            {
                "id": "c2023a3",
                "name": "C/2023 A3 (Tsuchinshan-ATLAS)",
                "type": "Comet",
                "command": "DES=C/2023 A3;CAP",
            },
            {
                "id": "12p",
                "name": "12P/Pons-Brooks",
                "type": "Comet",
                "command": "12P;CAP",
            },
            {
                "id": "1p",
                "name": "1P/Halley",
                "type": "Comet",
                "command": "1P;CAP",
            },
            {
                "id": "1ceres",
                "name": "1 Ceres",
                "type": "Dwarf Planet / Asteroid",
                "command": "1;",
            },
            {
                "id": "4vesta",
                "name": "4 Vesta",
                "type": "Asteroid",
                "command": "4;",
            },
            {
                "id": "433eros",
                "name": "433 Eros",
                "type": "NEO Asteroid",
                "command": "433;",
            },
            {
                "id": "99942apophis",
                "name": "99942 Apophis",
                "type": "NEO Asteroid",
                "command": "99942;",
            },
            {
                "id": "jwst",
                "name": "James Webb Space Telescope (JWST)",
                "type": "Spacecraft",
                "command": "-170",
            },
        ]

        results: List[dict] = []
        seen_commands = set()

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    "https://ssd.jpl.nasa.gov/api/horizons_lookup.api",
                    params={"sstr": q, "format": "json"},
                )
            if resp.status_code == 200:
                payload = resp.json()
                raw_items = payload.get("result") or []
                for item in raw_items[:20]:
                    name = str(item.get("name") or "").strip()
                    obj_type = str(item.get("type") or "Small Body").strip().title()
                    spkid = str(item.get("spkid") or "").strip()
                    pdes = str(item.get("pdes") or "").strip()
                    type_lower = obj_type.lower()

                    if "comet" in type_lower:
                        cmd = f"DES={spkid};CAP" if spkid else f"DES={pdes};CAP"
                    elif "asteroid" in type_lower:
                        if pdes and pdes.isdigit():
                            cmd = f"{pdes};"
                        elif spkid:
                            cmd = f"DES={spkid};"
                        else:
                            cmd = f"DES={pdes};"
                    else:
                        cmd = spkid or pdes or name

                    if not cmd or cmd in seen_commands:
                        continue
                    seen_commands.add(cmd)
                    results.append({
                        "id": spkid or cmd,
                        "name": name or cmd,
                        "type": obj_type,
                        "command": cmd,
                    })
        except Exception as exc:
            print(f"JPL Horizons lookup warning for '{q}': {exc}")

        q_lower = q.lower()
        for preset in presets:
            if (
                q_lower in preset["name"].lower()
                or q_lower in preset["command"].lower()
            ) and preset["command"] not in seen_commands:
                seen_commands.add(preset["command"])
                results.append(preset)

        if not results:
            results.append({
                "id": f"custom_{q}",
                "name": q,
                "type": "Direct JPL Command",
                "command": q,
            })

        return results

    async def fetch_horizons_ephemeris(
        self,
        command: str,
        object_name: str,
        start_time_str: str,
        stop_time_str: str,
        step_hours: int,
        lat: float,
        lon: float,
        elevation: float = 0.0,
        color: str = "#10B981",
    ) -> dict:
        """Queries JPL Horizons API asynchronously using CSV + decimal-degree format."""
        url = "https://ssd.jpl.nasa.gov/api/horizons.api"

        def format_jpl_time(t_input: str) -> str:
            raw = t_input.strip()
            try:
                if "T" in raw:
                    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                elif len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
                    dt = datetime.strptime(raw, "%Y-%m-%d")
                else:
                    dt = datetime.strptime(raw, "%Y-%m-%d %H:%M")
                return dt.strftime("%Y-%b-%d %H:%M")
            except Exception:
                return raw

        start_jpl = format_jpl_time(start_time_str)
        stop_jpl = format_jpl_time(stop_time_str)
        if start_jpl == stop_jpl:
            # Ensure non-zero interval when user picks same start/stop calendar date
            try:
                dt0 = datetime.strptime(start_jpl, "%Y-%b-%d %H:%M")
                stop_jpl = (dt0 + timedelta(days=1)).strftime("%Y-%b-%d %H:%M")
            except Exception:
                pass

        base_cmd = command.strip() if command.strip() else object_name.strip()
        step_h = max(1, int(step_hours))

        async def _query_horizons(cmd_str: str) -> str:
            params = {
                "format": "json",
                "COMMAND": f"'{cmd_str}'",
                "OBJ_DATA": "NO",
                "MAKE_EPHEM": "YES",
                "EPHEM_TYPE": "OBSERVER",
                "CENTER": "coord@399",
                "COORD_TYPE": "GEODETIC",
                "SITE_COORD": f"'{lon},{lat},{elevation / 1000.0}'",
                "START_TIME": f"'{start_jpl}'",
                "STOP_TIME": f"'{stop_jpl}'",
                "STEP_SIZE": f"'{step_h}h'",
                "QUANTITIES": "'1,9,20'",
                "CSV_FORMAT": "YES",
                "ANG_FORMAT": "DEG",
            }
            async with httpx.AsyncClient(timeout=25.0) as client:
                resp = await client.get(url, params=params)
            if resp.status_code != 200:
                raise ValueError(f"JPL Horizons returned HTTP {resp.status_code}")
            return resp.json().get("result", "")

        raw_text = await _query_horizons(base_cmd)
        used_cmd = base_cmd

        # Auto-retry with ;CAP if a comet designation returned a multi-apparition table
        if "$$SOE" not in raw_text and not base_cmd.upper().endswith("CAP"):
            if "Multiple major-bodies" not in raw_text and (
                "record #" in raw_text.lower() or "comet" in raw_text.lower() or "/" in base_cmd
            ):
                sep = "" if base_cmd.endswith(";") else ";"
                retry_cmd = f"{base_cmd}{sep}CAP"
                retry_text = await _query_horizons(retry_cmd)
                if "$$SOE" in retry_text:
                    raw_text = retry_text
                    used_cmd = retry_cmd

        soe_idx = raw_text.find("$$SOE")
        eoe_idx = raw_text.find("$$EOE")

        if soe_idx == -1 or eoe_idx == -1:
            lines = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]
            summary = "\n".join(lines[:14])
            raise ValueError(
                f"Ambiguous or unrecognized JPL Horizons target '{base_cmd}'. "
                f"Please pick a specific match from Search.\n{summary}"
            )

        # Locate CSV column header immediately preceding $$SOE
        pre_lines = [
            ln.strip()
            for ln in raw_text[:soe_idx].splitlines()
            if ln.strip() and not ln.strip().startswith("*")
        ]
        header_cols = []
        if pre_lines:
            header_cols = [c.strip().lower() for c in next(csv.reader([pre_lines[-1]]))]

        def _find_col(prefixes: Tuple[str, ...], default_idx: int) -> int:
            for idx, col_name in enumerate(header_cols):
                if any(col_name.startswith(p) for p in prefixes):
                    return idx
            return default_idx

        ra_idx = _find_col(("r.a.", "ra"), 3)
        dec_idx = _find_col(("dec",), 4)
        mag_idx = _find_col(("apmag", "t-mag", "v-mag"), 5)
        delta_idx = _find_col(("delta",), 7)

        # Extract target full name from Horizons header if user didn't supply a custom one
        resolved_title = object_name.strip()
        m_title = re.search(r"Target body name:\s*([^\r\n{]+)", raw_text)
        if m_title and (not resolved_title or resolved_title == base_cmd):
            resolved_title = m_title.group(1).strip()

        ephem_segment = raw_text[soe_idx + 5 : eoe_idx].strip()
        points = []

        for row in csv.reader(ephem_segment.splitlines()):
            if not row or len(row) <= max(ra_idx, dec_idx):
                continue
            t_str = row[0].strip()
            ra_str = row[ra_idx].strip()
            dec_str = row[dec_idx].strip()
            if not t_str or not ra_str or not dec_str:
                continue

            try:
                dt = datetime.strptime(t_str, "%Y-%b-%d %H:%M").replace(
                    tzinfo=timezone.utc
                )
                ra_deg = round(float(ra_str) % 360.0, 5)
                dec_deg = round(float(dec_str), 5)

                mag_val = None
                if mag_idx < len(row):
                    m_raw = row[mag_idx].strip()
                    if m_raw and m_raw.lower() != "n.a.":
                        try:
                            mag_val = round(float(m_raw), 2)
                        except ValueError:
                            mag_val = None

                dist_val = None
                if delta_idx < len(row):
                    d_raw = row[delta_idx].strip()
                    if d_raw and d_raw.lower() != "n.a.":
                        try:
                            dist_val = round(float(d_raw), 5)
                        except ValueError:
                            dist_val = None

                points.append({
                    "time": dt.isoformat(),
                    "display_time": dt.strftime("%b %d %H:%M UT"),
                    "ra": ra_deg,
                    "dec": dec_deg,
                    "mag": mag_val,
                    "distance_au": dist_val,
                })
            except Exception:
                continue

        if not points:
            raise ValueError("Failed to parse ephemeris points from JPL Horizons response.")

        clean_color = color.strip() if isinstance(color, str) and color.strip() else "#10B981"
        ephem_id = str(uuid.uuid4())[:8]
        display_name = resolved_title or used_cmd

        return {
            "id": ephem_id,
            "name": display_name,
            "command": used_cmd,
            "color": clean_color,
            "visible": True,
            "start_time": points[0]["time"],
            "stop_time": points[-1]["time"],
            "step_hours": step_h,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "points": points,
        }


def auto_stretch_image(image_bytes: bytes) -> bytes:
    """
    Robustly stretches the image using histogram normalization and Gamma correction.
    Ignores black (0) and white (255) pixels during calculation.
    """
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("L")
        arr = np.array(img)

        mask = (arr > 0) & (arr < 255)
        if not np.any(mask):
            return image_bytes

        valid_pixels = arr[mask]
        p_min, p_max = np.percentile(valid_pixels, (0.5, 99.5))

        if p_max <= p_min:
            return image_bytes

        stretched = (arr.astype(np.float32) - p_min) / (p_max - p_min) * 255.0
        stretched = np.clip(stretched, 0, 255)

        valid_stretched = stretched[mask]
        if valid_stretched.size > 0:
            current_mean = valid_stretched.mean()
            TARGET_MEAN = 60.0

            if current_mean > 1.0:
                g_min, g_max = 0.1, 10.0
                best_gamma = 1.0
                norm_pixels = valid_stretched / 255.0

                for _ in range(10):
                    g_mid = (g_min + g_max) / 2
                    temp_mean = np.mean(np.power(norm_pixels, g_mid)) * 255.0

                    if temp_mean > TARGET_MEAN:
                        g_min = g_mid
                    else:
                        g_max = g_mid

                    best_gamma = g_mid

                stretched = 255.0 * np.power(stretched / 255.0, best_gamma)
                stretched = np.clip(stretched, 0, 255)

        stretched = stretched.astype(np.uint8)
        out_img = Image.fromarray(stretched)
        buffer = io.BytesIO()
        out_img.save(buffer, format="JPEG", quality=95)
        return buffer.getvalue()

    except Exception as e:
        print(f"Error during auto-stretch: {e}")
        return image_bytes
