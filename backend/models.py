from typing import Literal, Union
from pydantic import BaseModel, Field

AllowedImageSource = Literal["dss2r", "dss2b", "dss2ir"]


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
    city_name: str = ""


class AltitudePoint(BaseModel):
    time: str
    altitude: float


class FetchImageRequest(BaseModel):
    ra: Union[float, str]
    dec: Union[float, str]
    fov: float = Field(..., gt=0, le=90)
    resolution: int = Field(default=1024, ge=64, le=4096)
    source: AllowedImageSource = "dss2r"
    timeout: int = Field(default=120, ge=5, le=600)


class NinaFramingRequest(BaseModel):
    ra: Union[float, str]
    dec: Union[float, str]
    rotation: float = 0.0
