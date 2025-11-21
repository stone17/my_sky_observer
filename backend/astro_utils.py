import math
import pandas as pd
from typing import Tuple, List, Dict
import astropy.units as u
from astropy.time import Time
from astropy.coordinates import SkyCoord, EarthLocation, AltAz, get_sun, Angle
from astroplan import Observer
import io
from PIL import Image

from .models import Telescope, Camera, Location, AltitudePoint

class AstroCalculator:
    """Performs astronomical calculations."""

    def calculate_fov(self, telescope: Telescope, camera: Camera) -> Tuple[float, float]:
        """Calculates the field of view in arcminutes."""
        fov_width = (camera.sensor_width / telescope.focal_length) * (180 / math.pi) * 60
        fov_height = (camera.sensor_height / telescope.focal_length) * (180 / math.pi) * 60
        return fov_width, fov_height

    def filter_objects_by_fov(self, objects: pd.DataFrame, fov: Tuple[float, float]) -> pd.DataFrame:
        """Filters a DataFrame of objects to those that fit within the given FOV."""
        min_fov_dim = min(fov[0], fov[1])
        return objects[objects['maj_ax'] < min_fov_dim].copy()

import re

    def get_max_altitude(self, dec: str, latitude: float) -> float:
        """Calculates an object's maximum possible altitude. Very fast."""
        try:
            # FIX: Sanitize the dec string to be more robust
            dec_formatted = re.sub(r"[^\d.\-]", " ", dec).strip()
            dec_angle = Angle(dec_formatted, unit=u.deg)
            max_alt = 90 - abs(latitude - dec_angle.deg)
            return max(0, max_alt)
        except Exception:
            return 0.0

    def get_altitude_graph(self, ra: str, dec: str, location: Location, num_points: int = 48) -> List[AltitudePoint]:
        """Generates altitude data for an object over a 24-hour period. Computationally expensive."""
        # FIX: Sanitize the RA and Dec strings to be more robust
        ra_formatted = re.sub(r"[^\d.\-]", " ", ra).strip()
        dec_formatted = re.sub(r"[^\d.\-]", " ", dec).strip()
        
        observer_location = EarthLocation(lat=location.latitude * u.deg, lon=location.longitude * u.deg)
        target_coords = SkyCoord(ra_formatted, dec_formatted, unit=(u.hourangle, u.deg))
        now = Time.now()
        time_deltas = u.Quantity([i * (24 / num_points) for i in range(num_points + 1)], u.hour)
        times = now + time_deltas
        altaz_frame = AltAz(obstime=times, location=observer_location)
        target_altaz = target_coords.transform_to(altaz_frame)
        return [AltitudePoint(time=t.isot, altitude=round(alt.deg, 2)) for t, alt in zip(times, target_altaz.alt)]

    def calculate_time_above_altitude(self, altitude_points: List[AltitudePoint], min_altitude: float) -> float:
        """Estimates the total hours an object is above a minimum altitude."""
        above_times = [p for p in altitude_points if p.altitude >= min_altitude]
        if len(above_times) < 2:
            return 0.0
        time_step_hours = 24.0 / (len(altitude_points) - 1) if len(altitude_points) > 1 else 0
        return round(len(above_times) * time_step_hours, 1)

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
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("L")
        hist = img.histogram()
        total_pixels = img.width * img.height
        if total_pixels == 0: return image_bytes
        clip_pixels = total_pixels * 0.005
        min_val, max_val = 0, 255
        
        cumulative_count = 0
        for i in range(256):
            cumulative_count += hist[i]
            if cumulative_count >= clip_pixels:
                min_val = i; break
        cumulative_count = 0
        for i in range(255, -1, -1):
            cumulative_count += hist[i]
            if cumulative_count >= clip_pixels:
                max_val = i; break
        
        if max_val <= min_val: return image_bytes
        
        lut = [int(((i - min_val) / (max_val - min_val)) * 255) if min_val < i < max_val else (0 if i <= min_val else 255) for i in range(256)]
        stretched_img = img.point(lut)
        
        buffer = io.BytesIO()
        stretched_img.save(buffer, format="JPEG", quality=90)
        return buffer.getvalue()
    except Exception as e:
        # FIX: Raise the exception instead of returning the bad image data
        # This prevents corrupted files from being saved to the cache.
        print(f"Error during auto-stretch: {e}")
        raise e