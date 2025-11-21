from pydantic import BaseModel, Field
from typing import List

# --- FOVRectangle MUST be defined before it is used ---
class FOVRectangle(BaseModel):
    width_percent: float
    height_percent: float

class Telescope(BaseModel):
    focal_length: float = Field(..., gt=0)

class Camera(BaseModel):
    sensor_width: float = Field(..., gt=0)
    sensor_height: float = Field(..., gt=0)

class Location(BaseModel):
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)

class AltitudePoint(BaseModel):
    time: str
    altitude: float

class DeepSkyObject(BaseModel):
    name: str
    ra: str
    dec: str
    catalog: str
    size: str
    image_url: str
    altitude_graph: List[AltitudePoint]
    fov_rectangle: FOVRectangle