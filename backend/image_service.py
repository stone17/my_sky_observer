import asyncio
import hashlib
import json
import os
import re
import shutil
from typing import Dict, List, Optional, Tuple, Union
import httpx
from astropy.coordinates import SkyCoord
import astropy.units as u

from .astro_utils import auto_stretch_image
from .config import CACHE_DIR

ALLOWED_IMAGE_SOURCES = {"dss2r", "dss2b", "dss2ir"}

# Limit concurrent SkyView CGI queries to avoid triggering NASA's "Too Many Processes" limit
_SKYVIEW_SEMAPHORE: Optional[asyncio.Semaphore] = None
_SKYVIEW_SEMAPHORE_LOOP: Optional[asyncio.AbstractEventLoop] = None
_INFLIGHT_DOWNLOADS: dict[str, asyncio.Future] = {}


def _get_skyview_semaphore() -> asyncio.Semaphore:
    global _SKYVIEW_SEMAPHORE, _SKYVIEW_SEMAPHORE_LOOP
    loop = asyncio.get_running_loop()
    if _SKYVIEW_SEMAPHORE is None or _SKYVIEW_SEMAPHORE_LOOP is not loop:
        _SKYVIEW_SEMAPHORE = asyncio.Semaphore(2)
        _SKYVIEW_SEMAPHORE_LOOP = loop
        _INFLIGHT_DOWNLOADS.clear()
    return _SKYVIEW_SEMAPHORE


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


def sanitize_cache_object_name(object_name: str) -> str:
    invalid_chars = '<>:"/\\|?*'
    sanitized_name = str(object_name)
    for char in invalid_chars:
        sanitized_name = sanitized_name.replace(char, "")
    return (
        sanitized_name.replace(" ", "_")
        .replace("°", "d")
        .replace("'", "m")
        .replace("..", "_")
    )


def get_cache_info(
    object_name: str, setup_hash: str, cache_dir: str = CACHE_DIR
) -> Tuple[str, str, str]:
    sanitized_name = sanitize_cache_object_name(object_name)

    filename = f"{sanitized_name}_{setup_hash}.jpg"
    setup_dir = resolve_safe_cache_path(cache_dir, setup_hash)
    filepath = resolve_safe_cache_path(cache_dir, setup_hash, filename)
    url = f"/cache/{setup_hash}/{filename}"
    return url, filepath, setup_dir


async def _perform_skyview_download(
    ra: float,
    dec: float,
    fov: float,
    object_id: str,
    url: str,
    filepath: str,
    setup_dir: str,
    resolution: int,
    source: str,
    timeout: int,
) -> str:
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

    semaphore = _get_skyview_semaphore()
    max_attempts = 4
    base_delay = 1.5

    async with semaphore:
        if os.path.exists(filepath):
            return url

        if not os.path.exists(setup_dir):
            os.makedirs(setup_dir, exist_ok=True)

        last_err: Optional[Exception] = None
        async with httpx.AsyncClient(timeout=timeout, verify=True) as client:
            for attempt in range(1, max_attempts + 1):
                try:
                    if attempt == 1:
                        print(f"    -> Downloading {object_id} from SkyView...")
                    else:
                        print(
                            f"    -> Retrying {object_id} from SkyView (attempt {attempt}/{max_attempts})..."
                        )

                    response = await client.get(base_url, params=params)
                    if response.status_code in (429, 502, 503, 504):
                        raise RuntimeError(
                            f"SkyView HTTP {response.status_code} (transient server overload)"
                        )
                    response.raise_for_status()

                    content_type = response.headers.get("Content-Type", "")
                    if "text" in content_type:
                        body_snippet = response.text[:150]
                        if (
                            "Too Many Processes" in body_snippet
                            or "busy" in body_snippet.lower()
                        ) and attempt < max_attempts:
                            delay = base_delay * (2 ** (attempt - 1))
                            print(
                                f"    -> SkyView busy ('Too Many Processes') for {object_id}, waiting {delay:.1f}s..."
                            )
                            await asyncio.sleep(delay)
                            continue
                        raise ValueError(
                            f"SkyView returned text/html: {response.text[:100]}"
                        )

                    stretched_bytes = await asyncio.to_thread(
                        auto_stretch_image, response.content
                    )

                    with open(filepath, "wb") as f:
                        f.write(stretched_bytes)
                    return url

                except (httpx.TransportError, RuntimeError) as transient_err:
                    last_err = transient_err
                    if attempt < max_attempts:
                        delay = base_delay * (2 ** (attempt - 1))
                        await asyncio.sleep(delay)
                        continue
                    print(f"    -> ERROR downloading {object_id}: {transient_err}")
                    raise transient_err
                except Exception as e:
                    print(f"    -> ERROR downloading {object_id}: {e}")
                    raise e

        if last_err:
            raise last_err
        raise RuntimeError(f"Failed to download {object_id} from SkyView")


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

    _get_skyview_semaphore()
    existing_task = _INFLIGHT_DOWNLOADS.get(filepath)
    if existing_task is not None and not existing_task.done():
        return await asyncio.shield(existing_task)

    task = asyncio.create_task(
        _perform_skyview_download(
            ra=ra,
            dec=dec,
            fov=fov,
            object_id=object_id,
            url=url,
            filepath=filepath,
            setup_dir=setup_dir,
            resolution=resolution,
            source=source,
            timeout=timeout,
        )
    )
    _INFLIGHT_DOWNLOADS[filepath] = task
    try:
        return await asyncio.shield(task)
    finally:
        if _INFLIGHT_DOWNLOADS.get(filepath) is task and task.done():
            _INFLIGHT_DOWNLOADS.pop(filepath, None)


