import asyncio
import hashlib
import os
import shutil
from typing import Optional, Tuple, Union
import httpx
from astropy.coordinates import SkyCoord
import astropy.units as u

from .astro_utils import auto_stretch_image
from .config import CACHE_DIR

ALLOWED_IMAGE_SOURCES = {"dss2r", "dss2b", "dss2ir"}


def validate_image_source(source: str) -> str:
    if not isinstance(source, str) or source not in ALLOWED_IMAGE_SOURCES:
        raise ValueError(
            f"Unsupported image survey source '{source}'. Allowed: {sorted(ALLOWED_IMAGE_SOURCES)}"
        )
    return source


def _strip_win_device_prefix(path: str) -> str:
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


def resolve_safe_cache_path(cache_dir: str, *parts: str) -> str:
    """Resolve a path inside cache_dir and prevent directory traversal."""
    if not parts:
        raise ValueError("Cache path parts cannot be empty")
    for part in parts:
        if (
            not part
            or part in (".", "..")
            or "/" in part
            or "\\" in part
            or ":" in part
            or ".." in part
        ):
            raise ValueError(f"Unsafe path segment: {part!r}")

    base_real = _strip_win_device_prefix(os.path.abspath(cache_dir))
    target_real = _strip_win_device_prefix(
        os.path.abspath(os.path.join(base_real, *parts))
    )
    try:
        common = os.path.commonpath([base_real, target_real])
    except ValueError as exc:
        raise ValueError("Path resolves across different drives") from exc

    if os.path.normcase(common) != os.path.normcase(base_real) or os.path.normcase(
        target_real
    ) == os.path.normcase(base_real):
        raise ValueError("Path escapes cache directory")
    return target_real


def get_setup_hash(
    fov_w_deg: float,
    fov_h_deg: float,
    image_padding: float,
    resolution: int,
    source: str,
    override_fov: Optional[float] = None,
) -> str:
    source = validate_image_source(source)
    default_fov = max(max(fov_w_deg, fov_h_deg) * image_padding, 0.25)
    if override_fov is not None and abs(float(override_fov) - default_fov) >= 0.01:
        effective_fov = max(float(override_fov), 0.25)
        return f"fov_custom_{effective_fov:.2f}_r{int(resolution)}_{source}"
    return f"fov_{fov_w_deg:.2f}_{fov_h_deg:.2f}_p{image_padding:.2f}_r{int(resolution)}_{source}"


def get_custom_coord_hash(
    ra: float,
    dec: float,
    fov: float,
    resolution: int,
    source: str,
) -> str:
    source = validate_image_source(source)
    coord_hash_str = (
        f"custom_ra{ra:.4f}_dec{dec:.4f}_fov_{fov:.4f}_res{int(resolution)}_{source}"
    )
    return "custom_" + hashlib.md5(coord_hash_str.encode()).hexdigest()[:12]


def get_cache_info(
    object_name: str, setup_hash: str, cache_dir: str = CACHE_DIR
) -> Tuple[str, str, str]:
    invalid_chars = '<>:"/\\|?*'
    sanitized_name = object_name
    for char in invalid_chars:
        sanitized_name = sanitized_name.replace(char, "")
    sanitized_name = (
        sanitized_name.replace(" ", "_")
        .replace("°", "d")
        .replace("'", "m")
        .replace("..", "_")
    )

    filename = f"{sanitized_name}_{setup_hash}.jpg"
    setup_dir = resolve_safe_cache_path(cache_dir, setup_hash)
    filepath = resolve_safe_cache_path(cache_dir, setup_hash, filename)
    url = f"/cache/{setup_hash}/{filename}"
    return url, filepath, setup_dir


async def download_image(
    ra: float,
    dec: float,
    fov: float,
    object_id: str,
    setup_hash: str,
    resolution: int = 1024,
    source: str = "dss2r",
    timeout: int = 120,
    cache_dir: str = CACHE_DIR,
) -> str:
    source = validate_image_source(source)
    url, filepath, setup_dir = get_cache_info(object_id, setup_hash, cache_dir=cache_dir)
    if os.path.exists(filepath):
        return url

    coords = SkyCoord(ra, dec, unit=(u.deg, u.deg))
    download_fov = max(fov, 0.25)
    base_url = "https://skyview.gsfc.nasa.gov/current/cgi/runquery.pl"
    params = {
        "Survey": source,
        "Position": f"{coords.ra.deg:.5f},{coords.dec.deg:.5f}",
        "Size": f"{download_fov:.4f}",
        "Pixels": str(int(resolution)),
        "Return": "JPG",
    }

    try:
        if not os.path.exists(setup_dir):
            os.makedirs(setup_dir, exist_ok=True)
        print(f"    -> Downloading {object_id} from SkyView...")

        async with httpx.AsyncClient(timeout=timeout, verify=True) as client:
            response = await client.get(base_url, params=params)
            response.raise_for_status()

            content_type = response.headers.get("Content-Type", "")
            if "text" in content_type:
                raise ValueError(f"SkyView returned text/html: {response.text[:100]}")

            stretched_bytes = await asyncio.to_thread(
                auto_stretch_image, response.content
            )

            with open(filepath, "wb") as f:
                f.write(stretched_bytes)
        return url
    except Exception as e:
        print(f"    -> ERROR downloading {object_id}: {e}")
        raise e


def get_cache_status_info(cache_dir: str = CACHE_DIR) -> dict:
    if not os.path.exists(cache_dir):
        return {"size_mb": 0, "count": 0}
    total_size = sum(
        os.path.getsize(os.path.join(r, f))
        for r, _, files in os.walk(cache_dir)
        for f in files
    )
    count = sum(len(files) for _, _, files in os.walk(cache_dir))
    return {"size_mb": round(total_size / (1024 * 1024), 2), "count": count}


def purge_image_cache(cache_dir: str = CACHE_DIR) -> dict:
    if os.path.exists(cache_dir):
        shutil.rmtree(cache_dir)
    os.makedirs(cache_dir, exist_ok=True)
    return {"status": "purged"}
