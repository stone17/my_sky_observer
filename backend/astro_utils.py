import math
import pandas as pd
import re
import warnings
from typing import Tuple, List, Dict, Optional
import astropy.units as u
from astropy.time import Time
from astropy.coordinates import SkyCoord, EarthLocation, AltAz, get_sun, get_body, Angle
from astroplan import Observer
import io
from PIL import Image, ImageOps
import numpy as np

from .models import Telescope, Camera, Location, AltitudePoint

class AstroCalculator:
    """Performs astronomical calculations."""

    def calculate_fov(self, telescope: Telescope, camera: Camera) -> Tuple[float, float]:
        """Calculates the field of view in arcminutes using exact formula."""
        # 2 * atan(sensor / (2 * focal_length))
        fov_width_rad = 2 * math.atan(camera.sensor_width / (2 * telescope.focal_length))
        fov_height_rad = 2 * math.atan(camera.sensor_height / (2 * telescope.focal_length))
        
        fov_width = fov_width_rad * (180 / math.pi) * 60
        fov_height = fov_height_rad * (180 / math.pi) * 60
        return fov_width, fov_height

    def filter_objects_by_fov(self, objects: pd.DataFrame, fov: Tuple[float, float]) -> pd.DataFrame:
        """Filters a DataFrame of objects to those that fit within the given FOV."""
        min_fov_dim = min(fov[0], fov[1])
        return objects[objects['maj_ax'] < min_fov_dim].copy()

    def get_observing_session(self, observer: Observer, now: Time) -> Tuple[Time, Time]:
        """
        Determines the observing session (Sunset to Sunrise).
        If currently night, returns (previous_sunset, next_sunrise).
        If currently day, returns (next_sunset, next_sunrise).
        """
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                next_sunrise = observer.sun_rise_time(now, which='next')
                next_sunset = observer.sun_set_time(now, which='next')

                if hasattr(next_sunrise, 'masked') and next_sunrise.masked:
                    raise ValueError("Sun does not rise")
                if hasattr(next_sunset, 'masked') and next_sunset.masked:
                    raise ValueError("Sun does not set")

                if next_sunrise < next_sunset:
                    # We are currently in the night (or early morning)
                    start_time = observer.sun_set_time(now, which='previous')
                    end_time = next_sunrise
                else:
                    # We are in the day, preparing for tonight
                    start_time = next_sunset
                    end_time = observer.sun_rise_time(start_time, which='next')
                    
                # Add a small buffer (e.g. +/- 30 mins) to show context
                start_time = start_time - 0.5 * u.hour
                end_time = end_time + 0.5 * u.hour
                
                return start_time, end_time
        except Exception as e:
            # If sun doesn't set/rise, just return a 24h window centered on now
            return now - 12 * u.hour, now + 12 * u.hour

    def get_max_altitude(self, dec: float, latitude: float) -> float:
        """Calculates an object's maximum possible altitude. Very fast."""
        try:
            if isinstance(dec, (float, int)):
                dec_deg = float(dec)
            else:
                dec_formatted = re.sub(r"[^\d.\-]", " ", str(dec)).strip()
                dec_deg = Angle(dec_formatted, unit=u.deg).deg
                
            max_alt = 90 - abs(latitude - dec_deg)
            return max(0, max_alt)
        except Exception:
            return 0.0

    def get_approx_hours_above(self, dec: float, latitude: float, min_altitude: float) -> float:
        """
        Calculates the approximate duration (in hours) an object is above min_altitude.
        Uses the spherical trig formula for hour angle.
        """
        try:
            if isinstance(dec, (float, int)):
                dec_deg = float(dec)
            else:
                dec_formatted = re.sub(r"[^\d.\-]", " ", str(dec)).strip()
                dec_deg = Angle(dec_formatted, unit=u.deg).deg

            lat_rad = math.radians(latitude)
            dec_rad = math.radians(dec_deg)
            min_alt_rad = math.radians(min_altitude)

            # cos(H) = (sin(h) - sin(phi)sin(delta)) / (cos(phi)cos(delta))
            numerator = math.sin(min_alt_rad) - math.sin(lat_rad) * math.sin(dec_rad)
            denominator = math.cos(lat_rad) * math.cos(dec_rad)

            if denominator == 0: return 0.0 # Pole?

            cos_h = numerator / denominator

            if cos_h >= 1.0: return 0.0 # Never rises above min_alt
            if cos_h <= -1.0: return 24.0 # Circumpolar (always above)

            h_rad = math.acos(cos_h)
            h_deg = math.degrees(h_rad)

            # Total time is 2 * H (converted to hours)
            return (2 * h_deg) / 15.0
        except Exception:
            return 0.0

    def batch_get_max_altitude(self, dec_array: np.ndarray, latitude: float) -> np.ndarray:
        """
        Vectorized calculation of max altitude for an array of declinations.
        """
        try:
            # max_alt = 90 - abs(lat - dec)
            # We assume dec_array is already float degrees
            return np.maximum(0, 90 - np.abs(latitude - dec_array))
        except Exception as e:
            print(f"Error in batch_get_max_altitude: {e}")
            return np.zeros_like(dec_array)

    def get_altitude_graph(self, ra: str, dec: str, location: Location, num_points: int = 60, start_time: Optional[Time] = None, end_time: Optional[Time] = None) -> Dict[str, List[AltitudePoint]]:
        """
        Generates altitude data for an object and the Moon.
        If start_time/end_time are not provided, calculates them for the current observing session.
        """
        observer_location = EarthLocation(lat=location.latitude * u.deg, lon=location.longitude * u.deg)
        observer = Observer(location=observer_location)
        
        if isinstance(ra, (float, int)) and isinstance(dec, (float, int)):
             target_coords = SkyCoord(ra, dec, unit=(u.deg, u.deg))
        else:
             ra_formatted = re.sub(r"[^\d.\-]", " ", str(ra)).strip()
             dec_formatted = re.sub(r"[^\d.\-]", " ", str(dec)).strip()
             target_coords = SkyCoord(ra_formatted, dec_formatted, unit=(u.hourangle, u.deg))

        if start_time is None or end_time is None:
            start_time, end_time = self.get_observing_session(observer, Time.now())

        duration = (end_time - start_time).to(u.hour).value
        # Ensure at least some duration
        if duration < 1: duration = 24.0

        # Generate time points
        times = start_time + np.linspace(0, duration, num_points) * u.hour

        altaz_frame = AltAz(obstime=times, location=observer_location)

        # Target Altitudes
        target_altaz = target_coords.transform_to(altaz_frame)
        target_points = [AltitudePoint(time=t.isot + 'Z', altitude=round(alt.deg, 2)) for t, alt in zip(times, target_altaz.alt)]

        # Moon Altitudes
        try:
            moon_coords = get_body("moon", times, location=observer_location)
            moon_altaz = moon_coords.transform_to(altaz_frame)
            moon_points = [AltitudePoint(time=t.isot + 'Z', altitude=round(alt.deg, 2)) for t, alt in zip(times, moon_altaz.alt)]
        except Exception as e:
            print(f"Error calculating moon altitude: {e}")
            moon_points = []

        # Sun Altitudes
        try:
            sun_coords = get_sun(times)
            sun_altaz = sun_coords.transform_to(altaz_frame)
            sun_points = [AltitudePoint(time=t.isot + 'Z', altitude=round(alt.deg, 2)) for t, alt in zip(times, sun_altaz.alt)]
        except Exception as e:
            print(f"Error calculating sun altitude: {e}")
            sun_points = []

        return {"target": target_points, "moon": moon_points, "sun": sun_points}


    def prepare_night_frame(self, location: Location, night_start: Time, night_end: Time) -> Optional[Tuple[AltAz, float]]:
        """
        Pre-calculates the AltAz frame for the night period to speed up batch calculations.
        Returns (AltAz_Frame, duration_in_hours).
        """
        try:
            duration = (night_end - night_start).to(u.hour).value
            if duration <= 0: return None

            num_points = int(duration * 4) # 4 points per hour
            if num_points < 2: num_points = 2

            times = night_start + np.linspace(0, duration, num_points) * u.hour
            observer_location = EarthLocation(lat=location.latitude * u.deg, lon=location.longitude * u.deg)
            altaz_frame = AltAz(obstime=times, location=observer_location)

            return altaz_frame, duration
        except Exception:
            return None

    def calculate_nightly_hours_fast(self, ra: str, dec: str, altaz_frame: AltAz, duration: float, min_altitude: float) -> float:
        """
        Calculates hours above altitude using a pre-calculated AltAz frame.
        """
        try:
            # Handle potentially different input types
            if isinstance(ra, (float, int)) and isinstance(dec, (float, int)):
                 target_coords = SkyCoord(ra, dec, unit=(u.deg, u.deg))
            else:
                ra_str = str(ra).strip()
                dec_str = str(dec).strip()

                # Check if RA looks like degrees (float string) or HMS
                if re.match(r'^[\d.]+$', ra_str):
                    unit = (u.deg, u.deg)
                else:
                    unit = (u.hourangle, u.deg)
                target_coords = SkyCoord(ra_str, dec_str, unit=unit)

            # This is the heavy part, but frame is reused
            altitudes = target_coords.transform_to(altaz_frame).alt.deg

            count_above = np.sum(altitudes >= min_altitude)
            hours = (count_above / len(altitudes)) * duration
            return round(hours, 1)

        except Exception as e:
            # print(f"Error in nightly calc: {e}")
            return 0.0

    def batch_calculate_nightly_hours(self, ra_array: np.ndarray, dec_array: np.ndarray, altaz_frame: AltAz, duration: float, min_altitude: float) -> np.ndarray:
        """
        Vectorized calculation of nightly hours visible.
        ra_array, dec_array: numpy arrays of float degrees.
        """
        try:
            # Create a single SkyCoord object with all targets
            # Reshape to (N, 1) to broadcast against (M,) time points
            targets = SkyCoord(ra_array, dec_array, unit=(u.deg, u.deg))[:, np.newaxis]
            
            # Transform all targets to AltAz frame
            # Result shape: (N_targets, N_times)
            target_altaz = targets.transform_to(altaz_frame)
            
            # Get altitudes in degrees
            altitudes = target_altaz.alt.deg
            
            # Count how many time points are above min_altitude for each target
            # axis=1 aggregates over time
            count_above = np.sum(altitudes >= min_altitude, axis=1)
            
            # Calculate hours
            # altitudes shape is (N_targets, N_times)
            num_times = altitudes.shape[1]
            if num_times == 0:
                return np.zeros_like(ra_array)
                
            hours = (count_above / num_times) * duration
            return np.round(hours, 1)
            
        except Exception as e:
            print(f"Error in batch_calculate_nightly_hours: {e}")
            return np.zeros_like(ra_array)

    def calculate_time_above_altitude(self, altitude_points: List[AltitudePoint], min_altitude: float, twilight_periods: Optional[Dict[str, List[str]]] = None) -> float:
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
            except: pass

        for p in altitude_points:
            if p.altitude >= min_altitude:
                if night_start and night_end:
                    # Check if point time is within night period
                    # Note: p.time is ISOT string.
                    pt = Time(p.time)
                    if pt >= night_start and pt <= night_end:
                        valid_points.append(p)
                else:
                    valid_points.append(p)

        if len(valid_points) < 1: # Single point counts as some time if step is known, but let's be safe
            return 0.0

        # Time step is constant
        time_step_hours = 24.0 / (len(altitude_points) - 1) if len(altitude_points) > 1 else 0

        return round(len(valid_points) * time_step_hours, 1)

    def get_twilight_periods(self, location: Location, base_time: Optional[Time] = None) -> Dict[str, List[str]]:
        """Calculates twilight periods for the 24 hours starting from base_time (or now)."""
        observer = Observer(location=EarthLocation(lat=location.latitude*u.deg, lon=location.longitude*u.deg))
        now = base_time if base_time else Time.now()
        end_time = now + 24*u.hour
        
        periods = {}
        
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            try:
                # 1. Find Sunset and Sunrise
                sunset = observer.sun_set_time(now, which='nearest')
                if hasattr(sunset, 'masked') and sunset.masked:
                    sunset = now
                
                sunrise = observer.sun_rise_time(sunset, which='next')
                if hasattr(sunrise, 'masked') and sunrise.masked:
                    sun_alt = get_sun(now).transform_to(AltAz(location=observer.location, obstime=now)).alt.deg
                    if sun_alt > 0: return {"day": [now.isot + 'Z', end_time.isot + 'Z']}
                    elif sun_alt > -6: return {"civil": [now.isot + 'Z', end_time.isot + 'Z']}
                    elif sun_alt > -12: return {"nautical": [now.isot + 'Z', end_time.isot + 'Z']}
                    elif sun_alt > -18: return {"astronomical": [now.isot + 'Z', end_time.isot + 'Z']}
                    else: return {"night": [now.isot + 'Z', end_time.isot + 'Z']}

                if sunrise < sunset:
                    sunset = observer.sun_set_time(now, which='previous')
                    sunrise = observer.sun_rise_time(sunset, which='next')

                # 2. Calculate Twilights using 'next' from sunset and 'previous' from sunrise
                def get_t(func, ref_time, which):
                    try:
                        t = func(ref_time, which=which)
                        if hasattr(t, 'masked') and t.masked: return None
                        if hasattr(t, 'jd') and np.isnan(t.jd): return None
                        # Ensure the twilight event actually happens within the current night bounds
                        if which == 'next' and t > sunrise: return None
                        if which == 'previous' and t < sunset: return None
                        return t
                    except Exception:
                        return None

                eve_civil = get_t(observer.twilight_evening_civil, sunset, 'next')
                eve_naut = get_t(observer.twilight_evening_nautical, sunset, 'next')
                eve_astro = get_t(observer.twilight_evening_astronomical, sunset, 'next')

                morn_astro = get_t(observer.twilight_morning_astronomical, sunrise, 'previous')
                morn_naut = get_t(observer.twilight_morning_nautical, sunrise, 'previous')
                morn_civil = get_t(observer.twilight_morning_civil, sunrise, 'previous')

                # 3. Build periods
                def add_p(name, start, end):
                    if start and end and start < end:
                        periods[name] = [start.isot + 'Z', end.isot + 'Z']

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
                    # Determine the darkest phase reached
                    midnight = t1 + (t2 - t1) / 2
                    midnight_alt = get_sun(midnight).transform_to(AltAz(location=observer.location, obstime=midnight)).alt.deg
                    
                    if midnight_alt >= -6: middle_phase = "civil"
                    elif midnight_alt >= -12: middle_phase = "nautical"
                    elif midnight_alt >= -18: middle_phase = "astronomical"
                    else: middle_phase = "night"
                        
                    add_p(middle_phase, t1, t2)

                print(f"\n--- Twilight Periods Calculated for Lat: {location.latitude}, Lon: {location.longitude} ---")
                for k, v in periods.items():
                    print(f"{k}: {v[0]} -> {v[1]}")
                print("-------------------------------------------------------------------\n")

                return periods
            except Exception as e:
                print(f"Twilight calculation error: {e}")
                sun_alt = get_sun(now).transform_to(AltAz(location=observer.location, obstime=now)).alt.deg
                return {"day": [now.isot + 'Z', end_time.isot + 'Z']} if sun_alt > -18 else {"night": [now.isot + 'Z', end_time.isot + 'Z']}