def _is_cached_image_file(filename: str) -> bool:
    lower = filename.lower()
    return lower.endswith((".jpg", ".jpeg", ".png", ".webp"))


def get_cache_status_info(
    cache_dir: str = CACHE_DIR, setup_hash: Optional[str] = None
) -> dict:
    if not os.path.exists(cache_dir):
        return {
            "size_mb": 0,
            "count": 0,
            "setup_count": 0,
            "setup_size_mb": 0,
            "setup_hash": setup_hash or "",
        }
    total_size = 0
    count = 0
    for r, dirs, files in os.walk(cache_dir):
        dirs[:] = [d for d in dirs if d != "_sky_atlas"]
        for f in files:
            if _is_cached_image_file(f):
                total_size += os.path.getsize(os.path.join(r, f))
                count += 1

    setup_count = 0
    setup_size = 0
    if setup_hash:
        try:
            setup_dir = resolve_safe_cache_path(cache_dir, setup_hash)
            if os.path.isdir(setup_dir):
                for f in os.listdir(setup_dir):
                    fp = os.path.join(setup_dir, f)
                    if os.path.isfile(fp) and _is_cached_image_file(f):
                        setup_count += 1
                        setup_size += os.path.getsize(fp)
        except ValueError:
            pass

    return {
        "size_mb": round(total_size / (1024 * 1024), 2),
        "count": count,
        "setup_count": setup_count,
        "setup_size_mb": round(setup_size / (1024 * 1024), 2),
        "setup_hash": setup_hash or "",
    }


def purge_image_cache(cache_dir: str = CACHE_DIR, include_atlas: bool = False) -> dict:
    if os.path.exists(cache_dir):
        if include_atlas:
            shutil.rmtree(cache_dir)
        else:
            for entry in os.listdir(cache_dir):
                if entry == "_sky_atlas":
                    continue
                try:
                    target = resolve_safe_cache_path(cache_dir, entry)
                except ValueError:
                    continue
                if os.path.isdir(target):
                    shutil.rmtree(target)
                elif os.path.isfile(target):
                    os.remove(target)
    os.makedirs(cache_dir, exist_ok=True)
    return {"status": "purged"}


_SETUP_FOV_RE = re.compile(
    r"^fov_([0-9]+(?:\.[0-9]+)?)_([0-9]+(?:\.[0-9]+)?)_p([0-9]+(?:\.[0-9]+)?)_r(\d+)_([a-z0-9]+)$"
)
_CUSTOM_FOV_RE = re.compile(
    r"^fov_custom_([0-9]+(?:\.[0-9]+)?)_r(\d+)_([a-z0-9]+)$"
)
_RADEC_PREFIX_RE = re.compile(
    r"^RADEC_(-?[0-9]+(?:\.[0-9]+)?)_(-?[0-9]+(?:\.[0-9]+)?)$"
)
_SAN_CATALOG_LOOKUP: Optional[Dict[str, dict]] = None


