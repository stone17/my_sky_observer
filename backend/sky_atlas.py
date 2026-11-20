import io
import json
import math
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
from PIL import Image

from .config import CACHE_DIR, get_resource_path
from .image_service import (
    list_cached_sky_images,
    parse_setup_hash_fov,
    resolve_safe_cache_path,
)

ATLAS_CACHE_SUBDIR = "_sky_atlas"
MANIFEST_FILENAME = "sky_atlas.json"
LICENSE_NOTICE_FILENAME = "LICENSE-ASSETS.md"

DEFAULT_LOD0_TILE_SIZE = 256
DEFAULT_LOD0_STEP_DEG = 12.85
DEFAULT_LOD1_TILE_SIZE = 512
DEFAULT_LOD1_STEP_DEG = 6.0
DEFAULT_LOD2_TILE_SIZE = 512
DEFAULT_LOD2_STEP_DEG = 3.0
DEFAULT_SHEET_SIZE = 4096
DEFAULT_WEBP_QUALITY = 84

DEG2RAD = math.pi / 180.0
RAD2DEG = 180.0 / math.pi

# Night-sky tone grade matching SkyContextMap's deep-space canvas (#050912)
_SKY_BASE_RGB = np.array([5.0, 9.0, 18.0], dtype=np.float32)
_SKY_SCREEN_GAIN = 0.88 * (255.0 - _SKY_BASE_RGB) / 255.0

# Precomputed raised-cosine smoothstep and full-cutout sine-squared bell LUTs
_LUT_LEN = 2048
_COS_LUT = (0.5 - 0.5 * np.cos(np.linspace(0.0, math.pi, _LUT_LEN, dtype=np.float32))).astype(
    np.float32
)
_BELL_LUT = (np.sin(np.linspace(0.0, math.pi, _LUT_LEN, dtype=np.float32)) ** 2).astype(
    np.float32
)

_BG_GRID_N = 16
_LOW_BAND_N = 64
_BG_GRID_CACHE_FILENAME = "_bg_grids_v1.npz"

ATLAS_LICENSE = "CC-BY-NC-SA-4.0"
ATLAS_ATTRIBUTION = (
    "Digitized Sky Survey (STScI / Caltech POSS-II / UKST / AAO) via NASA SkyView"
)

_SAFE_ATLAS_FILE_RE = re.compile(
    r"^(?:sky_atlas(?:_(?:lod\d+(?:_\d+)?|\d+)\.webp|\.json)|LICENSE-ASSETS\.md)$"
)