def auto_stretch_image(image_bytes: bytes) -> bytes:
    """
    Robustly stretches the image using histogram normalization and Gamma correction.
    Ignores black (0) and white (255) pixels during calculation.
    Target mean brightness is ~85 (1/3 of 255).
    """
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("L")
        arr = np.array(img)
        
        # Mask out exactly 0 (black) and 255 (white) to ignore borders and saturation
        mask = (arr > 0) & (arr < 255)
        
        if not np.any(mask):
            return image_bytes 
            
        valid_pixels = arr[mask]
        
        # Calculate percentiles on valid pixels only
        p_min, p_max = np.percentile(valid_pixels, (0.5, 99.5))
        
        if p_max <= p_min:
            return image_bytes
            
        # 1. Linear Stretch
        stretched = (arr.astype(np.float32) - p_min) / (p_max - p_min) * 255.0
        stretched = np.clip(stretched, 0, 255)
        
        # 2. Gamma Correction
        # Calculate mean only on valid pixels to avoid borders skewing gamma
        valid_stretched = stretched[mask]
        if valid_stretched.size > 0:
            current_mean = valid_stretched.mean()
            TARGET_MEAN = 60.0
            
            if current_mean > 1.0:
                # Binary search for gamma to target the mean robustly
                # Jensen's inequality prevents analytic solution from being accurate on skewed distributions
                g_min, g_max = 0.1, 10.0
                best_gamma = 1.0
                
                # Normalize pixels once for speed
                norm_pixels = valid_stretched / 255.0
                
                for _ in range(10): 
                    g_mid = (g_min + g_max) / 2
                    # Calculate mean with this gamma
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
