import io
import json
import math
import os
import re
import shutil
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from PIL import Image

from .config import CACHE_DIR, get_resource_path
from .image_service import list_cached_sky_images, resolve_safe_cache_path

ATLAS_CACHE_SUBDIR = "_sky_atlas"
MANIFEST_FILENAME = "sky_atlas.json"
LICENSE_NOTICE_FILENAME = "LICENSE-ASSETS.md"
DEFAULT_TILE_SIZE = 128
DEFAULT_SHEET_SIZE = 4096
DEFAULT_WEBP_QUALITY = 78

ATLAS_LICENSE = "CC-BY-NC-SA-4.0"
ATLAS_ATTRIBUTION = (
    "Digitized Sky Survey (STScI / Caltech POSS-II / UKST / AAO) via NASA SkyView"
)

_SAFE_ATLAS_FILE_RE = re.compile(
    r"^(?:sky_atlas(?:_\d+\.webp|\.json)|LICENSE-ASSETS\.md)$"
)

DSS_ASSET_NOTICE_MARKDOWN = """# Sky Atlas Imagery Attribution & License Notice

The pre-baked deep-sky preview sprite sheets (`sky_atlas_*.webp`) and coordinate manifest (`sky_atlas.json`) in this directory are derived from **Digitized Sky Survey (DSS / DSS2)** astronomical plates retrieved via NASA's *SkyView* Virtual Observatory.

Unlike the **My Sky Observer** application source code (which is licensed under the [MIT License](../../LICENSE)), the Digitized Sky Survey imagery is **not** under the MIT License or in the public domain.

## License (`CC BY-NC-SA 4.0`)

Per the official Space Telescope Science Institute (STScI) / MAST copyright policy (<https://archive.stsci.edu/dss/copyright.html>), Digitized Sky Survey images and derived mosaics/atlases are licensed for **non-commercial scientific, educational, and open-source use** under the **Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International License (CC BY-NC-SA 4.0)**:

- **License Terms:** <https://creativecommons.org/licenses/by-nc-sa/4.0/>
- **Commercial Use:** Commercial or for-profit redistribution of these DSS-derived image sheets requires separate written permission from STScI (<https://archive.stsci.edu/dss/copyright.html>).

## Required Attribution

> The Digitized Sky Surveys were produced at the Space Telescope Science Institute under U.S. Government grant NAG W-2166. The images of these surveys are based on photographic data obtained using the Oschin Schmidt Telescope on Palomar Mountain and the UK Schmidt Telescope. The plates were processed into the present compressed digital form with the permission of these institutions.
>
> The Second Palomar Observatory Sky Survey (POSS-II) was made by the California Institute of Technology with funds from the National Science Foundation, the National Geographic Society, the Sloan Foundation, the Samuel Oschin Foundation, and the Eastman Kodak Corporation.
>
> The UK Schmidt Telescope was operated by the Royal Observatory Edinburgh, with funding from the UK Science and Engineering Research Council (later the UK Particle Physics and Astronomy Research Council), until 1988 June, and thereafter by the Anglo-Australian Observatory.
>
> Supplemental SkyView survey cutouts courtesy of NASA's High Energy Astrophysics Science Archive Research Center (HEASARC).
"""


def validate_atlas_filename(filename: str) -> str:
    """Validate that a requested sky_atlas filename is strictly a safe atlas artifact."""
    if not isinstance(filename, str) or not _SAFE_ATLAS_FILE_RE.match(filename):
        raise ValueError(f"Invalid sky atlas filename: {filename!r}")
    return filename


def resolve_safe_dir_file(base_dir: str, filename: str) -> str:
    """Safely resolve a validated atlas filename inside base_dir."""
    safe_name = validate_atlas_filename(filename)
    base_abs = os.path.abspath(base_dir)
    target_abs = os.path.abspath(os.path.join(base_abs, safe_name))
    if os.path.normcase(os.path.commonpath([base_abs, target_abs])) != os.path.normcase(
        base_abs
    ):
        raise ValueError("Atlas path escapes directory")
    return target_abs


