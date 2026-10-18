import io
import math
import re
import warnings
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

    def get_observing_session(self, observer: Observer, now: Time) -> Tuple[Time, Time]:
        """
        Determines the observing session (Sunset to Sunrise).
        If currently night, returns (previous_sunset, next_sunrise).
        If currently day, returns (next_sunset, next_sunrise).
        """
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                next_sunrise = observer.sun_rise_time(now, which="next")
                next_sunset = observer.sun_set_time(now, which="next")

                if hasattr(next_sunrise, "masked") and next_sunrise.masked:
                    raise ValueError("Sun does not rise")
                if hasattr(next_sunset, "masked") and next_sunset.masked:
                    raise ValueError("Sun does not set")

                if next_sunrise < next_sunset:
                    start_time = observer.sun_set_time(now, which="previous")
                    end_time = next_sunrise
                else:
                    start_time = next_sunset
                    end_time = observer.sun_rise_time(start_time, which="next")

                start_time = start_time - 0.5 * u.hour
                end_time = end_time + 0.5 * u.hour

                return start_time, end_time
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

    def prepare_session_ephemeris(
        self,
        location: Location,
        start_time: Optional[Time] = None,
        end_time: Optional[Time] = None,
        num_points: int = 60,
        twilight_periods: Optional[Dict[str, List[str]]] = None,
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
    ) -> np.ndarray:
        """
        Vectorized calculation of nightly hours visible.
        ra_array, dec_array: numpy arrays of float degrees.
        """
        try:
            targets = SkyCoord(ra_array, dec_array, unit=(u.deg, u.deg))[:, np.newaxis]
            target_altaz = targets.transform_to(altaz_frame)
            altitudes = target_altaz.alt.deg

            count_above = np.sum(altitudes >= min_altitude, axis=1)
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
    ) -> float:
        """
        Estimates the total hours an object is above a minimum altitude based on the graph points.
        If twilight_periods is provided (and contains 'night'), only counts hours during the night.
        """
        valid_points = []
        night_start = None
        night_end = None

        if twilight_periods and "night" in twilight_periods:
            try:
                night_start = Time(twilight_periods["night"][0])
                night_end = Time(twilight_periods["night"][1])
            except Exception:
                pass

        for p in altitude_points:
            if p.altitude >= min_altitude:
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
        """Calculates twilight periods for the 24 hours starting from base_time (or now)."""
        observer = Observer(
            location=EarthLocation(
                lat=location.latitude * u.deg, lon=location.longitude * u.deg
            )
        )
        now = base_time if base_time else Time.now()
        end_time = now + 24 * u.hour

        periods = {}

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            try:
                sunset = observer.sun_set_time(now, which="nearest")
                if hasattr(sunset, "masked") and sunset.masked:
                    sunset = now

                sunrise = observer.sun_rise_time(sunset, which="next")
                if hasattr(sunrise, "masked") and sunrise.masked:
                    sun_alt = (
                        get_sun(now)
                        .transform_to(AltAz(location=observer.location, obstime=now))
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

                if sunrise < sunset:
                    sunset = observer.sun_set_time(now, which="previous")
                    sunrise = observer.sun_rise_time(sunset, which="next")

                def get_t(func, ref_time, which):
                    try:
                        t = func(ref_time, which=which)
                        if hasattr(t, "masked") and t.masked:
                            return None
                        if hasattr(t, "jd") and np.isnan(t.jd):
                            return None
                        if which == "next" and t > sunrise:
                            return None
                        if which == "previous" and t < sunset:
                            return None
                        return t
                    except Exception:
                        return None

                eve_civil = get_t(observer.twilight_evening_civil, sunset, "next")
                eve_naut = get_t(observer.twilight_evening_nautical, sunset, "next")
                eve_astro = get_t(
                    observer.twilight_evening_astronomical, sunset, "next"
                )

                morn_astro = get_t(
                    observer.twilight_morning_astronomical, sunrise, "previous"
                )
                morn_naut = get_t(
                    observer.twilight_morning_nautical, sunrise, "previous"
                )
                morn_civil = get_t(observer.twilight_morning_civil, sunrise, "previous")

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
                        .transform_to(
                            AltAz(location=observer.location, obstime=midnight)
                        )
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
                    .transform_to(AltAz(location=observer.location, obstime=now))
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