DSS_ASSET_NOTICE_MARKDOWN = """# Sky Atlas Imagery Attribution & License Notice

The pre-baked deep-sky survey sector sheets (`sky_atlas_*.webp`) and coordinate manifest (`sky_atlas.json`) in this directory are derived from **Digitized Sky Survey (DSS / DSS2)** astronomical plates retrieved via NASA's *SkyView* Virtual Observatory.

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
        ("public", os.path.abspath(get_resource_path("frontend/public/sky_atlas"))),
        ("dist", os.path.abspath(get_resource_path("frontend/dist/sky_atlas"))),
        ("cache", os.path.abspath(os.path.join(cache_dir, ATLAS_CACHE_SUBDIR))),
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
        if not isinstance(data, dict):
            return None

        sheets = data.get("sheets") or []
        for sh in sheets:
            sh_file = sh.get("file", "")
            if not sh_file or not os.path.isfile(os.path.join(atlas_dir, sh_file)):
                return None

        version = int(data.get("version") or 1)
        lod_count = 1
        if version >= 2 and data.get("mode") == "sectors":
            raw_lods = data.get("lods")
            if isinstance(raw_lods, list) and raw_lods:
                lod_count = len(raw_lods)
                max_sectors = max(
                    (len(l.get("sectors") or []) for l in raw_lods if isinstance(l, dict)),
                    default=0,
                )
            else:
                lod0_sectors = (data.get("lod0") or {}).get("sectors") or []
                lod1_sectors = (data.get("lod1") or {}).get("sectors") or []
                lod2_sectors = (data.get("lod2") or {}).get("sectors") or []
                if not isinstance(lod0_sectors, list) or not isinstance(lod1_sectors, list):
                    return None
                lod_count = 3 if (isinstance(lod2_sectors, list) and lod2_sectors) else 2
                max_sectors = max(len(lod0_sectors), len(lod1_sectors), len(lod2_sectors) if isinstance(lod2_sectors, list) else 0)
            source_count = int(data.get("source_count") or 0)
            count_metric = max(source_count, max_sectors)
        elif isinstance(data.get("items"), list):
            count_metric = len(data["items"])
        else:
            return None

        mtime = os.path.getmtime(manifest_path)
        return {
            "manifest": data,
            "version": version,
            "lod_count": lod_count,
            "count": count_metric,
            "sheets_count": len(sheets),
            "generated_at": str(data.get("generated_at") or ""),
            "mtime": mtime,
        }
    except Exception:
        return None


def resolve_active_atlas_dir(
    cache_dir: str = CACHE_DIR,
) -> Tuple[Optional[str], Optional[str], Optional[dict]]:
    """
    Pick the best valid sky_atlas directory across public, dist, and runtime cache.
    Prefers v2 pre-composited sector atlases, higher LOD tier depth, higher coverage count, then newer mtime.
    """
    best_label: Optional[str] = None
    best_dir: Optional[str] = None
    best_summary: Optional[dict] = None
    best_rank = (-1, -1, -1, -1.0)

    for label, path in get_candidate_atlas_dirs(cache_dir):
        summary = _read_manifest_summary(path)
        if summary is None:
            continue
        rank = (summary["version"], summary["lod_count"], summary["count"], summary["mtime"])
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
            "version": 2,
            "count": 0,
            "sheets": 0,
            "size_mb": 0.0,
            "generated_at": None,
            "source": None,
        }

    total_bytes = 0
    for fname in os.listdir(atlas_dir):
        if _SAFE_ATLAS_FILE_RE.match(fname):
            fpath = os.path.join(atlas_dir, fname)
            if os.path.isfile(fpath):
                total_bytes += os.path.getsize(fpath)

    return {
        "available": True,
        "exists": True,
        "version": summary["version"],
        "count": summary["count"],
        "sheets": summary["sheets_count"],
        "size_mb": round(total_bytes / (1024 * 1024), 2),
        "generated_at": summary["generated_at"] or None,
        "source": label,
    }


def _candidate_rank(item: dict) -> Tuple[int, int, float, int]:
    """Rank multiple cached cutouts of the same target; higher tuple wins."""
    fov = float(item.get("fov") or 1.0)
    reasonable_fov = 1 if 0.3 <= fov <= 10.0 else 0
    is_active = 1 if item.get("is_active_setup") else 0
    res = int(item.get("resolution") or 1024)
    fov_score = fov if reasonable_fov else -abs(fov - 2.0)
    return (reasonable_fov, is_active, round(fov_score, 3), res)


def _make_tangent_basis(ra_deg: float, dec_deg: float) -> np.ndarray:
    """
    Return 3x3 float64 basis matrix rows [T, E, N] for gnomonic projection at (ra_deg, dec_deg).
    Handles exact North (+90) and South (-90) celestial poles cleanly without singularity.
    """
    if dec_deg >= 89.9999:
        return np.array(
            [
                [0.0, 0.0, 1.0],
                [0.0, 1.0, 0.0],
                [-1.0, 0.0, 0.0],
            ],
            dtype=np.float64,
        )
    if dec_deg <= -89.9999:
        return np.array(
            [
                [0.0, 0.0, -1.0],
                [0.0, 1.0, 0.0],
                [1.0, 0.0, 0.0],
            ],
            dtype=np.float64,
        )

    a = ra_deg * DEG2RAD
    d = dec_deg * DEG2RAD
    sa, ca = math.sin(a), math.cos(a)
    sd, cd = math.sin(d), math.cos(d)
    return np.array(
        [
            [cd * ca, cd * sa, sd],
            [-sa, ca, 0.0],
            [-sd * ca, -sd * sa, cd],
        ],
        dtype=np.float64,
    )


def _generate_sector_grid(step_deg: float, tile_px: int) -> List[dict]:
    """
    Partition the 360x180 celestial sphere into quasi-uniform spherical RA/Dec cells
    with polar caps at +-90 deg, and compute each cell's padded gnomonic tangent window.
    """
    n_dec = max(6, int(round(180.0 / float(step_deg))))
    d_step = 180.0 / n_dec
    sectors: List[dict] = []

    for b in range(n_dec):
        dec_min = -90.0 + b * d_step
        dec_max = -90.0 + (b + 1) * d_step

        if b == 0 or b == n_dec - 1:
            is_north = b == n_dec - 1
            ra0 = 0.0
            dec0 = 90.0 if is_north else -90.0
            basis = _make_tangent_basis(ra0, dec0)
            req_angle = d_step * DEG2RAD
            px_rad_est = (2.0 * req_angle) / max(16.0, tile_px - 12.0)
            half_angle = req_angle + 6.0 * px_rad_est
            half_tan = math.tan(half_angle)
            cone_rad = math.atan(math.sqrt(2.0) * half_tan)
            sectors.append(
                {
                    "b": b,
                    "c": 0,
                    "ra0": ra0,
                    "dec0": dec0,
                    "ra_min": 0.0,
                    "ra_max": 360.0,
                    "ra_step": 360.0,
                    "dec_min": dec_min,
                    "dec_max": dec_max,
                    "d_step": d_step,
                    "pole": 1 if is_north else -1,
                    "basis": basis,
                    "half_tan": half_tan,
                    "span_deg": 2.0 * half_angle * RAD2DEG,
                    "cone_rad": cone_rad,
                    "tile_px": tile_px,
                }
            )
            continue

        dec0 = 0.5 * (dec_min + dec_max)
        n_ra = max(6, int(round(360.0 * math.cos(dec0 * DEG2RAD) / d_step)))
        ra_step = 360.0 / n_ra

        for c in range(n_ra):
            ra_min = c * ra_step
            ra_max = (c + 1) * ra_step
            ra0 = 0.5 * (ra_min + ra_max)
            basis = _make_tangent_basis(ra0, dec0)

            # Sample boundary of [ra_min, ra_max] x [dec_min, dec_max] to guarantee complete gnomonic enclosure
            ra_samples = np.linspace(ra_min, ra_max, 9) * DEG2RAD
            dec_samples = np.linspace(dec_min, dec_max, 9) * DEG2RAD

            b_ra = np.concatenate(
                [
                    ra_samples,
                    ra_samples,
                    np.full(9, ra_min * DEG2RAD),
                    np.full(9, ra_max * DEG2RAD),
                ]
            )
            b_dec = np.concatenate(
                [
                    np.full(9, dec_min * DEG2RAD),
                    np.full(9, dec_max * DEG2RAD),
                    dec_samples,
                    dec_samples,
                ]
            )
            cd = np.cos(b_dec)
            pts = np.stack([cd * np.cos(b_ra), cd * np.sin(b_ra), np.sin(b_dec)], axis=0)
            proj = basis @ pts
            denom = np.maximum(proj[0], 1e-6)
            max_tan = float(
                max(np.max(np.abs(proj[1] / denom)), np.max(np.abs(proj[2] / denom)))
            )
            req_angle = math.atan(max_tan)
            px_rad_est = (2.0 * req_angle) / max(16.0, tile_px - 12.0)
            half_angle = req_angle + 6.0 * px_rad_est
            half_tan = math.tan(half_angle)
            cone_rad = math.atan(math.sqrt(2.0) * half_tan)

            sectors.append(
                {
                    "b": b,
                    "c": c,
                    "ra0": ra0,
                    "dec0": dec0,
                    "ra_min": ra_min,
                    "ra_max": ra_max,
                    "ra_step": ra_step,
                    "dec_min": dec_min,
                    "dec_max": dec_max,
                    "d_step": d_step,
                    "pole": 0,
                    "basis": basis,
                    "half_tan": half_tan,
                    "span_deg": 2.0 * half_angle * RAD2DEG,
                    "cone_rad": cone_rad,
                    "tile_px": tile_px,
                }
            )

    return sectors


def _load_cutout_array(
    filepath: str, fov_deg: float, max_px_per_deg: float = 192.0
) -> Optional[np.ndarray]:
    """Decode JPEG cutout using libjpeg DCT draft scaling matched to finest LOD angular resolution."""
    target_px = min(1024, max(256, int(math.ceil(fov_deg * max_px_per_deg))))
    try:
        with Image.open(filepath) as im:
            im.draft("RGB", (target_px, target_px))
            rgb = im.convert("RGB")
            if rgb.width > target_px * 1.18 or rgb.height > target_px * 1.18:
                rgb = rgb.resize((target_px, target_px), Image.Resampling.BILINEAR)
            return np.asarray(rgb, dtype=np.uint8)
    except Exception:
        return None


def _extract_single_bg_grid(filepath: str) -> Optional[np.ndarray]:
    """
    Fast DCT-draft decode to 128x128 and extract a star-rejected (16, 16, 3) uint8
    30th-percentile sky-background grid for global photometric harmonization.
    """
    g = _BG_GRID_N
    draft_px = g * 8  # 128x128 -> 8x8 = 64 samples per cell
    try:
        with Image.open(filepath) as im:
            im.draft("RGB", (draft_px, draft_px))
            rgb = im.convert("RGB")
            if rgb.width != draft_px or rgb.height != draft_px:
                rgb = rgb.resize((draft_px, draft_px), Image.Resampling.BILINEAR)
            arr = np.asarray(rgb, dtype=np.uint8)
        blocks = arr.reshape(g, 8, g, 8, 3).transpose(0, 2, 1, 3, 4).reshape(g, g, 64, 3)
        # 30th percentile (index 19 of 64) rejects stars while preserving dust lanes and sky glow
        return np.partition(blocks, 19, axis=2)[:, :, 19, :].copy()
    except Exception:
        return None


def _load_or_extract_bg_grids(
    cutout_metas: List[dict],
    root_cache_dir: str,
    max_workers: int = 8,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Extract or load cached (N, 16, 16, 3) star-rejected background grids for all cutouts.
    Returns (bg_grids_float32, valid_mask_bool).
    """
    n = len(cutout_metas)
    g = _BG_GRID_N
    bg_u8 = np.zeros((n, g, g, 3), dtype=np.uint8)
    valid = np.zeros(n, dtype=bool)
    if n == 0:
        return bg_u8.astype(np.float32), valid

    use_disk_cache = n >= 32
    cache_candidates = [
        os.path.join(root_cache_dir, ATLAS_CACHE_SUBDIR, _BG_GRID_CACHE_FILENAME),
        os.path.join(CACHE_DIR, ATLAS_CACHE_SUBDIR, _BG_GRID_CACHE_FILENAME),
    ]

    cached_lookup: Dict[str, Tuple[int, int, np.ndarray]] = {}
    if use_disk_cache:
        for cpath in cache_candidates:
            if not os.path.isfile(cpath):
                continue
            try:
                with np.load(cpath, allow_pickle=False) as npz:
                    c_keys = npz["keys"]
                    c_stats = npz["stats"]
                    c_grids = npz["grids"]
                    if (
                        c_grids.ndim == 4
                        and c_grids.shape[1:] == (g, g, 3)
                        and len(c_keys) == len(c_grids)
                        and len(c_stats) == len(c_grids)
                    ):
                        for i, k_str in enumerate(c_keys.tolist()):
                            cached_lookup[str(k_str)] = (
                                int(c_stats[i, 0]),
                                int(c_stats[i, 1]),
                                c_grids[i],
                            )
                        break
            except Exception:
                continue

    # Batch stat via os.scandir per parent directory to avoid thousands of SMB round-trips
    dir_stat_map: Dict[str, Tuple[int, int]] = {}
    seen_parent_dirs = set()
    for meta in cutout_metas:
        pdir = os.path.dirname(os.path.abspath(meta["filepath"]))
        pdir_norm = os.path.normcase(pdir)
        if pdir_norm in seen_parent_dirs:
            continue
        seen_parent_dirs.add(pdir_norm)
        try:
            with os.scandir(pdir) as it:
                for entry in it:
                    try:
                        st = entry.stat(follow_symlinks=False)
                        ekey = os.path.normcase(os.path.abspath(entry.path))
                        dir_stat_map[ekey] = (
                            int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))),
                            int(st.st_size),
                        )
                    except OSError:
                        continue
        except OSError:
            continue

    file_keys: List[str] = []
    file_stats = np.zeros((n, 2), dtype=np.int64)
    missing_indices: List[int] = []

    for idx, meta in enumerate(cutout_metas):
        fpath = meta["filepath"]
        norm_key = os.path.normcase(os.path.abspath(fpath))
        file_keys.append(norm_key)
        st_pair = dir_stat_map.get(norm_key)
        if st_pair is not None:
            mtime_ns, fsize = st_pair
        else:
            try:
                st = os.stat(fpath)
                mtime_ns = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))
                fsize = int(st.st_size)
            except OSError:
                mtime_ns, fsize = 0, 0
        file_stats[idx, 0] = mtime_ns
        file_stats[idx, 1] = fsize

        hit = cached_lookup.get(norm_key)
        if hit is not None and hit[0] == mtime_ns and hit[1] == fsize:
            bg_u8[idx] = hit[2]
            valid[idx] = True
        else:
            missing_indices.append(idx)

    if missing_indices:
        if progress_callback and len(missing_indices) >= 64:
            progress_callback(
                f"  Sampling sky-background grids for {len(missing_indices)} cutouts..."
            )

        def _job(idx: int) -> Tuple[int, Optional[np.ndarray]]:
            return idx, _extract_single_bg_grid(cutout_metas[idx]["filepath"])

        with ThreadPoolExecutor(max_workers=min(max_workers, len(missing_indices))) as pool:
            for idx, grid in pool.map(_job, missing_indices):
                if grid is not None:
                    bg_u8[idx] = grid
                    valid[idx] = True

        if use_disk_cache and np.any(valid):
            valid_idx = np.flatnonzero(valid)
            save_keys = np.array([file_keys[i] for i in valid_idx], dtype="<U512")
            save_stats = file_stats[valid_idx]
            save_grids = bg_u8[valid_idx]
            for cpath in cache_candidates:
                try:
                    os.makedirs(os.path.dirname(cpath), exist_ok=True)
                    buf = io.BytesIO()
                    np.savez(
                        buf,
                        keys=save_keys,
                        stats=save_stats,
                        grids=save_grids,
                    )
                    _atomic_or_direct_write(cpath, buf.getvalue())
                    break
                except Exception:
                    continue

    return bg_u8.astype(np.float32), valid