def parse_setup_hash_fov(setup_hash: str) -> Tuple[Optional[float], int]:
    """Parse (download_fov_deg, resolution_px) from a standard setup_hash folder name."""
    m = _SETUP_FOV_RE.match(setup_hash)
    if m:
        fov_w = float(m.group(1))
        fov_h = float(m.group(2))
        padding = float(m.group(3))
        res = int(m.group(4))
        return max(max(fov_w, fov_h) * padding, 0.25), res
    m2 = _CUSTOM_FOV_RE.match(setup_hash)
    if m2:
        eff = float(m2.group(1))
        res = int(m2.group(2))
        return max(eff, 0.25), res
    return None, 1024


def save_custom_image_meta(
    setup_hash: str,
    ra: float,
    dec: float,
    fov: float,
    resolution: int,
    source: str,
    cache_dir: str = CACHE_DIR,
) -> None:
    """Write sidecar metadata for a custom-coordinate sky survey image."""
    try:
        setup_dir = resolve_safe_cache_path(cache_dir, setup_hash)
        os.makedirs(setup_dir, exist_ok=True)
        meta_path = resolve_safe_cache_path(cache_dir, setup_hash, "meta.json")
        payload = {
            "ra": round(float(ra), 5),
            "dec": round(float(dec), 5),
            "fov": round(max(float(fov), 0.25), 4),
            "resolution": int(resolution),
            "source": str(source),
        }
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except Exception:
        pass


def _get_sanitized_catalog_lookup() -> Dict[str, dict]:
    """Build and cache a fast lookup from sanitized catalog object name -> metadata."""
    global _SAN_CATALOG_LOOKUP
    if _SAN_CATALOG_LOOKUP is not None:
        return _SAN_CATALOG_LOOKUP

    from .data_manager import CATALOG_PRIORITY, CatalogManager

    lookup: Dict[str, dict] = {}
    avail = CatalogManager.get_available_catalogs()
    ordered = sorted(avail, key=lambda c: (CATALOG_PRIORITY.get(c.upper(), 99), c))
    for cat in ordered:
        try:
            df = CatalogManager.get_catalog(cat)
        except Exception:
            continue
        for row in df.to_dict("records"):
            obj_id = str(row.get("id", "")).strip()
            if not obj_id:
                continue
            san = sanitize_cache_object_name(obj_id)
            if san in lookup:
                continue
            try:
                ra = float(row.get("ra"))
                dec = float(row.get("dec"))
            except (TypeError, ValueError):
                continue
            raw_common = row.get("name", "N/A")
            common_name = (
                ""
                if raw_common is None or str(raw_common).strip() in ("", "N/A", "nan")
                else str(raw_common).strip()
            )
            lookup[san] = {
                "name": obj_id,
                "common_name": common_name,
                "ra": round(ra, 5),
                "dec": round(dec, 5),
                "type": str(row.get("type", "") or ""),
            }

    _SAN_CATALOG_LOOKUP = lookup
    return lookup