def get_candidate_atlas_dirs(cache_dir: str = CACHE_DIR) -> List[Tuple[str, str]]:
    """Return ordered (source_label, abs_dir_path) candidates where sky_atlas may reside."""
    candidates: List[Tuple[str, str]] = []
    seen = set()

    raw_list = [
        ("cache", os.path.abspath(os.path.join(cache_dir, ATLAS_CACHE_SUBDIR))),
        ("public", os.path.abspath(get_resource_path("frontend/public/sky_atlas"))),
        ("dist", os.path.abspath(get_resource_path("frontend/dist/sky_atlas"))),
    ]
    for label, path in raw_list:
        norm = os.path.normcase(path)
        if norm not in seen:
            seen.add(norm)
            candidates.append((label, path))
    return candidates


def _read_manifest_summary(atlas_dir: str) -> Optional[dict]:
    manifest_path = os.path.join(atlas_dir, MANIFEST_FILENAME)
    if not os.path.isfile(manifest_path):
        return None
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            return None
        sheets = data.get("sheets") or []
        # Verify all referenced sheet files actually exist
        for sh in sheets:
            sh_file = sh.get("file", "")
            if not sh_file or not os.path.isfile(os.path.join(atlas_dir, sh_file)):
                return None
        mtime = os.path.getmtime(manifest_path)
        return {
            "manifest": data,
            "count": len(data["items"]),
            "sheets_count": len(sheets),
            "generated_at": str(data.get("generated_at") or ""),
            "mtime": mtime,
        }
    except Exception:
        return None


def resolve_active_atlas_dir(cache_dir: str = CACHE_DIR) -> Tuple[Optional[str], Optional[str], Optional[dict]]:
    """
    Pick the best valid sky_atlas directory across runtime cache, public, and dist.
    Prefers whichever atlas contains more targets, breaking ties by modification time.
    Returns (source_label, dir_path, summary_dict).
    """
    best_label: Optional[str] = None
    best_dir: Optional[str] = None
    best_summary: Optional[dict] = None
    best_rank = (-1, -1.0)

    for label, path in get_candidate_atlas_dirs(cache_dir):
        summary = _read_manifest_summary(path)
        if summary is None:
            continue
        rank = (summary["count"], summary["mtime"])
        if rank > best_rank:
            best_rank = rank
            best_label = label
            best_dir = path
            best_summary = summary

    return best_label, best_dir, best_summary


def get_sky_atlas_status(cache_dir: str = CACHE_DIR) -> dict:
    """Return status metadata for the active pre-baked sky atlas."""
    label, atlas_dir, summary = resolve_active_atlas_dir(cache_dir)
    if not atlas_dir or not summary:
        return {
            "available": False,
            "exists": False,
            "count": 0,
            "sheets": 0,
            "size_mb": 0.0,
            "tile_size": DEFAULT_TILE_SIZE,
            "generated_at": None,
            "source": None,
        }

    total_bytes = 0
    for fname in os.listdir(atlas_dir):
        if _SAFE_ATLAS_FILE_RE.match(fname):
            fpath = os.path.join(atlas_dir, fname)
            if os.path.isfile(fpath):
                total_bytes += os.path.getsize(fpath)

    manifest = summary["manifest"]
    return {
        "available": True,
        "exists": True,
        "count": summary["count"],
        "sheets": summary["sheets_count"],
        "size_mb": round(total_bytes / (1024 * 1024), 2),
        "tile_size": int(manifest.get("tile_size") or DEFAULT_TILE_SIZE),
        "generated_at": summary["generated_at"] or None,
        "source": label,
    }


def _candidate_rank(item: dict) -> Tuple[int, int, float, int]:
    """Rank multiple cached cutouts of the same target; higher tuple wins."""
    fov = float(item.get("fov") or 1.0)
    reasonable_fov = 1 if 0.35 <= fov <= 8.0 else 0
    is_active = 1 if item.get("is_active_setup") else 0
    res = int(item.get("resolution") or 1024)
    # Within reasonable framing FOVs, slightly wider cutouts provide better crop margin
    fov_score = fov if reasonable_fov else -abs(fov - 2.0)
    return (reasonable_fov, is_active, round(fov_score, 3), res)


def _load_and_resize_tile(filepath: str, tile_size: int) -> Optional[Image.Image]:
    try:
        with Image.open(filepath) as im:
            rgb = im.convert("RGB")
            if rgb.size != (tile_size, tile_size):
                return rgb.resize((tile_size, tile_size), Image.Resampling.LANCZOS)
            return rgb.copy()
    except Exception:
        return None