def _smooth_grid_3d_batch(arr: np.ndarray, passes: int = 4) -> np.ndarray:
    """Apply fast separable [1, 2, 1]/4 binomial smoothing on (N, H, W) float32 batch."""
    out = arr.astype(np.float32, copy=True)
    for _ in range(passes):
        py = np.pad(out, ((0, 0), (1, 1), (0, 0)), mode="edge")
        out = 0.25 * (py[:, :-2, :] + 2.0 * py[:, 1:-1, :] + py[:, 2:, :])
        px = np.pad(out, ((0, 0), (0, 0), (1, 1)), mode="edge")
        out = 0.25 * (px[:, :, :-2] + 2.0 * px[:, :, 1:-1] + px[:, :, 2:])
    return out


def _compute_cutout_harmonization(
    cutout_metas: List[dict],
    bg_grids: np.ndarray,
    valid_mask: np.ndarray,
) -> List[Optional[dict]]:
    """
    Global spherical coupled photometric harmonization across all overlapping cutouts.
    Neutralizes independent per-cutout auto-stretch brightness/contrast jumps, linear
    Schmidt-plate vignetting gradients, and non-linear edge/Voronoi plate discrepancies
    while preserving global contrast and large-scale Galactic dust/Milky Way structure.
    """
    n = len(cutout_metas)
    if n <= 1 or int(np.count_nonzero(valid_mask)) <= 1:
        return [None] * n

    g = _BG_GRID_N
    # Luminance (N, G, G) and centered zero-mean luminance dY so gain `a` and offset `b` are orthogonal
    lum = (
        0.299 * bg_grids[:, :, :, 0]
        + 0.587 * bg_grids[:, :, :, 1]
        + 0.114 * bg_grids[:, :, :, 2]
    ).astype(np.float32)

    # Down-weight bright emission cores / saturated star clusters so fits lock onto sky + dust background
    neb_w = np.clip((210.0 - lum) / 75.0, 0.12, 1.0).astype(np.float32)
    neb_w[~valid_mask] = 0.0

    w_sum_k = np.maximum(np.sum(neb_w, axis=(1, 2)), 1e-6)
    y_mean = (np.sum(lum * neb_w, axis=(1, 2)) / w_sum_k).astype(np.float32)
    dy = lum - y_mean[:, None, None]

    # Cutout grid coordinates matching _render_sector_rgba:
    # nx = (ix + 0.5)/G -> xi = (0.5 - nx) * 2 * half_tan_k
    # ny = (iy + 0.5)/G -> eta = (0.5 - ny) * 2 * half_tan_k
    centers_1d = (np.arange(g, dtype=np.float64) + 0.5) / float(g)
    gx_1d = (centers_1d - 0.5).astype(np.float32)  # [-0.46875 .. +0.46875], East->West
    gy_1d = (centers_1d - 0.5).astype(np.float32)  # [-0.46875 .. +0.46875], North->South
    gx = np.broadcast_to(gx_1d[None, :], (g, g)).astype(np.float32)
    gy = np.broadcast_to(gy_1d[:, None], (g, g)).astype(np.float32)

    xi_norm = (0.5 - centers_1d)[None, :]  # (1, G)
    eta_norm = (0.5 - centers_1d)[:, None]  # (G, 1)

    bases = np.array([m["basis"] for m in cutout_metas], dtype=np.float64)  # (N, 3, 3)
    half_tans = np.array([m["half_tan"] for m in cutout_metas], dtype=np.float64)  # (N,)

    two_ht = (2.0 * half_tans)[:, None, None]  # (N, 1, 1)
    xi_k = two_ht * xi_norm[None, :, :]  # (N, 1, G)
    eta_k = two_ht * eta_norm[None, :, :]  # (N, G, 1)

    t_vec = bases[:, 0, :][:, None, None, :]  # (N, 1, 1, 3)
    e_vec = bases[:, 1, :][:, None, None, :]
    n_vec = bases[:, 2, :][:, None, None, :]

    rays = t_vec + xi_k[..., None] * e_vec + eta_k[..., None] * n_vec  # (N, G, G, 3)
    inv_norm = 1.0 / np.maximum(np.linalg.norm(rays, axis=-1), 1e-12)
    px = rays[..., 0] * inv_norm
    py = rays[..., 1] * inv_norm
    pz = np.clip(rays[..., 2] * inv_norm, -1.0, 1.0)

    dec_deg = np.arcsin(pz) * RAD2DEG
    ra_deg = (np.arctan2(py, px) * RAD2DEG) % 360.0

    # 0.20 deg quasi-uniform cos(Dec) spherical cells for exact cross-cutout overlap matching
    cell_step_deg = 0.20
    n_dec_bins = int(round(180.0 / cell_step_deg))
    d_step = 180.0 / float(n_dec_bins)
    dec_centers = -90.0 + (np.arange(n_dec_bins, dtype=np.float64) + 0.5) * d_step
    n_ra_per_band = np.maximum(
        1, np.rint(360.0 * np.cos(dec_centers * DEG2RAD) / d_step).astype(np.int32)
    )
    band_offsets = np.zeros(n_dec_bins + 1, dtype=np.int32)
    band_offsets[1:] = np.cumsum(n_ra_per_band)

    b_dec = np.clip(((dec_deg + 90.0) / d_step).astype(np.int32), 0, n_dec_bins - 1)
    n_ra_s = n_ra_per_band[b_dec]
    c_ra = np.clip((ra_deg * (n_ra_s / 360.0)).astype(np.int32), 0, n_ra_s - 1)
    raw_cell_id = band_offsets[b_dec] + c_ra

    _, cell_idx = np.unique(raw_cell_id.ravel(), return_inverse=True)
    num_cells = int(cell_idx.max()) + 1

    valid_sample_flat = np.broadcast_to(valid_mask[:, None, None], (n, g, g)).ravel()
    cell_cnt = np.bincount(
        cell_idx, weights=valid_sample_flat.astype(np.int32), minlength=num_cells
    )
    is_overlap = (cell_cnt[cell_idx] >= 2).reshape(n, g, g) & valid_mask[:, None, None]
    if not np.any(is_overlap):
        return [None] * n

    # Frozen ~2 deg macro Galactic background anchor via normalized convolution on 1x1 deg sphere
    b_coarse = np.clip((dec_deg + 90.0).astype(np.int32), 0, 179)
    c_coarse = np.clip(ra_deg.astype(np.int32), 0, 359)
    coarse_idx = (b_coarse * 360 + c_coarse).ravel()
    coarse_sum = np.bincount(
        coarse_idx, weights=(lum * neb_w).ravel(), minlength=180 * 360
    ).reshape(180, 360)
    coarse_w = np.bincount(
        coarse_idx, weights=neb_w.ravel(), minlength=180 * 360
    ).reshape(180, 360)
    for _ in range(3):
        cs_y = np.pad(coarse_sum, ((1, 1), (0, 0)), mode="edge")
        cw_y = np.pad(coarse_w, ((1, 1), (0, 0)), mode="edge")
        coarse_sum = 0.25 * (cs_y[:-2, :] + 2.0 * cs_y[1:-1, :] + cs_y[2:, :])
        coarse_w = 0.25 * (cw_y[:-2, :] + 2.0 * cw_y[1:-1, :] + cw_y[2:, :])
        cs_x = np.pad(coarse_sum, ((0, 0), (1, 1)), mode="wrap")
        cw_x = np.pad(coarse_w, ((0, 0), (1, 1)), mode="wrap")
        coarse_sum = 0.25 * (cs_x[:, :-2] + 2.0 * cs_x[:, 1:-1] + cs_x[:, 2:])
        coarse_w = 0.25 * (cw_x[:, :-2] + 2.0 * cw_x[:, 1:-1] + cw_x[:, 2:])
    coarse_map = (coarse_sum / np.maximum(coarse_w, 1e-6)).astype(np.float32)
    t_anchor = coarse_map[b_coarse, c_coarse]

    # Solve Y_pred = lum + da * dy + b + cx * gx + cy * gy where a = 1 + da
    da = np.zeros(n, dtype=np.float32)
    b = np.zeros(n, dtype=np.float32)
    cx = np.zeros(n, dtype=np.float32)
    cy = np.zeros(n, dtype=np.float32)
    cutout_cons_w = np.ones(n, dtype=np.float32)

    dy_var = np.mean(dy * dy, axis=(1, 2))
    reg_da = (6.0 * dy_var + 25.0).astype(np.float32)
    reg_b = np.float32(1.2)
    reg_c = np.float32(6.0)
    has_ov_cutout = np.any(is_overlap, axis=(1, 2))

    # Pre-stack basis features X = [dy, 1, gx, gy] of shape (N, 256, 4)
    n_pts = g * g
    x_mat = np.empty((n, n_pts, 4), dtype=np.float32)
    x_mat[:, :, 0] = dy.reshape(n, n_pts)
    x_mat[:, :, 1] = 1.0
    x_mat[:, :, 2] = gx.reshape(1, n_pts)
    x_mat[:, :, 3] = gy.reshape(1, n_pts)

    base_fit_w = np.where(is_overlap, 1.0, 0.06).astype(np.float32) * neb_w

    for it in range(6):
        y_pred = (
            lum
            + da[:, None, None] * dy
            + b[:, None, None]
            + cx[:, None, None] * gx[None, :, :]
            + cy[:, None, None] * gy[None, :, :]
        )
        w_cons = neb_w * cutout_cons_w[:, None, None]
        sum_cell = np.bincount(
            cell_idx, weights=(y_pred * w_cons).ravel(), minlength=num_cells
        )
        w_cell = np.bincount(cell_idx, weights=w_cons.ravel(), minlength=num_cells)
        t_exact = (sum_cell / np.maximum(w_cell, 1e-6))[cell_idx].reshape(n, g, g).astype(
            np.float32
        )

        target = np.where(is_overlap, 0.88 * t_exact + 0.12 * t_anchor, t_anchor)

        if it == 0:
            w = base_fit_w
        else:
            abs_r = np.abs(target - y_pred)
            w_huber = np.where(abs_r > 10.0, 10.0 / np.maximum(abs_r, 1e-6), 1.0).astype(
                np.float32
            )
            w = base_fit_w * w_huber

        w_flat = w.reshape(n, n_pts)
        rhs_flat = (target - lum).reshape(n, n_pts)

        wx = x_mat * w_flat[:, :, None]  # (N, 256, 4)
        ata = np.einsum("npi,npj->nij", wx, x_mat).astype(np.float64)
        atb = np.einsum("npi,np->ni", wx, rhs_flat).astype(np.float64)

        ata[:, 0, 0] += reg_da
        ata[:, 1, 1] += reg_b
        ata[:, 2, 2] += reg_c
        ata[:, 3, 3] += reg_c

        sol = np.linalg.solve(ata, atb[..., None])[..., 0].astype(np.float32)
        damp = np.float32(0.80 if it < 3 else 0.65)
        da = np.where(has_ov_cutout, (1.0 - damp) * da + damp * sol[:, 0], 0.0)
        b = np.where(has_ov_cutout, (1.0 - damp) * b + damp * sol[:, 1], 0.0)
        cx = np.where(has_ov_cutout, (1.0 - damp) * cx + damp * sol[:, 2], 0.0)
        cy = np.where(has_ov_cutout, (1.0 - damp) * cy + damp * sol[:, 3], 0.0)

        a_cur = np.clip(1.0 + da, 0.72, 1.38)
        # Gauge-fix geometric mean of gain to 1.0 and mean offset to 0.0 so global tone never drifts
        if np.any(has_ov_cutout):
            mean_log_a = float(np.mean(np.log(a_cur[has_ov_cutout])))
            a_cur = np.where(
                has_ov_cutout,
                np.clip(a_cur * math.exp(-mean_log_a), 0.72, 1.38),
                1.0,
            )
            mean_b = float(np.mean(b[has_ov_cutout]))
            b = np.where(has_ov_cutout, np.clip(b - mean_b, -38.0, 38.0), 0.0)

        da = a_cur - 1.0
        cx = np.clip(cx, -28.0, 28.0)
        cy = np.clip(cy, -28.0, 28.0)

        if it == 2:
            # Down-weight cutouts with strong internal plate-edge anomalies in consensus
            y_mid = (
                lum
                + da[:, None, None] * dy
                + b[:, None, None]
                + cx[:, None, None] * gx[None, :, :]
                + cy[:, None, None] * gy[None, :, :]
            )
            ov_err = np.where(is_overlap, (t_exact - y_mid) * neb_w, 0.0)
            ov_cnt_k = np.maximum(np.sum(is_overlap, axis=(1, 2)), 1)
            rms_k = np.sqrt(np.sum(ov_err * ov_err, axis=(1, 2)) / ov_cnt_k)
            cutout_cons_w = np.clip(1.0 / (1.0 + (rms_k / 5.0) ** 2), 0.25, 1.0).astype(
                np.float32
            )

    a_final = (1.0 + da).astype(np.float32)
    y_param = (
        lum
        + da[:, None, None] * dy
        + b[:, None, None]
        + cx[:, None, None] * gx[None, :, :]
        + cy[:, None, None] * gy[None, :, :]
    )
    w_cons = neb_w * cutout_cons_w[:, None, None]
    sum_cell = np.bincount(
        cell_idx, weights=(y_param * w_cons).ravel(), minlength=num_cells
    )
    w_cell = np.bincount(cell_idx, weights=w_cons.ravel(), minlength=num_cells)
    t_exact = (sum_cell / np.maximum(w_cell, 1e-6))[cell_idx].reshape(n, g, g).astype(
        np.float32
    )

    raw_disc = np.where(is_overlap, (t_exact - y_param) * neb_w, 0.0).astype(np.float32)
    res_grids = np.clip(_smooth_grid_3d_batch(raw_disc, passes=4), -22.0, 22.0)

    # Chromatic background balance for mixed RGB/monochrome caches (identically 0 for monochrome DSS2)
    chroma = bg_grids - lum[..., None]  # (N, G, G, 3)
    color_deltas = np.zeros((n, 3), dtype=np.float32)
    for ch in range(3):
        ch_sum = np.bincount(
            cell_idx, weights=(chroma[:, :, :, ch] * w_cons).ravel(), minlength=num_cells
        )
        ch_cons = (ch_sum / np.maximum(w_cell, 1e-6))[cell_idx].reshape(n, g, g)
        ch_diff = np.where(is_overlap, (ch_cons - chroma[:, :, :, ch]) * neb_w, 0.0)
        ov_w_sum = np.sum(np.where(is_overlap, neb_w, 0.0), axis=(1, 2))
        color_deltas[:, ch] = np.where(
            ov_w_sum > 4.0,
            np.clip(np.sum(ch_diff, axis=(1, 2)) / np.maximum(ov_w_sum, 1e-6), -12.0, 12.0),
            0.0,
        )

    harmonization: List[Optional[dict]] = [None] * n
    for k in range(n):
        if not valid_mask[k] or not has_ov_cutout[k]:
            continue

        # Detect internal Voronoi Schmidt-plate step discontinuities inside cutout k
        ov_k = is_overlap[k] & (neb_w[k] > 0.45)
        plate_quality = 1.0
        if int(np.count_nonzero(ov_k)) >= 16:
            rd_k = raw_disc[k]
            res_rms = float(math.sqrt(float(np.mean(rd_k[ov_k] ** 2))))
            vx_valid = ov_k[:, 1:] & ov_k[:, :-1]
            vy_valid = ov_k[1:, :] & ov_k[:-1, :]
            jump_x = float(np.max(np.abs(rd_k[:, 1:] - rd_k[:, :-1])[vx_valid])) if np.any(vx_valid) else 0.0
            jump_y = float(np.max(np.abs(rd_k[1:, :] - rd_k[:-1, :])[vy_valid])) if np.any(vy_valid) else 0.0
            max_jump = max(jump_x, jump_y)
            seam_metric = max(0.0, res_rms - 2.2) + 0.35 * max(0.0, max_jump - 5.5)
            if seam_metric > 0.0:
                plate_quality = max(0.15, 1.0 / (1.0 + (seam_metric / 3.0) ** 2))

        cutout_metas[k]["plate_quality"] = plate_quality

        # Pre-bake combined (16, 16, 3) additive offset field at 16x16 grid centers:
        # I_corr = a * I + ((1 - a) * y_mean + b + cx * gx + cy * gy + res_grid + color_delta)
        ak = float(a_final[k])
        scalar_offset_16 = (
            (1.0 - ak) * float(y_mean[k])
            + float(b[k])
            + float(cx[k]) * gx
            + float(cy[k]) * gy
            + res_grids[k]
        ).astype(np.float32)
        offset_16_rgb = scalar_offset_16[:, :, None] + color_deltas[k][None, None, :]

        harmonization[k] = {
            "a": ak,
            "offset_16": offset_16_rgb.astype(np.float32),
            "plate_quality": plate_quality,
        }

    return harmonization


