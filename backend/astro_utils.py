import io
import math
import re
import warnings
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import astropy.units as u
import numpy as np
from astroplan import Observer
from astropy.coordinates import AltAz, EarthLocation, SkyCoord, get_body, get_sun
from astropy.time import Time
from PIL import Image

from .models import AltitudePoint, Camera, Location, Telescope


class AstroCalculator:
    """Performs astronomical calculations."""

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