def _extract_existing_atlas_tiles(
    atlas_dir: str, tile_size: int
) -> Dict[str, Tuple[dict, Image.Image]]:
    """Load existing tiles from an earlier sky_atlas so incremental builds keep prior targets."""
    preserved: Dict[str, Tuple[dict, Image.Image]] = {}
    summary = _read_manifest_summary(atlas_dir)
    if not summary:
        return preserved

    manifest = summary["manifest"]
    sheets_meta = {
        int(sh["index"]): sh["file"]
        for sh in (manifest.get("sheets") or [])
        if "index" in sh and "file" in sh
    }
    opened_sheets: Dict[int, Image.Image] = {}
    try:
        for idx, sh_file in sheets_meta.items():
            sh_path = os.path.join(atlas_dir, sh_file)
            if os.path.isfile(sh_path):
                with Image.open(sh_path) as sim:
                    opened_sheets[idx] = sim.convert("RGB")

        for entry in manifest.get("items") or []:
            name = str(entry.get("name") or "").strip()
            s_idx = entry.get("s")
            if not name or s_idx not in opened_sheets:
                continue
            u = int(entry.get("u", 0))
            v = int(entry.get("v", 0))
            w = int(entry.get("w", tile_size))
            h = int(entry.get("h", tile_size))
            sheet_im = opened_sheets[s_idx]
            if u < 0 or v < 0 or u + w > sheet_im.width or v + h > sheet_im.height:
                continue
            cropped = sheet_im.crop((u, v, u + w, v + h))
            if cropped.size != (tile_size, tile_size):
                cropped = cropped.resize((tile_size, tile_size), Image.Resampling.LANCZOS)
            preserved[name] = (entry, cropped)
    except Exception:
        pass
    finally:
        for sim in opened_sheets.values():
            try:
                sim.close()
            except Exception:
                pass

    return preserved


def _write_atlas_directory(
    target_dir: str,
    manifest_bytes: bytes,
    encoded_sheets: List[Tuple[str, bytes]],
) -> None:
    os.makedirs(target_dir, exist_ok=True)
    expected_files = {MANIFEST_FILENAME, LICENSE_NOTICE_FILENAME}

    for sheet_filename, webp_bytes in encoded_sheets:
        expected_files.add(sheet_filename)
        out_path = resolve_safe_dir_file(target_dir, sheet_filename)
        tmp_path = out_path + ".tmp"
        with open(tmp_path, "wb") as f:
            f.write(webp_bytes)
        os.replace(tmp_path, out_path)

    notice_path = resolve_safe_dir_file(target_dir, LICENSE_NOTICE_FILENAME)
    with open(notice_path, "w", encoding="utf-8") as nf:
        nf.write(DSS_ASSET_NOTICE_MARKDOWN)

    manifest_path = resolve_safe_dir_file(target_dir, MANIFEST_FILENAME)
    tmp_manifest = manifest_path + ".tmp"
    with open(tmp_manifest, "wb") as mf:
        mf.write(manifest_bytes)
    os.replace(tmp_manifest, manifest_path)

    # Remove stale sheet files from older larger builds
    for existing in os.listdir(target_dir):
        if (
            existing.startswith("sky_atlas_")
            and existing.endswith(".webp")
            and existing not in expected_files
        ):
            try:
                os.remove(resolve_safe_dir_file(target_dir, existing))
            except Exception:
                pass