def _build_low_band_64(arr_u8: np.ndarray) -> np.ndarray:
    """Build compact (64, 64, 3) float32 smoothed low-frequency sky band (~48 KB)."""
    ln = _LOW_BAND_N
    small = Image.fromarray(arr_u8, mode="RGB").resize((ln, ln), Image.Resampling.BOX)
    low = np.asarray(small, dtype=np.float32)
    for _ in range(2):
        py = np.pad(low, ((1, 1), (0, 0), (0, 0)), mode="edge")
        low = 0.25 * (py[:-2, :, :] + 2.0 * py[1:-1, :, :] + py[2:, :, :])
        px = np.pad(low, ((0, 0), (1, 1), (0, 0)), mode="edge")
        low = 0.25 * (px[:, :-2, :] + 2.0 * px[:, 1:-1, :] + px[:, 2:, :])
    return low.astype(np.float32, copy=False)


def _prepare_cutout_bands(
    arr: np.ndarray, harm: Optional[dict]
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Apply global spherical photometric harmonization to decoded cutout `arr` (H, W, 3)
    using fast separable 16x16->HxW upsampling with star-core roll-off protection,
    and return `(harmonized_u8, low_band_64_f32)`.
    """
    h, w = arr.shape[:2]
    if harm is not None and h >= 2 and w >= 2:
        g = _BG_GRID_N
        offset_16 = harm["offset_16"]  # (16, 16, 3)
        ak = np.float32(harm["a"])

        # Separable bilinear upsample (16, 16, 3) -> (16, W, 3) -> (H, W, 3)
        gx_f = np.clip(
            (np.arange(w, dtype=np.float32) + 0.5) * np.float32(g / float(w)) - 0.5,
            0.0,
            float(g - 1.0001),
        )
        ix0 = gx_f.astype(np.int32)
        wx = (gx_f - ix0)[None, :, None]
        row_interp = offset_16[:, ix0, :] + (offset_16[:, ix0 + 1, :] - offset_16[:, ix0, :]) * wx

        gy_f = np.clip(
            (np.arange(h, dtype=np.float32) + 0.5) * np.float32(g / float(h)) - 0.5,
            0.0,
            float(g - 1.0001),
        )
        iy0 = gy_f.astype(np.int32)
        wy = (gy_f - iy0)[:, None, None]
        offset_full = row_interp[iy0, :, :] + (row_interp[iy0 + 1, :, :] - row_interp[iy0, :, :]) * wy

        arr_f = arr.astype(np.float32)
        lum = np.mean(arr_f, axis=2, keepdims=True)
        delta = (ak - np.float32(1.0)) * arr_f + offset_full
        # Preserve crisp white saturated star cores (lum -> 250..255) without greying or harsh clipping
        star_taper = np.clip((np.float32(250.0) - lum) * np.float32(1.0 / 95.0), 0.0, 1.0)
        arr_harm = np.clip(np.rint(arr_f + delta * star_taper), 0.0, 255.0).astype(np.uint8)
    else:
        arr_harm = arr

    return arr_harm, _build_low_band_64(arr_harm)


def _compute_sector_cell_mask(
    sec: dict, xi_1d: np.ndarray, eta_1d: np.ndarray
) -> np.ndarray:
    """
    Compute exact spherical RA/Dec cell membership mask with a 1.3 px opaque pad
    plus a 2.2 px raised-cosine skirt outside the boundary for seamless source-over tiling.
    """
    basis = sec["basis"]
    T = basis[0].astype(np.float32)
    E = basis[1].astype(np.float32)
    N = basis[2].astype(np.float32)

    # R(v, u) = T + xi[u]*E + eta[v]*N -> shape (H, W, 3)
    rx = T[0] + eta_1d[:, None] * N[0] + xi_1d[None, :] * E[0]
    ry = T[1] + eta_1d[:, None] * N[1] + xi_1d[None, :] * E[1]
    rz = T[2] + eta_1d[:, None] * N[2] + xi_1d[None, :] * E[2]

    inv_len = 1.0 / np.sqrt(rx * rx + ry * ry + rz * rz)
    pz = np.clip(rz * inv_len, -1.0, 1.0)
    dec_rad = np.arcsin(pz)
    dec_deg = dec_rad * np.float32(RAD2DEG)

    pole = sec["pole"]
    if pole == 1:
        sd_deg = dec_deg - np.float32(90.0 - sec["d_step"])
    elif pole == -1:
        sd_deg = np.float32(-90.0 + sec["d_step"]) - dec_deg
    else:
        sd_dec = np.minimum(
            dec_deg - np.float32(sec["dec_min"]),
            np.float32(sec["dec_max"]) - dec_deg,
        )
        ra_deg = (np.arctan2(ry, rx) * np.float32(RAD2DEG)) % np.float32(360.0)
        d_ra = ((ra_deg - np.float32(sec["ra0"]) + np.float32(180.0)) % np.float32(360.0)) - np.float32(180.0)
        sd_ra = (np.float32(0.5 * sec["ra_step"]) - np.abs(d_ra)) * np.cos(dec_rad)
        sd_deg = np.minimum(sd_dec, sd_ra)

    px_deg = float(sec["span_deg"]) / float(sec["tile_px"])
    opaque_pad = np.float32(1.3 * px_deg)
    feather_w = np.float32(2.2 * px_deg)

    t = np.clip((sd_deg + opaque_pad + feather_w) / feather_w, 0.0, 1.0)
    mask = 0.5 - 0.5 * np.cos(t * np.float32(math.pi))

    # Enforce 1-pixel zero-alpha safety gutter at outer tile perimeter so packed sheet neighbors never bleed
    mask[0, :] = 0.0
    mask[-1, :] = 0.0
    mask[:, 0] = 0.0
    mask[:, -1] = 0.0
    return mask.astype(np.float32, copy=False)


def _render_sector_rgba(
    sec: dict,
    cutout_indices: List[int],
    cutout_metas: List[dict],
    decoded_images: Dict[int, np.ndarray],
    decoded_low_bands: Optional[Dict[int, np.ndarray]] = None,
) -> Optional[Image.Image]:
    """
    Composite all overlapping gnomonic TAN cutouts onto a single spherical sector tile.
    Uses tight projective homography bounding boxes and two-band spatial-frequency blending:
    - Low-frequency sky background blends across full cutout footprints via a plateau-free
      C^1 raised-cosine bell window (eliminating square cutout edges and corner vignetting).
    - High-frequency star/DSO detail blends sharply with detail_weight without ghosting.
    """
    tile_px = int(sec["tile_px"])
    half_tan_s = float(sec["half_tan"])
    inv_two_ht_s = 1.0 / (2.0 * half_tan_s)
    basis_s = sec["basis"]  # (3, 3)

    # Tangent coordinates for pixel centers u, v in 0..tile_px-1 (East is left -u, North is up -v)
    coords = (np.arange(tile_px, dtype=np.float32) + 0.5) / float(tile_px) - 0.5
    xi_1d = (-2.0 * half_tan_s * coords).astype(np.float32)
    eta_1d = xi_1d

    accum_low_rgb = np.zeros((tile_px, tile_px, 3), dtype=np.float32)
    accum_low_w = np.zeros((tile_px, tile_px), dtype=np.float32)
    accum_high_rgb = np.zeros((tile_px, tile_px, 3), dtype=np.float32)
    accum_high_w = np.zeros((tile_px, tile_px), dtype=np.float32)
    accum_cov = np.zeros((tile_px, tile_px), dtype=np.float32)
    any_contributed = False

    for c_idx in cutout_indices:
        arr = decoded_images.get(c_idx)
        if arr is None:
            continue
        meta = cutout_metas[c_idx]
        h_k, w_k = arr.shape[:2]
        if h_k < 2 or w_k < 2:
            continue

        low_arr = decoded_low_bands.get(c_idx) if decoded_low_bands is not None else None
        if low_arr is None:
            low_arr = _build_low_band_64(arr)
        lh_k, lw_k = low_arr.shape[:2]

        basis_k = meta["basis"]
        half_tan_k = meta["half_tan"]

        # 3x3 homography M mapping sector ray [1, xi_s, eta_s] -> cutout ray
        m = basis_k @ basis_s.T  # M_ij = basis_k[i] . basis_s[j]

        # Inverse homography M^T projects cutout's 4 corners (+-half_tan_k, +-half_tan_k) onto sector pixel bbox
        corners_k = np.array(
            [
                [1.0, 1.0, 1.0, 1.0],
                [-half_tan_k, half_tan_k, -half_tan_k, half_tan_k],
                [-half_tan_k, -half_tan_k, half_tan_k, half_tan_k],
            ],
            dtype=np.float64,
        )
        proj_s = m.T @ corners_k
        if np.any(proj_s[0] <= 1e-4):
            x0, y0, x1, y1 = 0, 0, tile_px, tile_px
        else:
            xi_c = proj_s[1] / proj_s[0]
            eta_c = proj_s[2] / proj_s[0]
            u_c = (0.5 - xi_c * inv_two_ht_s) * tile_px
            v_c = (0.5 - eta_c * inv_two_ht_s) * tile_px
            x0 = max(0, int(math.floor(float(np.min(u_c)))) - 1)
            x1 = min(tile_px, int(math.ceil(float(np.max(u_c)))) + 1)
            y0 = max(0, int(math.floor(float(np.min(v_c)))) - 1)
            y1 = min(tile_px, int(math.ceil(float(np.max(v_c)))) + 1)
            if x1 <= x0 or y1 <= y0:
                continue

        sub_xi = xi_1d[x0:x1]
        sub_eta = eta_1d[y0:y1]

        m00, m01, m02 = np.float32(m[0, 0]), np.float32(m[0, 1]), np.float32(m[0, 2])
        m10, m11, m12 = np.float32(m[1, 0]), np.float32(m[1, 1]), np.float32(m[1, 2])
        m20, m21, m22 = np.float32(m[2, 0]), np.float32(m[2, 1]), np.float32(m[2, 2])

        denom = (m00 + sub_eta[:, None] * m02) + sub_xi[None, :] * m01
        inv_denom = np.where(denom > 1e-5, 1.0 / np.maximum(denom, 1e-5), 0.0)

        xi_k = ((m10 + sub_eta[:, None] * m12) + sub_xi[None, :] * m11) * inv_denom
        eta_k = ((m20 + sub_eta[:, None] * m22) + sub_xi[None, :] * m21) * inv_denom

        inv_two_ht_k = np.float32(1.0 / (2.0 * half_tan_k))
        nx = np.float32(0.5) - xi_k * inv_two_ht_k
        ny = np.float32(0.5) - eta_k * inv_two_ht_k

        valid = (denom > 1e-5) & (nx > 0.0) & (nx < 1.0) & (ny > 0.0) & (ny < 1.0)
        if not np.any(valid):
            continue

        # Outer 8% raised-cosine coverage feather for opaque alpha footprint
        d_edge = np.minimum(np.minimum(nx, 1.0 - nx), np.minimum(ny, 1.0 - ny))
        cov_idx = np.clip(
            (d_edge * np.float32((_LUT_LEN - 1) / 0.08)).astype(np.int32),
            0,
            _LUT_LEN - 1,
        )
        w_cov = np.where(valid, _COS_LUT[cov_idx], 0.0)

        # Plateau-free full-cutout C^1 sine-squared bell for seamless low-frequency sky blending
        bx_idx = np.clip((nx * np.float32(_LUT_LEN - 1)).astype(np.int32), 0, _LUT_LEN - 1)
        by_idx = np.clip((ny * np.float32(_LUT_LEN - 1)).astype(np.int32), 0, _LUT_LEN - 1)
        w_bell = _BELL_LUT[bx_idx] * _BELL_LUT[by_idx]

        edge12_idx = np.clip(
            (d_edge * np.float32((_LUT_LEN - 1) / 0.12)).astype(np.int32),
            0,
            _LUT_LEN - 1,
        )
        w_edge12 = _COS_LUT[edge12_idx]

        plate_q = np.float32(meta.get("plate_quality", 1.0))
        detail_boost = np.float32(meta["detail_weight"]) * plate_q

        # Low band ignores detail_weight so catalog cutouts never stamp bright/dark squares onto SKYFILL
        w_low = np.where(valid, (w_bell + np.float32(1e-3) * w_cov) * plate_q, 0.0)
        w_high = np.where(
            valid,
            w_edge12 * (np.float32(0.20) + np.float32(0.80) * w_bell) * detail_boost,
            0.0,
        )

        # Vectorized bilinear texture sample on full-res arr (h_k, w_k, 3)
        uf = np.clip(nx * np.float32(w_k) - np.float32(0.5), 0.0, float(w_k - 1.0001))
        vf = np.clip(ny * np.float32(h_k) - np.float32(0.5), 0.0, float(h_k - 1.0001))
        u0 = uf.astype(np.int32)
        v0 = vf.astype(np.int32)
        du = (uf - u0)[..., None]
        dv = (vf - v0)[..., None]

        p00 = arr[v0, u0].astype(np.float32)
        p10 = arr[v0, u0 + 1].astype(np.float32)
        p01 = arr[v0 + 1, u0].astype(np.float32)
        p11 = arr[v0 + 1, u0 + 1].astype(np.float32)

        top = p00 + (p10 - p00) * du
        bot = p01 + (p11 - p01) * du
        sampled_full = top + (bot - top) * dv

        # Vectorized bilinear texture sample on compact low-pass grid low_arr (64, 64, 3)
        luf = np.clip(nx * np.float32(lw_k) - np.float32(0.5), 0.0, float(lw_k - 1.0001))
        lvf = np.clip(ny * np.float32(lh_k) - np.float32(0.5), 0.0, float(lh_k - 1.0001))
        lu0 = luf.astype(np.int32)
        lv0 = lvf.astype(np.int32)
        ldu = (luf - lu0)[..., None]
        ldv = (lvf - lv0)[..., None]

        lp00 = low_arr[lv0, lu0]
        lp10 = low_arr[lv0, lu0 + 1]
        lp01 = low_arr[lv0 + 1, lu0]
        lp11 = low_arr[lv0 + 1, lu0 + 1]

        ltop = lp00 + (lp10 - lp00) * ldu
        lbot = lp01 + (lp11 - lp01) * ldu
        sampled_low = ltop + (lbot - ltop) * ldv

        sampled_high = sampled_full - sampled_low

        accum_low_rgb[y0:y1, x0:x1] += sampled_low * w_low[..., None]
        accum_low_w[y0:y1, x0:x1] += w_low
        accum_high_rgb[y0:y1, x0:x1] += sampled_high * w_high[..., None]
        accum_high_w[y0:y1, x0:x1] += w_high
        accum_cov[y0:y1, x0:x1] += w_cov
        any_contributed = True

    if not any_contributed:
        return None

    cell_mask = _compute_sector_cell_mask(sec, xi_1d, eta_1d)
    survey_cov = np.clip(accum_cov, 0.0, 1.0)
    final_alpha_f = survey_cov * cell_mask
    alpha_u8 = np.rint(final_alpha_f * 255.0).astype(np.uint8)

    if not np.any(alpha_u8):
        return None

    low_blend = accum_low_rgb / np.maximum(accum_low_w, 1e-8)[..., None]
    high_blend = accum_high_rgb / np.maximum(accum_high_w, 1e-8)[..., None]
    mosaic_rgb = np.clip(low_blend + high_blend, 0.0, 255.0)

    # Apply night-sky screen tone grade once onto opaque dark backing
    graded_rgb = _SKY_BASE_RGB + mosaic_rgb * _SKY_SCREEN_GAIN
    rgb_u8 = np.clip(np.rint(graded_rgb), 0.0, 255.0).astype(np.uint8)

    # Uniform night-sky RGB in fully transparent blocks prevents WebP chroma ringing at alpha borders
    zero_mask = alpha_u8 == 0
    rgb_u8[zero_mask] = _SKY_BASE_RGB.astype(np.uint8)

    rgba = np.dstack([rgb_u8, alpha_u8])
    return Image.fromarray(rgba, mode="RGBA")


def _pack_sectors_into_sheets(
    rendered_sectors: List[Tuple[dict, Image.Image]],
    tile_px: int,
    sheet_size: int,
    quality: int,
    lod_level: int,
    start_sheet_index: int,
    max_workers: int,
) -> Tuple[List[dict], List[dict], List[Tuple[str, bytes]]]:
    """Pack rendered RGBA sector tiles into 4096x4096 WebP sheets in parallel."""
    if not rendered_sectors:
        return [], [], []

    cols = max(1, int(sheet_size) // int(tile_px))
    sheet_px_w = cols * tile_px
    tiles_per_sheet = cols * cols
    num_sheets = math.ceil(len(rendered_sectors) / tiles_per_sheet)

    sheet_jobs: List[Tuple[int, str, Image.Image, int]] = []
    sector_manifest_entries: List[dict] = []

    for local_s in range(num_sheets):
        global_s = start_sheet_index + local_s
        batch = rendered_sectors[
            local_s * tiles_per_sheet : (local_s + 1) * tiles_per_sheet
        ]
        rows_used = max(1, math.ceil(len(batch) / cols))
        sheet_px_h = rows_used * tile_px
        sheet_im = Image.new("RGBA", (sheet_px_w, sheet_px_h), (5, 9, 18, 0))

        if lod_level == 0:
            sheet_filename = (
                "sky_atlas_lod0.webp"
                if num_sheets == 1
                else f"sky_atlas_lod0_{local_s}.webp"
            )
        else:
            sheet_filename = f"sky_atlas_lod{lod_level}_{local_s}.webp"

        for i, (sec, tile_im) in enumerate(batch):
            col = i % cols
            row = i // cols
            u = col * tile_px
            v = row * tile_px
            sheet_im.paste(tile_im, (u, v))
            try:
                tile_im.close()
            except Exception:
                pass

            sector_manifest_entries.append(
                [
                    round(float(sec["ra0"]), 4),
                    round(float(sec["dec0"]), 4),
                    round(float(sec["span_deg"]), 4),
                    global_s,
                    u,
                    v,
                ]
            )

        sheet_jobs.append((global_s, sheet_filename, sheet_im, len(batch)))

    def _encode_job(
        job: Tuple[int, str, Image.Image, int],
    ) -> Tuple[dict, Tuple[str, bytes]]:
        global_s, fname, s_im, tile_cnt = job
        w, h = s_im.size
        buf = io.BytesIO()
        s_im.save(
            buf,
            format="WEBP",
            quality=quality,
            alpha_quality=min(92, quality + 6),
            method=4,
        )
        s_im.close()
        webp_bytes = buf.getvalue()
        meta = {
            "index": global_s,
            "lod": lod_level,
            "file": fname,
            "width": w,
            "height": h,
            "tiles": tile_cnt,
            "bytes": len(webp_bytes),
        }
        return meta, (fname, webp_bytes)

    sheets_meta: List[dict] = []
    encoded_sheets: List[Tuple[str, bytes]] = []
    with ThreadPoolExecutor(max_workers=min(max_workers, len(sheet_jobs))) as pool:
        for meta, enc in pool.map(_encode_job, sheet_jobs):
            sheets_meta.append(meta)
            encoded_sheets.append(enc)

    return sector_manifest_entries, sheets_meta, encoded_sheets


def _atomic_or_direct_write(out_path: str, payload: bytes) -> None:
    """Write atomically via .tmp rename, falling back to direct write on SMB/CIFS shares."""
    tmp_path = out_path + ".tmp"
    try:
        with open(tmp_path, "wb") as f:
            f.write(payload)
        os.replace(tmp_path, out_path)
    except OSError:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        with open(out_path, "wb") as f:
            f.write(payload)


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
        _atomic_or_direct_write(out_path, webp_bytes)

    notice_path = resolve_safe_dir_file(target_dir, LICENSE_NOTICE_FILENAME)
    _atomic_or_direct_write(notice_path, DSS_ASSET_NOTICE_MARKDOWN.encode("utf-8"))

    manifest_path = resolve_safe_dir_file(target_dir, MANIFEST_FILENAME)
    _atomic_or_direct_write(manifest_path, manifest_bytes)

    # Remove stale sheet files from older builds
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


def normalize_cache_dir_and_setup(cache_dir: str) -> Tuple[str, Optional[str]]:
    """
    Accept either a root cache directory (`.../image_cache`) or a direct setup
    subdirectory (`.../image_cache/fov_2.15_1.44_p1.50_r1024_dss2r`).
    Returns `(root_cache_dir, setup_filter)`.
    """
    abs_dir = os.path.abspath(os.path.normpath(cache_dir))
    base_name = os.path.basename(abs_dir)
    parent_dir = os.path.dirname(abs_dir)

    if os.path.isdir(abs_dir) and parent_dir and parent_dir != abs_dir:
        parsed_fov, _ = parse_setup_hash_fov(base_name)
        if parsed_fov is not None or base_name.startswith("custom_"):
            return parent_dir, base_name
        # Fallback: check if directory directly holds .jpg/.jpeg cutouts without setup subfolders
        try:
            has_direct_jpg = False
            has_setup_subdir = False
            with os.scandir(abs_dir) as it:
                for d in it:
                    if d.name.lower().endswith((".jpg", ".jpeg")) and d.is_file(
                        follow_symlinks=False
                    ):
                        has_direct_jpg = True
                        break
                    if (
                        d.name != ATLAS_CACHE_SUBDIR
                        and d.is_dir(follow_symlinks=False)
                        and parse_setup_hash_fov(d.name)[0] is not None
                    ):
                        has_setup_subdir = True
            if has_direct_jpg and not has_setup_subdir:
                return parent_dir, base_name
        except OSError:
            pass

    return abs_dir, None


def build_sky_atlas(
    cache_dir: str = CACHE_DIR,
    output_dir: Optional[str] = None,
    mirror_dirs: Optional[List[str]] = None,
    extra_cache_dirs: Optional[List[str]] = None,
    settings: Optional[dict] = None,
    active_setup_hash: Optional[str] = None,
    fallback_fov: float = 1.0,
    lod0_tile_size: int = DEFAULT_LOD0_TILE_SIZE,
    lod0_step_deg: float = DEFAULT_LOD0_STEP_DEG,
    lod1_tile_size: int = DEFAULT_LOD1_TILE_SIZE,
    lod1_step_deg: float = DEFAULT_LOD1_STEP_DEG,
    lod2_tile_size: int = DEFAULT_LOD2_TILE_SIZE,
    lod2_step_deg: float = DEFAULT_LOD2_STEP_DEG,
    sheet_size: int = DEFAULT_SHEET_SIZE,
    quality: int = DEFAULT_WEBP_QUALITY,
    include_custom: bool = False,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> dict:
    """
    Build a 3-tier multi-resolution pre-composited celestial sector map (LOD 0 + LOD 1 + LOD 2)
    plus a compact individual target footprint table from downloaded sky survey cutouts.

    - LOD 0 (~12.85 deg sectors, 256x256 px): fits the entire sky onto a single 4096x4096 WebP sheet
      for instant, overlap-free wide-sky rendering.
    - LOD 1 (~6.0 deg sectors, 512x512 px): regional 4096x4096 WebP sheets for crisp mid-field zoom.
    - LOD 2 (~3.0 deg sectors, 512x512 px): high-resolution regional 4096x4096 WebP sheets for sharp
      close-up Sky Map zooming and instant Framing previews.
    """
    lod0_tile_size = max(64, min(512, int(lod0_tile_size)))
    lod1_tile_size = max(128, min(1024, int(lod1_tile_size)))
    enable_lod2 = int(lod2_tile_size or 0) > 0 and float(lod2_step_deg or 0.0) > 0.0
    if enable_lod2:
        lod2_tile_size = max(128, min(1024, int(lod2_tile_size)))
    max_tile_px = max(lod0_tile_size, lod1_tile_size, lod2_tile_size if enable_lod2 else 0)
    sheet_size = max(max_tile_px, int(sheet_size))
    quality = max(40, min(95, int(quality)))

    if output_dir is None:
        output_dir = os.path.abspath(get_resource_path("frontend/public/sky_atlas"))
    else:
        output_dir = os.path.abspath(output_dir)

    root_cache_dir, setup_filter = normalize_cache_dir_and_setup(cache_dir)
    if setup_filter and not active_setup_hash:
        active_setup_hash = setup_filter

    cache_sources: List[Tuple[str, Optional[str]]] = [(root_cache_dir, setup_filter)]
    seen_sources = {
        (os.path.normcase(root_cache_dir), setup_filter or "")
    }
    for extra_raw in extra_cache_dirs or []:
        if not extra_raw:
            continue
        ext_root, ext_filter = normalize_cache_dir_and_setup(extra_raw)
        src_key = (os.path.normcase(ext_root), ext_filter or "")
        if src_key not in seen_sources:
            seen_sources.add(src_key)
            cache_sources.append((ext_root, ext_filter))

    validated_setup_dirs: Dict[Tuple[str, str], str] = {}
    best_by_key: Dict[str, Tuple[dict, str]] = {}

    for src_root, src_filter in cache_sources:
        src_items = list_cached_sky_images(
            cache_dir=src_root,
            settings=settings,
            active_setup_hash=active_setup_hash,
            fallback_fov=fallback_fov,
            setup_filter=src_filter,
        )
        if src_filter and os.path.isdir(src_root):
            for sib in sorted(os.listdir(src_root)):
                if sib == src_filter or not sib.startswith("fov_custom_"):
                    continue
                sib_items = list_cached_sky_images(
                    cache_dir=src_root,
                    settings=settings,
                    active_setup_hash=active_setup_hash,
                    fallback_fov=fallback_fov,
                    setup_filter=sib,
                )
                src_items.extend(it for it in sib_items if it.get("is_sky_fill"))

        src_root_norm = os.path.normcase(src_root)
        for item in src_items:
            if item.get("is_custom") and not include_custom:
                continue
            url = str(item.get("url") or "")
            if not url.startswith("/cache/"):
                continue
            rel_parts = url[len("/cache/") :].split("/")
            if len(rel_parts) != 2:
                continue
            setup_part, fname_part = rel_parts
            if "/" in fname_part or "\\" in fname_part or fname_part in ("", ".", ".."):
                continue
            cache_key = (src_root_norm, setup_part)
            setup_abs = validated_setup_dirs.get(cache_key)
            if setup_abs is None:
                try:
                    setup_abs = resolve_safe_cache_path(src_root, setup_part)
                except ValueError:
                    continue
                validated_setup_dirs[cache_key] = setup_abs
            filepath = os.path.join(setup_abs, fname_part)
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

    # Prepare geometric metadata for all unique cached cutouts
    cutout_metas: List[dict] = []
    for k in sorted(best_by_key.keys()):
        item, fpath = best_by_key[k]
        ra = float(item["ra"]) % 360.0
        dec = max(-90.0, min(90.0, float(item["dec"])))
        fov = max(0.15, min(25.0, float(item["fov"])))
        basis = _make_tangent_basis(ra, dec)
        half_tan = math.tan(0.5 * fov * DEG2RAD)
        cone_rad = math.atan(math.sqrt(2.0) * half_tan)
        # Slightly higher weight for narrower-FOV and catalog-target cutouts so fine detail wins inside overlaps
        detail_weight = min(2.5, max(1.0, 2.0 / max(fov, 0.5)))
        if not item.get("is_sky_fill"):
            detail_weight *= 1.15
        cutout_metas.append(
            {
                "filepath": fpath,
                "ra": ra,
                "dec": dec,
                "fov": fov,
                "basis": basis,
                "half_tan": half_tan,
                "cone_rad": cone_rad,
                "detail_weight": detail_weight,
            }
        )

    source_count = len(cutout_metas)
    if source_count == 0:
        return {
            "status": "empty",
            "version": 2,
            "source_count": 0,
            "count": 0,
            "lod0_sectors": 0,
            "lod1_sectors": 0,
            "lod2_sectors": 0,
            "sheets": 0,
            "lod0_sheets": 0,
            "lod1_sheets": 0,
            "lod2_sheets": 0,
            "size_mb": 0.0,
            "generated_at": None,
            "written_dirs": [],
        }

    if progress_callback:
        scope_msg = f" (setup: {setup_filter})" if setup_filter else ""
        progress_callback(
            f"Indexed {source_count} unique survey cutouts from {root_cache_dir}{scope_msg}..."
        )

    max_workers = min(16, max(4, (os.cpu_count() or 4) * 2))

    # Global spherical photometric harmonization across overlapping cutouts
    cutout_harmonization: List[Optional[dict]] = [None] * source_count
    if source_count >= 2:
        bg_grids, bg_valid = _load_or_extract_bg_grids(
            cutout_metas=cutout_metas,
            root_cache_dir=root_cache_dir,
            max_workers=max_workers,
            progress_callback=progress_callback,
        )
        if progress_callback and source_count >= 64:
            progress_callback(
                f"  Solving global spherical photometric harmonization across {source_count} cutouts..."
            )
        cutout_harmonization = _compute_cutout_harmonization(
            cutout_metas=cutout_metas,
            bg_grids=bg_grids,
            valid_mask=bg_valid,
        )
        del bg_grids, bg_valid

    # Match source JPEG decode scale to finest active LOD angular density
    finest_px_per_deg = max(
        lod0_tile_size / max(lod0_step_deg, 0.5),
        lod1_tile_size / max(lod1_step_deg, 0.5),
        (lod2_tile_size / max(lod2_step_deg, 0.5)) if enable_lod2 else 0.0,
    )
    max_px_per_deg = max(80.0, finest_px_per_deg * 1.12)

    rendered_by_lod: Dict[int, List[Tuple[dict, Image.Image]]] = {0: [], 1: [], 2: []}

    lod0_grid = _generate_sector_grid(lod0_step_deg, lod0_tile_size)
    lod1_grid = _generate_sector_grid(lod1_step_deg, lod1_tile_size)
    lod2_grid = _generate_sector_grid(lod2_step_deg, lod2_tile_size) if enable_lod2 else []

    cutout_t = np.array([m["basis"][0] for m in cutout_metas], dtype=np.float64)
    cutout_cones = np.array([m["cone_rad"] for m in cutout_metas], dtype=np.float64)

    def _match_sectors(grid: List[dict]) -> List[Tuple[dict, List[int]]]:
        if not grid:
            return []
        sec_t = np.array([s["basis"][0] for s in grid], dtype=np.float64)
        sec_cones = np.array([s["cone_rad"] for s in grid], dtype=np.float64)
        dots = sec_t @ cutout_t.T  # (S, N)
        thresh = np.cos(
            np.minimum(math.pi, sec_cones[:, None] + cutout_cones[None, :] + 0.004)
        )
        mask = dots >= thresh
        matched: List[Tuple[dict, List[int]]] = []
        for s_idx, sec in enumerate(grid):
            c_ids = np.flatnonzero(mask[s_idx]).tolist()
            if c_ids:
                matched.append((sec, c_ids))
        return matched

    active_lod0 = _match_sectors(lod0_grid)
    active_lod1 = _match_sectors(lod1_grid)
    active_lod2 = _match_sectors(lod2_grid) if enable_lod2 else []

    # Group all LODs into 18 Declination sweep strips (10 deg each, -90..+90) so each JPEG is
    # decoded once and evicted immediately once the sweep passes north of all sectors referencing it.
    num_strips = 18
    strip_span_deg = 180.0 / num_strips
    strip_tasks: List[List[Tuple[int, dict, List[int]]]] = [
        [] for _ in range(num_strips)
    ]
    last_strip_for_cutout = np.full(source_count, -1, dtype=np.int32)

    for lod_lvl, active_list in ((0, active_lod0), (1, active_lod1), (2, active_lod2)):
        for sec, c_ids in active_list:
            s_bin = min(
                num_strips - 1,
                max(0, int((sec["dec0"] + 90.0) / strip_span_deg)),
            )
            strip_tasks[s_bin].append((lod_lvl, sec, c_ids))
            for cid in c_ids:
                if s_bin > last_strip_for_cutout[cid]:
                    last_strip_for_cutout[cid] = s_bin

    decoded_images: Dict[int, np.ndarray] = {}
    decoded_low_bands: Dict[int, np.ndarray] = {}

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for s_bin in range(num_strips):
            tasks = strip_tasks[s_bin]
            if not tasks:
                continue

            dec_lo = -90.0 + s_bin * strip_span_deg
            dec_hi = dec_lo + strip_span_deg
            needed_new = sorted(
                {
                    cid
                    for _, _, c_ids in tasks
                    for cid in c_ids
                    if cid not in decoded_images
                }
            )
            if progress_callback:
                progress_callback(
                    f"  [Strip {s_bin + 1:2d}/{num_strips} | Dec {dec_lo:+5.1f}°..{dec_hi:+5.1f}°] "
                    f"decoding {len(needed_new)} cutouts, compositing {len(tasks)} sectors..."
                )

            if needed_new:

                def _decode_one(
                    cid: int,
                ) -> Tuple[int, Optional[np.ndarray], Optional[np.ndarray]]:
                    m = cutout_metas[cid]
                    raw_arr = _load_cutout_array(
                        m["filepath"], m["fov"], max_px_per_deg=max_px_per_deg
                    )
                    if raw_arr is None:
                        return cid, None, None
                    harm_arr, low_64 = _prepare_cutout_bands(
                        raw_arr, cutout_harmonization[cid]
                    )
                    return cid, harm_arr, low_64

                for cid, arr, low_64 in pool.map(_decode_one, needed_new):
                    if arr is not None and low_64 is not None:
                        decoded_images[cid] = arr
                        decoded_low_bands[cid] = low_64

            def _render_task(
                task: Tuple[int, dict, List[int]],
            ) -> Tuple[int, dict, Optional[Image.Image]]:
                lod_lvl, sec, c_ids = task
                im = _render_sector_rgba(
                    sec, c_ids, cutout_metas, decoded_images, decoded_low_bands
                )
                return lod_lvl, sec, im

            for lod_lvl, sec, im in pool.map(_render_task, tasks):
                if im is not None:
                    rendered_by_lod[lod_lvl].append((sec, im))

            # Evict cutouts no longer needed by any subsequent northern declination strip
            to_evict = [
                cid
                for cid in list(decoded_images.keys())
                if last_strip_for_cutout[cid] <= s_bin
            ]
            for cid in to_evict:
                decoded_images.pop(cid, None)
                decoded_low_bands.pop(cid, None)

    rendered_lod0 = rendered_by_lod[0]
    rendered_lod1 = rendered_by_lod[1]
    rendered_lod2 = rendered_by_lod[2]

    # Sort LOD 0 South-to-North, LOD 1 by 36x36 deg sky region, and LOD 2 by 18x18 deg sky region
    rendered_lod0.sort(key=lambda pair: (pair[0]["b"], pair[0]["c"]))
    rendered_lod1.sort(
        key=lambda pair: (
            int((pair[0]["dec0"] + 90.0) // 36.0),
            int((pair[0]["ra0"] % 360.0) // 36.0),
            pair[0]["b"],
            pair[0]["c"],
        )
    )
    rendered_lod2.sort(
        key=lambda pair: (
            int((pair[0]["dec0"] + 90.0) // 18.0),
            int((pair[0]["ra0"] % 360.0) // 18.0),
            pair[0]["b"],
            pair[0]["c"],
        )
    )

    lod0_entries, lod0_sheets, lod0_encoded = _pack_sectors_into_sheets(
        rendered_sectors=rendered_lod0,
        tile_px=lod0_tile_size,
        sheet_size=sheet_size,
        quality=quality,
        lod_level=0,
        start_sheet_index=0,
        max_workers=max_workers,
    )
    lod1_entries, lod1_sheets, lod1_encoded = _pack_sectors_into_sheets(
        rendered_sectors=rendered_lod1,
        tile_px=lod1_tile_size,
        sheet_size=sheet_size,
        quality=quality,
        lod_level=1,
        start_sheet_index=len(lod0_sheets),
        max_workers=max_workers,
    )
    lod2_entries, lod2_sheets, lod2_encoded = (
        _pack_sectors_into_sheets(
            rendered_sectors=rendered_lod2,
            tile_px=lod2_tile_size,
            sheet_size=sheet_size,
            quality=quality,
            lod_level=2,
            start_sheet_index=len(lod0_sheets) + len(lod1_sheets),
            max_workers=max_workers,
        )
        if enable_lod2 and rendered_lod2
        else ([], [], [])
    )

    all_sheets = lod0_sheets + lod1_sheets + lod2_sheets
    all_encoded = lod0_encoded + lod1_encoded + lod2_encoded

    lod0_node = {
        "lod": 0,
        "tile_size": lod0_tile_size,
        "step_deg": lod0_step_deg,
        "sectors": lod0_entries,
    }
    lod1_node = {
        "lod": 1,
        "tile_size": lod1_tile_size,
        "step_deg": lod1_step_deg,
        "sectors": lod1_entries,
    }
    has_lod2 = bool(enable_lod2 and lod2_entries)

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest_payload = {
        "version": 2,
        "mode": "sectors",
        "lod_count": 3 if has_lod2 else 2,
        "generated_at": generated_at,
        "license": ATLAS_LICENSE,
        "attribution": ATLAS_ATTRIBUTION,
        "source_count": source_count,
        "sheets": all_sheets,
        "lod0": lod0_node,
        "lod1": lod1_node,
    }
    if has_lod2:
        manifest_payload["lod2"] = {
            "lod": 2,
            "tile_size": lod2_tile_size,
            "step_deg": lod2_step_deg,
            "sectors": lod2_entries,
        }

    manifest_bytes = json.dumps(
        manifest_payload, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")

    written_dirs: List[str] = []
    _write_atlas_directory(output_dir, manifest_bytes, all_encoded)
    written_dirs.append(output_dir)

    for mdir in mirror_dirs or []:
        abs_m = os.path.abspath(mdir)
        if os.path.normcase(abs_m) == os.path.normcase(output_dir):
            continue
        try:
            _write_atlas_directory(abs_m, manifest_bytes, all_encoded)
            written_dirs.append(abs_m)
        except Exception:
            pass

    total_bytes = len(manifest_bytes) + sum(len(b) for _, b in all_encoded)
    return {
        "status": "ok",
        "version": 2,
        "source_count": source_count,
        "count": source_count,
        "lod0_sectors": len(lod0_entries),
        "lod1_sectors": len(lod1_entries),
        "lod2_sectors": len(lod2_entries),
        "sheets": len(all_sheets),
        "lod0_sheets": len(lod0_sheets),
        "lod1_sheets": len(lod1_sheets),
        "lod2_sheets": len(lod2_sheets),
        "size_mb": round(total_bytes / (1024 * 1024), 2),
        "generated_at": generated_at,
        "written_dirs": written_dirs,
    }
