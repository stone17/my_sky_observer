import math
import pandas as pd
import re
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

    def get_max_altitude(self, dec: str, latitude: float) -> float:
        """Calculates an object's maximum possible altitude. Very fast."""
        try:
            dec_formatted = re.sub(r"[^\d.\-]", " ", dec).strip()
            dec_angle = Angle(dec_formatted, unit=u.deg)
            max_alt = 90 - abs(latitude - dec_angle.deg)
            return max(0, max_alt)
        except Exception:
            return 0.0

    def get_approx_hours_above(self, dec: str, latitude: float, min_altitude: float) -> float:
        """
        Calculates the approximate duration (in hours) an object is above min_altitude.
        Uses the spherical trig formula for hour angle.
        """
        try:
            dec_formatted = re.sub(r"[^\d.\-]", " ", dec).strip()
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

    def get_altitude_graph(self, ra: str, dec: str, location: Location, num_points: int = 48) -> Dict[str, List[AltitudePoint]]:
        """
        Generates altitude data for an object and the Moon over a 24-hour period.
        Returns dict with 'target' and 'moon' lists.
        """
        ra_formatted = re.sub(r"[^\d.\-]", " ", ra).strip()
        dec_formatted = re.sub(r"[^\d.\-]", " ", dec).strip()
        
        observer_location = EarthLocation(lat=location.latitude * u.deg, lon=location.longitude * u.deg)
        target_coords = SkyCoord(ra_formatted, dec_formatted, unit=(u.hourangle, u.deg))

        now = Time.now()
        time_deltas = u.Quantity([i * (24 / num_points) for i in range(num_points + 1)], u.hour)
        times = now + time_deltas

        altaz_frame = AltAz(obstime=times, location=observer_location)

        # Target Altitudes
        target_altaz = target_coords.transform_to(altaz_frame)
        target_points = [AltitudePoint(time=t.isot, altitude=round(alt.deg, 2)) for t, alt in zip(times, target_altaz.alt)]

        # Moon Altitudes
        try:
            moon_coords = get_body("moon", times, location=observer_location)
            moon_altaz = moon_coords.transform_to(altaz_frame)
            moon_points = [AltitudePoint(time=t.isot, altitude=round(alt.deg, 2)) for t, alt in zip(times, moon_altaz.alt)]
        except Exception as e:
            print(f"Error calculating moon altitude: {e}")
            moon_points = []

        return {"target": target_points, "moon": moon_points}

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

    def get_twilight_periods(self, location: Location) -> Dict[str, List[str]]:
        """Calculates twilight periods for the next 24 hours."""
        observer = Observer(location=EarthLocation(lat=location.latitude*u.deg, lon=location.longitude*u.deg))
        now = Time.now()
        end_time = now + 24*u.hour
        
        periods = {}
        try:
            sunset = observer.sun_set_time(now, which='next')
            sunrise = observer.sun_rise_time(sunset, which='next')
            eve_civil = observer.twilight_evening_civil(now, which='next')
            eve_nautical = observer.twilight_evening_nautical(now, which='next')
            eve_astro = observer.twilight_evening_astronomical(now, which='next')
            morn_astro = observer.twilight_morning_astronomical(sunset, which='next')
            morn_nautical = observer.twilight_morning_nautical(sunset, which='next')
            morn_civil = observer.twilight_morning_civil(sunset, which='next')
            
            periods["day"] = [now.isot, sunset.isot] if sunset > now and sunset < end_time else None
            if sunrise < end_time: periods["day_morn"] = [sunrise.isot, end_time.isot]
            periods["civil"] = [sunset.isot, eve_civil.isot]
            periods["nautical"] = [eve_civil.isot, eve_nautical.isot]
            periods["astronomical"] = [eve_nautical.isot, eve_astro.isot]
            periods["night"] = [eve_astro.isot, morn_astro.isot]
            periods["astronomical_morn"] = [morn_astro.isot, morn_nautical.isot]
            periods["nautical_morn"] = [morn_nautical.isot, morn_civil.isot]
            periods["civil_morn"] = [morn_civil.isot, sunrise.isot]

            return {k: v for k, v in periods.items() if v is not None and Time(v[1]) > Time(v[0])}
        except Exception:
            sun_alt = get_sun(now).transform_to(AltAz(location=observer.location, obstime=now)).alt
            return {"day": [now.isot, end_time.isot]} if sun_alt > -18*u.deg else {"night": [now.isot, end_time.isot]}

def auto_stretch_image(image_bytes: bytes) -> bytes:
    """
    Robustly stretches the image using histogram normalization and Gamma correction.
    Ignores black (0) and white (255) pixels during calculation.
    Target mean brightness is ~85 (1/3 of 255).
    """
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("L")
        arr = np.array(img)
        
        # Mask out 0 (black) and 255 (white) to ignore borders/artifacts/saturated stars
        mask = (arr > 3) & (arr < 250)
        
        if not np.any(mask):
            # print("  -> Warning: No valid pixels found for stretching. Returning original.")
            return image_bytes 
            
        valid_pixels = arr[mask]
        
        # Calculate percentiles on valid pixels only
        p_min, p_max = np.percentile(valid_pixels, (0.1, 99.9)) 
        
        if p_max <= p_min:
            return image_bytes
            
        # 1. Linear Stretch
        stretched = (arr.astype(np.float32) - p_min) / (p_max - p_min) * 255.0
        stretched = np.clip(stretched, 0, 255)
        
        # 2. Gamma Correction to Target Mean
        # Target mean ~ 64 (1/4 of 255) - User requested 1/4 (approx 60-65)
        TARGET_MEAN = 64.0
        current_mean = stretched.mean()
        
        if current_mean > 1.0:
            norm_mean = current_mean / 255.0
            norm_target = TARGET_MEAN / 255.0
            
            # gamma = log(target) / log(current)
            gamma = math.log(norm_target) / math.log(norm_mean)
            
            # Apply Gamma: pixel' = 255 * (pixel/255)^gamma
            stretched = 255.0 * np.power(stretched / 255.0, gamma)
            stretched = np.clip(stretched, 0, 255)
        
        stretched = stretched.astype(np.uint8)
        
        out_img = Image.fromarray(stretched)
        buffer = io.BytesIO()
        out_img.save(buffer, format="JPEG", quality=95)
        return buffer.getvalue()
        
    except Exception as e:
        print(f"Error during auto-stretch: {e}")
        return image_bytes