def build_sky_atlas(
    cache_dir: str = CACHE_DIR,
    output_dir: Optional[str] = None,
    mirror_dirs: Optional[List[str]] = None,
    settings: Optional[dict] = None,
    active_setup_hash: Optional[str] = None,
    fallback_fov: float = 1.0,
    tile_size: int = DEFAULT_TILE_SIZE,
    sheet_size: int = DEFAULT_SHEET_SIZE,
    quality: int = DEFAULT_WEBP_QUALITY,
    include_custom: bool = False,
    merge_existing: bool = True,
) -> dict:
    """
    Build pre-baked WebP sprite sheet(s) + JSON coordinate manifest from cached sky images.
    """
    tile_size = max(32, min(512, int(tile_size)))
    cols = max(1, int(sheet_size) // tile_size)
    sheet_px_w = cols * tile_size
    tiles_per_sheet = cols * cols
    quality = max(40, min(95, int(quality)))

    if output_dir is None:
        output_dir = os.path.abspath(os.path.join(cache_dir, ATLAS_CACHE_SUBDIR))
    else:
        output_dir = os.path.abspath(output_dir)

    cached_items = list_cached_sky_images(
        cache_dir=cache_dir,
        settings=settings,
        active_setup_hash=active_setup_hash,
        fallback_fov=fallback_fov,
    )

    best_by_key: Dict[str, Tuple[dict, str]] = {}
    for item in cached_items:
        if item.get("is_custom") and not include_custom:
            continue
        url = str(item.get("url") or "")
        if not url.startswith("/cache/"):
            continue
        rel_parts = url[len("/cache/") :].split("/")
        if len(rel_parts) != 2:
            continue
        try:
            filepath = resolve_safe_cache_path(cache_dir, rel_parts[0], rel_parts[1])
        except ValueError:
            continue
        if not os.path.isfile(filepath):
            continue

        dedup_key = (
            f"custom_{item['ra']:.3f}_{item['dec']:.3f}"
            if item.get("is_custom")
            else str(item["name"]).strip()
        )
        prev = best_by_key.get(dedup_key)
        if prev is None or _candidate_rank(item) > _candidate_rank(prev[0]):
            best_by_key[dedup_key] = (item, filepath)

    # Optionally preserve previously baked targets that are no longer in raw image_cache
    preserved_tiles: Dict[str, Tuple[dict, Image.Image]] = {}
    if merge_existing:
        _, existing_dir, _ = resolve_active_atlas_dir(cache_dir)
        source_existing_dir = (
            existing_dir
            if existing_dir
            else (output_dir if os.path.isdir(output_dir) else None)
        )
        if source_existing_dir:
            preserved_tiles = _extract_existing_atlas_tiles(source_existing_dir, tile_size)

    # Resize fresh cached images in parallel across worker threads
    fresh_keys = sorted(best_by_key.keys())
    resized_fresh: Dict[str, Image.Image] = {}
    if fresh_keys:
        max_workers = min(8, max(2, (os.cpu_count() or 4)))

        def _worker(k: str) -> Tuple[str, Optional[Image.Image]]:
            _, fpath = best_by_key[k]
            return k, _load_and_resize_tile(fpath, tile_size)

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            for k, tile_im in pool.map(_worker, fresh_keys):
                if tile_im is not None:
                    resized_fresh[k] = tile_im

    # Combine fresh + preserved entries
    combined_records: List[Tuple[dict, Image.Image]] = []
    for k in fresh_keys:
        tile_im = resized_fresh.get(k)
        if tile_im is None:
            continue
        item, _ = best_by_key[k]
        combined_records.append(
            (
                {
                    "name": item["name"],
                    "common": item.get("common_name") or "",
                    "type": item.get("type") or "",
                    "ra": round(float(item["ra"]), 5),
                    "dec": round(float(item["dec"]), 5),
                    "fov": round(float(item["fov"]), 4),
                    "custom": bool(item.get("is_custom")),
                },
                tile_im,
            )
        )

    fresh_names = {rec[0]["name"] for rec in combined_records}
    for pres_name, (pres_entry, pres_img) in preserved_tiles.items():
        if pres_name in fresh_names:
            try:
                pres_img.close()
            except Exception:
                pass
            continue
        if pres_entry.get("custom") and not include_custom:
            try:
                pres_img.close()
            except Exception:
                pass
            continue
        combined_records.append(
            (
                {
                    "name": pres_name,
                    "common": str(pres_entry.get("common") or ""),
                    "type": str(pres_entry.get("type") or ""),
                    "ra": round(float(pres_entry["ra"]), 5),
                    "dec": round(float(pres_entry["dec"]), 5),
                    "fov": round(float(pres_entry["fov"]), 4),
                    "custom": bool(pres_entry.get("custom")),
                },
                pres_img,
            )
        )

    # Sort widest FOV first so smaller, higher-detail cutouts naturally layer on top
    combined_records.sort(key=lambda pair: (-pair[0]["fov"], pair[0]["name"]))

    sheets_meta: List[dict] = []
    encoded_sheets: List[Tuple[str, bytes]] = []
    manifest_items: List[dict] = []

    total = len(combined_records)
    num_sheets = math.ceil(total / tiles_per_sheet) if total > 0 else 0

    for s_idx in range(num_sheets):
        batch = combined_records[s_idx * tiles_per_sheet : (s_idx + 1) * tiles_per_sheet]
        rows_used = max(1, math.ceil(len(batch) / cols))
        sheet_px_h = rows_used * tile_size
        sheet_im = Image.new("RGB", (sheet_px_w, sheet_px_h), (0, 0, 0))

        for local_i, (meta, tile_im) in enumerate(batch):
            col = local_i % cols
            row = local_i // cols
            u = col * tile_size
            v = row * tile_size
            sheet_im.paste(tile_im, (u, v))
            try:
                tile_im.close()
            except Exception:
                pass

            entry_out = {
                "name": meta["name"],
                "ra": meta["ra"],
                "dec": meta["dec"],
                "fov": meta["fov"],
                "s": s_idx,
                "u": u,
                "v": v,
                "w": tile_size,
                "h": tile_size,
            }
            if meta["common"]:
                entry_out["common"] = meta["common"]
            if meta["type"]:
                entry_out["type"] = meta["type"]
            if meta["custom"]:
                entry_out["custom"] = True
            manifest_items.append(entry_out)

        sheet_filename = f"sky_atlas_{s_idx}.webp"
        buf = io.BytesIO()
        sheet_im.save(buf, format="WEBP", quality=quality, method=4)
        sheet_im.close()
        webp_bytes = buf.getvalue()

        encoded_sheets.append((sheet_filename, webp_bytes))
        sheets_meta.append(
            {
                "index": s_idx,
                "file": sheet_filename,
                "width": sheet_px_w,
                "height": sheet_px_h,
                "tiles": len(batch),
                "bytes": len(webp_bytes),
            }
        )

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest_payload = {
        "version": 1,
        "generated_at": generated_at,
        "license": ATLAS_LICENSE,
        "attribution": ATLAS_ATTRIBUTION,
        "tile_size": tile_size,
        "count": len(manifest_items),
        "sheets": sheets_meta,
        "items": manifest_items,
    }
    manifest_bytes = json.dumps(
        manifest_payload, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")

    written_dirs: List[str] = []
    _write_atlas_directory(output_dir, manifest_bytes, encoded_sheets)
    written_dirs.append(output_dir)

    for mdir in mirror_dirs or []:
        abs_m = os.path.abspath(mdir)
        if os.path.normcase(abs_m) == os.path.normcase(output_dir):
            continue
        try:
            _write_atlas_directory(abs_m, manifest_bytes, encoded_sheets)
            written_dirs.append(abs_m)
        except Exception:
            pass

    total_bytes = len(manifest_bytes) + sum(len(b) for _, b in encoded_sheets)
    return {
        "status": "ok",
        "count": len(manifest_items),
        "fresh_cached_used": len(resized_fresh),
        "preserved_from_previous": max(0, len(manifest_items) - len(resized_fresh)),
        "sheets": len(sheets_meta),
        "tile_size": tile_size,
        "size_mb": round(total_bytes / (1024 * 1024), 2),
        "generated_at": generated_at,
        "written_dirs": written_dirs,
    }


def create_atlas_export_zip_bytes(cache_dir: str = CACHE_DIR) -> Optional[bytes]:
    """Package the active sky_atlas directory into an in-memory .zip archive for git/export."""
    _, atlas_dir, summary = resolve_active_atlas_dir(cache_dir)
    if not atlas_dir or not summary:
        return None

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_STORED) as zf:
        for fname in sorted(os.listdir(atlas_dir)):
            if not _SAFE_ATLAS_FILE_RE.match(fname):
                continue
            fpath = resolve_safe_dir_file(atlas_dir, fname)
            if os.path.isfile(fpath):
                zf.write(fpath, arcname=f"sky_atlas/{fname}")
        if not os.path.isfile(os.path.join(atlas_dir, LICENSE_NOTICE_FILENAME)):
            zf.writestr(
                f"sky_atlas/{LICENSE_NOTICE_FILENAME}",
                DSS_ASSET_NOTICE_MARKDOWN.encode("utf-8"),
            )
    return buf.getvalue()