def list_cached_sky_images(
    cache_dir: str = CACHE_DIR,
    settings: Optional[dict] = None,
    active_setup_hash: Optional[str] = None,
    fallback_fov: float = 1.0,
) -> List[dict]:
    """Scan cache_dir and return all cached sky survey images with celestial RA/Dec/FOV."""
    if not os.path.isdir(cache_dir):
        return []

    catalog_lookup = _get_sanitized_catalog_lookup()

    # Build lookup for custom_framed entries saved in client_settings.object_settings
    url_meta_lookup: Dict[str, dict] = {}
    if isinstance(settings, dict):
        obj_settings = (
            (settings.get("client_settings") or {}).get("object_settings") or {}
        )
        if isinstance(obj_settings, dict):
            for obj_name, cfg in obj_settings.items():
                if not isinstance(cfg, dict):
                    continue
                raw_url = str(cfg.get("image_url") or "").split("?")[0]
                if raw_url.startswith("/cache/"):
                    url_meta_lookup[raw_url] = {
                        "name": str(obj_name),
                        "ra": cfg.get("center_ra"),
                        "dec": cfg.get("center_dec"),
                        "fov": cfg.get("fov"),
                    }

    dedup: Dict[Tuple[str, float], dict] = {}

    for entry in sorted(os.listdir(cache_dir)):
        if entry == "_sky_atlas":
            continue
        try:
            setup_dir = resolve_safe_cache_path(cache_dir, entry)
        except ValueError:
            continue
        if not os.path.isdir(setup_dir):
            continue

        setup_fov, setup_res = parse_setup_hash_fov(entry)
        sidecar_meta: Optional[dict] = None
        if setup_fov is None and entry.startswith("custom_"):
            try:
                meta_path = resolve_safe_cache_path(cache_dir, entry, "meta.json")
                if os.path.isfile(meta_path):
                    with open(meta_path, "r", encoding="utf-8") as mf:
                        sidecar_meta = json.load(mf)
            except Exception:
                sidecar_meta = None

        suffix = f"_{entry}.jpg"
        for fname in os.listdir(setup_dir):
            if not _is_cached_image_file(fname):
                continue
            try:
                fpath = resolve_safe_cache_path(cache_dir, entry, fname)
            except ValueError:
                continue
            if not os.path.isfile(fpath):
                continue

            url = f"/cache/{entry}/{fname}"
            if fname.endswith(suffix):
                san_prefix = fname[: -len(suffix)]
            else:
                san_prefix = os.path.splitext(fname)[0]

            ra: Optional[float] = None
            dec: Optional[float] = None
            fov: Optional[float] = setup_fov
            res_px: int = setup_res
            obj_name = san_prefix.replace("_", " ")
            common_name = ""
            obj_type = ""
            is_custom = False

            cat_entry = catalog_lookup.get(san_prefix)
            if cat_entry is not None:
                obj_name = cat_entry["name"]
                common_name = cat_entry["common_name"]
                obj_type = cat_entry["type"]
                ra = cat_entry["ra"]
                dec = cat_entry["dec"]
            else:
                is_custom = True
                if sidecar_meta:
                    try:
                        ra = float(sidecar_meta["ra"])
                        dec = float(sidecar_meta["dec"])
                        fov = float(sidecar_meta["fov"])
                        res_px = int(sidecar_meta.get("resolution", 1024))
                    except (KeyError, TypeError, ValueError):
                        pass

                saved_cfg = url_meta_lookup.get(url)
                if saved_cfg:
                    if saved_cfg.get("name"):
                        obj_name = saved_cfg["name"]
                        cat_by_saved = catalog_lookup.get(
                            sanitize_cache_object_name(obj_name)
                        )
                        if cat_by_saved:
                            common_name = cat_by_saved["common_name"]
                            obj_type = cat_by_saved["type"]
                    if ra is None and saved_cfg.get("ra") is not None:
                        try:
                            ra = float(saved_cfg["ra"])
                            dec = float(saved_cfg["dec"])
                        except (TypeError, ValueError):
                            pass
                    if fov is None and saved_cfg.get("fov") is not None:
                        try:
                            fov = max(float(saved_cfg["fov"]), 0.25)
                        except (TypeError, ValueError):
                            pass

                if ra is None or dec is None:
                    m_rd = _RADEC_PREFIX_RE.match(san_prefix)
                    if m_rd:
                        try:
                            ra = float(m_rd.group(1))
                            dec = float(m_rd.group(2))
                            obj_name = f"RA {ra:.2f}° Dec {dec:+.2f}°"
                        except ValueError:
                            pass

                if fov is None:
                    fov = max(float(fallback_fov or 1.0), 0.25)

            if ra is None or dec is None or fov is None or fov <= 0:
                continue

            is_active = bool(active_setup_hash and entry == active_setup_hash)
            item = {
                "name": obj_name,
                "common_name": common_name,
                "type": obj_type,
                "ra": round(float(ra), 5),
                "dec": round(float(dec), 5),
                "fov": round(float(fov), 4),
                "url": url,
                "setup_hash": entry,
                "resolution": res_px,
                "is_active_setup": is_active,
                "is_custom": is_custom,
            }

            key = (f"{item['ra']:.3f}_{item['dec']:.3f}", round(item["fov"], 2))
            existing = dedup.get(key)
            if existing is None or (
                (item["is_active_setup"], item["resolution"])
                > (existing["is_active_setup"], existing["resolution"])
            ):
                dedup[key] = item

    # Sort widest FOV first so narrower, higher-detail cutouts render cleanly on top
    return sorted(
        dedup.values(),
        key=lambda x: (-x["fov"], x["is_active_setup"], x["name"]),
    )

