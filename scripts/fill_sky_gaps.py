#!/usr/bin/env python3
"""
Download missing sky survey tiles to eliminate black/empty patches in the Sky View panels,
using the exact same setup FOV, resolution, and survey source as your existing target cutouts,
and rebuild the pre-composited multi-LOD Sky Atlas (`frontend/public/sky_atlas/`).

Why this script exists:
  Astronomical catalogs (~13k DSOs) cluster along the Milky Way and major galaxy groups,
  leaving ~40% of the celestial sphere empty of catalog targets. Because there are no targets
  in those empty voids, the app cannot queue them via target lists. Run this developer script
  once against your `image_cache` directory to fill every uncovered patch of the 360°×180°
  sky and produce a 100% seamless sky survey map.

Usage:
    python scripts/fill_sky_gaps.py
    python scripts/fill_sky_gaps.py --cache-dir ./image_cache --dry-run
    python scripts/fill_sky_gaps.py --cache-dir "D:/00_SkyCache"
"""

import argparse
import asyncio
import heapq
import math
import os
import sys
import time
from typing import Dict, List, Optional, Tuple

import httpx
import numpy as np

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from backend.astro_utils import auto_stretch_image
from backend.config import CACHE_DIR, load_settings
from backend.image_service import (
    ALLOWED_IMAGE_SOURCES,
    _is_cached_image_file,
    get_cache_info,
    list_cached_sky_images,
    parse_setup_hash_details,
    resolve_safe_cache_path,
    validate_image_source,
)
from backend.models import Camera, Telescope
from backend.sky_atlas import (
    ATLAS_CACHE_SUBDIR,
    DEFAULT_LOD0_STEP_DEG,
    DEFAULT_LOD0_TILE_SIZE,
    DEFAULT_LOD1_STEP_DEG,
    DEFAULT_LOD1_TILE_SIZE,
    DEFAULT_LOD2_STEP_DEG,
    DEFAULT_LOD2_TILE_SIZE,
    DEFAULT_SHEET_SIZE,
    DEFAULT_WEBP_QUALITY,
    DEG2RAD,
    RAD2DEG,
    _make_tangent_basis,
    build_sky_atlas,
    normalize_cache_dir_and_setup,
    resolve_active_atlas_dir,
)
from backend.stream_service import compute_fov_and_setup

SKYVIEW_CGI_URL = "https://skyview.gsfc.nasa.gov/current/cgi/runquery.pl"
FEATHER_EDGE_FRAC = 0.08
COVERAGE_TARGET = 0.99


class FatalDiskWriteError(OSError):
    """Raised when saving a downloaded tile fails due to local/SMB filesystem permissions or space."""


def _format_skyfill_setup_hash(fov: float, resolution: int, source: str) -> str:
    """Format a deterministic custom setup folder name preserving exact 4-decimal FOV."""
    clean_src = validate_image_source(str(source).lower())
    return f"fov_custom_{max(0.25, float(fov)):.4f}_r{int(resolution)}_{clean_src}"


def detect_target_setup(
    cache_dir: str,
    settings: Optional[dict] = None,
    setup_override: Optional[str] = None,
    fov_override: Optional[float] = None,
    resolution_override: Optional[int] = None,
    source_override: Optional[str] = None,
) -> dict:
    """
    Auto-detect the target setup folder, download FOV, resolution, and survey source
    matching the user's existing downloaded target cutouts (falling back to settings).
    """
    settings = settings if isinstance(settings, dict) else {}
    root_cache_dir, setup_filter = normalize_cache_dir_and_setup(cache_dir)

    active_setup_hash: Optional[str] = None
    settings_fov = 1.5
    settings_res = int(settings.get("survey_resolution") or 1024)
    settings_source = str(settings.get("image_source") or "dss2r").lower()
    if settings_source not in ALLOWED_IMAGE_SOURCES:
        settings_source = "dss2r"

    try:
        telescope = Telescope(**settings.get("telescope", {"focal_length": 400}))
        camera = Camera(
            **settings.get("camera", {"sensor_width": 23.5, "sensor_height": 15.6})
        )
        fov_info = compute_fov_and_setup(telescope, camera, settings)
        active_setup_hash = fov_info.get("setup_hash")
        settings_fov = float(fov_info.get("default_download_fov") or settings_fov)
        _, parsed_res, parsed_src = parse_setup_hash_details(active_setup_hash or "")
        if parsed_src in ALLOWED_IMAGE_SOURCES:
            settings_res = parsed_res
            settings_source = parsed_src
    except Exception:
        pass

    # Count cached files per valid setup folder in root_cache_dir
    folder_counts: Dict[str, int] = {}
    if os.path.isdir(root_cache_dir):
        try:
            entries = sorted(os.listdir(root_cache_dir))
        except OSError:
            entries = []
        for entry in entries:
            if not entry or entry == ATLAS_CACHE_SUBDIR:
                continue
            p_fov, _, p_src = parse_setup_hash_details(entry)
            if p_fov is None or p_src not in ALLOWED_IMAGE_SOURCES:
                continue
            try:
                sdir = resolve_safe_cache_path(root_cache_dir, entry)
                if not os.path.isdir(sdir):
                    continue
                cnt = 0
                with os.scandir(sdir) as it:
                    for d in it:
                        if (
                            _is_cached_image_file(d.name)
                            and not d.is_symlink()
                            and d.is_file(follow_symlinks=False)
                        ):
                            cnt += 1
                folder_counts[entry] = cnt
            except (OSError, ValueError):
                continue

    chosen_base_setup: Optional[str] = None
    detection_reason = "settings"

    if setup_override:
        chosen_base_setup = setup_override.strip()
        detection_reason = "explicit --setup"
    elif setup_filter:
        chosen_base_setup = setup_filter
        detection_reason = "direct setup directory"
    elif folder_counts:
        # Prefer standard equipment setup folders over fov_custom_* gap-fill sibling folders
        non_custom_populated = [
            h
            for h, cnt in folder_counts.items()
            if cnt > 0 and not h.startswith("fov_custom_")
        ]
        candidate_keys = (
            non_custom_populated if non_custom_populated else list(folder_counts.keys())
        )
        chosen_base_setup = max(
            candidate_keys,
            key=lambda h: (folder_counts[h], 1 if h == active_setup_hash else 0, h),
        )
        if folder_counts[chosen_base_setup] > 0:
            detection_reason = (
                f"existing cache ({folder_counts[chosen_base_setup]} cutouts)"
            )
        elif active_setup_hash:
            chosen_base_setup = active_setup_hash
            detection_reason = "active equipment settings"
    elif active_setup_hash:
        chosen_base_setup = active_setup_hash
        detection_reason = "active equipment settings"

    base_fov, base_res, base_src = (
        parse_setup_hash_details(chosen_base_setup)
        if chosen_base_setup
        else (None, settings_res, settings_source)
    )

    eff_fov = max(
        0.25,
        float(
            fov_override
            if fov_override is not None
            else (base_fov if base_fov is not None else settings_fov)
        ),
    )
    eff_res = int(
        resolution_override
        if resolution_override is not None
        else (base_res if base_fov is not None else settings_res)
    )
    raw_src = (
        source_override
        if source_override is not None
        else (base_src if base_src in ALLOWED_IMAGE_SOURCES else settings_source)
    )
    eff_src = validate_image_source(str(raw_src).lower())

    # Preserve the exact existing setup folder whenever FOV/res/source match it
    if (
        chosen_base_setup
        and base_fov is not None
        and abs(eff_fov - base_fov) < 0.01
        and eff_res == base_res
        and eff_src == base_src
    ):
        target_setup_hash = chosen_base_setup
    else:
        target_setup_hash = _format_skyfill_setup_hash(eff_fov, eff_res, eff_src)

    return {
        "root_cache_dir": root_cache_dir,
        "setup_filter": setup_filter,
        "active_setup_hash": active_setup_hash,
        "target_setup_hash": target_setup_hash,
        "fov": round(eff_fov, 4),
        "resolution": eff_res,
        "source": eff_src,
        "detection_reason": detection_reason,
        "folder_counts": folder_counts,
    }


def _build_spherical_sample_grid(
    step_deg: float,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, float]:
    """
    Build a quasi-uniform spherical grid of unit vectors ordered South-to-North,
    plus 1-degree Declination band offsets for O(local) spatial slicing.

    Returns:
        pts_xyz   : float32 array of shape (N, 3)
        pts_ra    : float32 array of shape (N,) in [0, 360)
        pts_dec   : float32 array of shape (N,) in [-90, 90]
        band_offs : int32 array of shape (181,) indexing 1-deg strips [-90..+90]
        d_step    : actual declination ring spacing in degrees
    """
    n_dec = max(12, int(math.ceil(180.0 / max(0.05, float(step_deg)))))
    d_step = 180.0 / n_dec

    ra_chunks: List[np.ndarray] = [np.array([0.0], dtype=np.float32)]
    dec_chunks: List[np.ndarray] = [np.array([-90.0], dtype=np.float32)]

    for b in range(n_dec):
        dec0 = -90.0 + (b + 0.5) * d_step
        cos_d = max(1e-4, math.cos(dec0 * DEG2RAD))
        n_ra = max(8, int(math.ceil(360.0 * cos_d / d_step)))
        ra_step = 360.0 / n_ra
        offset = 0.5 * ra_step if (b & 1) else 0.0
        ras = ((np.arange(n_ra, dtype=np.float32) * ra_step) + offset) % 360.0
        decs = np.full(n_ra, dec0, dtype=np.float32)
        ra_chunks.append(ras)
        dec_chunks.append(decs)

    ra_chunks.append(np.array([0.0], dtype=np.float32))
    dec_chunks.append(np.array([90.0], dtype=np.float32))

    pts_ra = np.concatenate(ra_chunks)
    pts_dec = np.concatenate(dec_chunks)

    # Guarantee monotonic non-decreasing declination for binary/band slicing
    order = np.argsort(pts_dec, kind="mergesort")
    pts_ra = pts_ra[order]
    pts_dec = pts_dec[order]

    ra_rad = pts_ra * np.float32(DEG2RAD)
    dec_rad = pts_dec * np.float32(DEG2RAD)
    cd = np.cos(dec_rad)
    pts_xyz = np.column_stack(
        [cd * np.cos(ra_rad), cd * np.sin(ra_rad), np.sin(dec_rad)]
    ).astype(np.float32)

    edges = np.linspace(-90.0, 90.0, 181, dtype=np.float32)
    band_offs = np.searchsorted(pts_dec, edges, side="left").astype(np.int32)
    band_offs[-1] = len(pts_dec)

    return pts_xyz, pts_ra, pts_dec, band_offs, d_step


def _eval_tile_weights_on_slice(
    ra_deg: float,
    dec_deg: float,
    fov_deg: float,
    pts_xyz: np.ndarray,
    band_offs: np.ndarray,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Evaluate exact gnomonic TAN + 8% raised-cosine perimeter feather weights
    (matching `backend/sky_atlas.py`) on the local Declination slice around `(ra_deg, dec_deg)`.
    Returns `(global_point_indices, weights)`.
    """
    basis = _make_tangent_basis(ra_deg, dec_deg).astype(np.float32)
    half_tan = float(math.tan(0.5 * fov_deg * DEG2RAD))
    cone_rad = float(math.atan(math.sqrt(2.0) * half_tan))
    cone_deg = cone_rad * RAD2DEG

    b_lo = max(0, int(math.floor(dec_deg - cone_deg + 90.0)) - 1)
    b_hi = min(180, int(math.ceil(dec_deg + cone_deg + 90.0)) + 1)
    i0 = int(band_offs[b_lo])
    i1 = int(band_offs[b_hi])
    if i1 <= i0:
        return np.empty(0, dtype=np.int32), np.empty(0, dtype=np.float32)

    sub = pts_xyz[i0:i1]
    cos_thresh = np.float32(math.cos(min(math.pi, cone_rad + 0.002)))
    denom = sub @ basis[0]
    local_idx = np.flatnonzero(denom >= cos_thresh)
    if local_idx.size == 0:
        return np.empty(0, dtype=np.int32), np.empty(0, dtype=np.float32)

    sub_hit = sub[local_idx]
    denom_hit = denom[local_idx]
    inv_two_ht = np.float32(1.0 / (2.0 * half_tan))

    xi = (sub_hit @ basis[1]) / denom_hit
    eta = (sub_hit @ basis[2]) / denom_hit
    nx = np.float32(0.5) - xi * inv_two_ht
    ny = np.float32(0.5) - eta * inv_two_ht

    inside = (nx > 0.0) & (nx < 1.0) & (ny > 0.0) & (ny < 1.0)
    if not np.any(inside):
        return np.empty(0, dtype=np.int32), np.empty(0, dtype=np.float32)

    nx = nx[inside]
    ny = ny[inside]
    global_idx = (i0 + local_idx[inside]).astype(np.int32)

    d_edge = np.minimum(np.minimum(nx, 1.0 - nx), np.minimum(ny, 1.0 - ny))
    t = np.clip(d_edge * np.float32(1.0 / FEATHER_EDGE_FRAC), 0.0, 1.0)
    weights = (0.5 - 0.5 * np.cos(t * np.float32(math.pi))).astype(np.float32)
    return global_idx, weights


def _generate_candidate_centers(fov_deg: float) -> List[Tuple[float, float]]:
    """
    Generate a brick-staggered quasi-uniform spherical grid of candidate tile centers
    spaced at `0.78 * fov_deg` so inner unfeathered `0.84 * fov_deg` cores overlap seamlessly.
    """
    step = max(0.18, float(fov_deg) * 0.78)
    n_dec = max(4, int(math.ceil(180.0 / step)))
    d_step = 180.0 / n_dec

    centers: List[Tuple[float, float]] = [(0.0, -90.0)]
    for b in range(n_dec):
        dec0 = -90.0 + (b + 0.5) * d_step
        if abs(dec0) > 89.95:
            continue
        cos_d = max(1e-4, math.cos(dec0 * DEG2RAD))
        n_ra = max(6, int(math.ceil(360.0 * cos_d / step)))
        ra_step = 360.0 / n_ra
        offset = 0.5 * ra_step if (b & 1) else 0.0
        for c in range(n_ra):
            ra0 = (c * ra_step + offset) % 360.0
            centers.append((round(ra0, 4), round(dec0, 4)))
    centers.append((0.0, 90.0))
    return centers


def plan_missing_sky_tiles(
    existing_items: List[dict],
    fill_fov_deg: float,
    sample_step_deg: Optional[float] = None,
    include_custom: bool = False,
) -> dict:
    """
    Analyze spherical coverage of `existing_items` and compute the minimal set of
    `fill_fov_deg` SkyView tiles needed to bring 100% of the celestial sphere to full opacity.

    Returns a dictionary with coverage metrics and `planned_tiles` `[(ra_deg, dec_deg), ...]`.
    """
    fill_fov = max(0.25, float(fill_fov_deg))
    eff_sample_step = (
        float(sample_step_deg)
        if sample_step_deg is not None
        else min(0.25, max(0.08, fill_fov * 0.12))
    )

    pts_xyz, pts_ra, pts_dec, band_offs, actual_step = _build_spherical_sample_grid(
        eff_sample_step
    )
    total_pts = len(pts_dec)
    accum_w = np.zeros(total_pts, dtype=np.float32)

    used_existing = 0
    for item in existing_items:
        if item.get("is_custom") and not include_custom:
            continue
        try:
            ra = float(item["ra"]) % 360.0
            dec = max(-90.0, min(90.0, float(item["dec"])))
            fov = max(0.15, min(25.0, float(item["fov"])))
        except (KeyError, TypeError, ValueError):
            continue

        g_idx, w = _eval_tile_weights_on_slice(ra, dec, fov, pts_xyz, band_offs)
        if g_idx.size:
            accum_w[g_idx] += w
        used_existing += 1

    initial_covered_mask = accum_w >= COVERAGE_TARGET
    initial_coverage_pct = 100.0 * float(np.mean(initial_covered_mask))

    if np.all(initial_covered_mask):
        return {
            "sample_points": total_pts,
            "sample_step_deg": round(actual_step, 4),
            "existing_cutouts_used": used_existing,
            "initial_coverage_pct": round(initial_coverage_pct, 2),
            "final_coverage_pct": 100.0,
            "planned_tiles": [],
        }

    # Build candidate footprints restricted to currently uncovered sample points
    candidates = _generate_candidate_centers(fill_fov)
    cand_coords: List[Tuple[float, float]] = []
    cand_idx_list: List[np.ndarray] = []
    cand_w_list: List[np.ndarray] = []
    heap: List[Tuple[float, int]] = []

    for ra_c, dec_c in candidates:
        g_idx, w = _eval_tile_weights_on_slice(ra_c, dec_c, fill_fov, pts_xyz, band_offs)
        if g_idx.size == 0:
            continue
        need = COVERAGE_TARGET - accum_w[g_idx]
        active = (need > 1e-4) & (w > 0.05)
        if not np.any(active):
            continue
        g_sub = g_idx[active]
        w_sub = w[active]
        gain = float(np.sum(np.minimum(COVERAGE_TARGET - accum_w[g_sub], w_sub)))
        if gain <= 1e-3:
            continue
        cid = len(cand_coords)
        cand_coords.append((ra_c, dec_c))
        cand_idx_list.append(g_sub)
        cand_w_list.append(w_sub)
        heapq.heappush(heap, (-gain, cid))

    selected: List[Tuple[float, float]] = []

    # Lazy-greedy set cover over candidate tiles
    while heap:
        neg_est, cid = heapq.heappop(heap)
        g_sub = cand_idx_list[cid]
        deficit = COVERAGE_TARGET - accum_w[g_sub]
        pos = deficit > 1e-4
        if not np.any(pos):
            continue
        w_sub = cand_w_list[cid]
        true_gain = float(np.sum(np.minimum(deficit[pos], w_sub[pos])))
        if true_gain <= 1e-3:
            continue

        # Re-heapify if gain dropped significantly and another candidate may now beat it
        if heap and true_gain < 0.85 * (-neg_est) and (-heap[0][0]) > true_gain:
            cand_idx_list[cid] = g_sub[pos]
            cand_w_list[cid] = w_sub[pos]
            heapq.heappush(heap, (-true_gain, cid))
            continue

        selected.append(cand_coords[cid])
        accum_w[g_sub] += w_sub

    # Residual spot pass: guarantee 100.0% of spherical sample points reach COVERAGE_TARGET
    uncovered = np.flatnonzero(accum_w < COVERAGE_TARGET)
    while uncovered.size > 0:
        # Center a patch directly on the worst remaining uncovered sample point
        worst_i = int(uncovered[int(np.argmin(accum_w[uncovered]))])
        ra_fix = round(float(pts_ra[worst_i]) % 360.0, 4)
        dec_fix = round(max(-90.0, min(90.0, float(pts_dec[worst_i]))), 4)
        g_idx, w = _eval_tile_weights_on_slice(
            ra_fix, dec_fix, fill_fov, pts_xyz, band_offs
        )
        if g_idx.size == 0:
            accum_w[worst_i] = COVERAGE_TARGET
        else:
            before = float(accum_w[worst_i])
            accum_w[g_idx] += w
            if float(accum_w[worst_i]) <= before + 1e-5:
                accum_w[worst_i] = COVERAGE_TARGET
        selected.append((ra_fix, dec_fix))
        uncovered = np.flatnonzero(accum_w < COVERAGE_TARGET)

    # Sort South-to-North, then RA [0..360) for spatially coherent downloading & strip caching
    selected.sort(key=lambda rd: (round(rd[1], 1), rd[0]))
    final_coverage_pct = 100.0 * float(np.mean(accum_w >= COVERAGE_TARGET))

    return {
        "sample_points": total_pts,
        "sample_step_deg": round(actual_step, 4),
        "existing_cutouts_used": used_existing,
        "initial_coverage_pct": round(initial_coverage_pct, 2),
        "final_coverage_pct": round(final_coverage_pct, 2),
        "planned_tiles": selected,
    }


def make_skyfill_object_id(ra_deg: float, dec_deg: float) -> str:
    """Deterministic cache object identifier for a spherical gap-fill cutout."""
    ra_norm = float(ra_deg) % 360.0
    dec_norm = max(-90.0, min(90.0, float(dec_deg)))
    return f"SKYFILL_{ra_norm:.4f}_{dec_norm:.4f}"


def _probe_dir_writable(dir_path: str) -> Tuple[bool, Optional[str]]:
    """Verify `dir_path` can be created and written to (including over SMB/CIFS shares)."""
    probe_file = os.path.join(dir_path, f".skyfill_write_probe_{os.getpid()}")
    try:
        os.makedirs(dir_path, exist_ok=True)
        with open(probe_file, "wb") as f:
            f.write(b"0")
        return True, None
    except OSError as exc:
        return False, str(exc)
    finally:
        try:
            if os.path.exists(probe_file):
                os.remove(probe_file)
        except OSError:
            pass


def resolve_writable_download_target(
    root_cache_dir: str,
    target_setup: str,
    fov: float,
    resolution: int,
    source: str,
    download_dir_override: Optional[str] = None,
    fallback_local_cache_dir: Optional[str] = None,
) -> Tuple[str, str, Optional[str]]:
    """
    Ensure a writable `(dl_root_cache_dir, dl_setup_hash, fallback_note)` destination before
    starting SkyView HTTP downloads:
      1. Exact `root_cache_dir/target_setup` (preferred).
      2. Writable sibling `root_cache_dir/fov_custom_<exact_fov>_r<res>_<src>` when the
         existing subfolder was created as `root:root` (0755) by Docker on a NAS/SMB share.
      3. Local `./image_cache/<target_setup>` when the entire `--cache-dir` share is read-only.
    """
    if download_dir_override:
        ov_root, ov_setup = normalize_cache_dir_and_setup(download_dir_override)
        chosen_setup = ov_setup or target_setup
        ov_target_dir = resolve_safe_cache_path(ov_root, chosen_setup)
        ok, err = _probe_dir_writable(ov_target_dir)
        if not ok:
            raise FatalDiskWriteError(
                f"Specified --download-dir '{ov_target_dir}' is not writable: {err}"
            )
        return (
            ov_root,
            chosen_setup,
            f"using explicit --download-dir '{ov_target_dir}'",
        )

    primary_dir = resolve_safe_cache_path(root_cache_dir, target_setup)
    ok1, err1 = _probe_dir_writable(primary_dir)
    if ok1:
        return root_cache_dir, target_setup, None

    # Tier 2: writable sibling folder in the same --cache-dir (exact same FOV/res/source)
    sib_setup = _format_skyfill_setup_hash(fov, resolution, source)
    if sib_setup != target_setup:
        sib_dir = resolve_safe_cache_path(root_cache_dir, sib_setup)
        ok2, _ = _probe_dir_writable(sib_dir)
        if ok2:
            return (
                root_cache_dir,
                sib_setup,
                f"'{primary_dir}' is read-only ({err1}); "
                f"saving same-FOV gap tiles into writable sibling '{sib_dir}'",
            )

    # Tier 3: local ./image_cache fallback when the entire NAS/SMB share is read-only
    if fallback_local_cache_dir:
        loc_root = os.path.abspath(fallback_local_cache_dir)
        if os.path.normcase(loc_root) != os.path.normcase(
            os.path.abspath(root_cache_dir)
        ):
            loc_dir = resolve_safe_cache_path(loc_root, target_setup)
            ok3, _ = _probe_dir_writable(loc_dir)
            if ok3:
                return (
                    loc_root,
                    target_setup,
                    f"'{root_cache_dir}' is read-only ({err1}); "
                    f"saving same-FOV gap tiles into local cache '{loc_dir}'",
                )

    raise FatalDiskWriteError(
        f"Cannot write SKYFILL tiles to '{primary_dir}' ({err1}). "
        f"Pass --download-dir <writable_folder> or grant write permissions."
    )


def _collect_existing_sky_items(
    root_cache_dir: str,
    setup_filter: Optional[str],
    target_setup: str,
    fill_fov: float,
    settings: Optional[dict],
    download_dir_override: Optional[str] = None,
    fallback_local_cache_dir: Optional[str] = None,
) -> Tuple[List[dict], List[str]]:
    """
    Gather existing cached cutouts across `root_cache_dir`, any sibling `fov_custom_*`
    SKYFILL folders, and any active/previous fallback download directories so `Ctrl+C`
    resumes accurately even when `target_setup` is read-only.
    """
    effective_filter = setup_filter or target_setup
    items = list_cached_sky_images(
        cache_dir=root_cache_dir,
        settings=settings,
        active_setup_hash=target_setup,
        fallback_fov=fill_fov,
        setup_filter=effective_filter,
    )
    if effective_filter and os.path.isdir(root_cache_dir):
        try:
            sib_entries = sorted(os.listdir(root_cache_dir))
        except OSError:
            sib_entries = []
        for sib in sib_entries:
            if sib == effective_filter or not sib.startswith("fov_custom_"):
                continue
            sib_items = list_cached_sky_images(
                cache_dir=root_cache_dir,
                settings=settings,
                active_setup_hash=target_setup,
                fallback_fov=fill_fov,
                setup_filter=sib,
            )
            items.extend(
                it
                for it in sib_items
                if it.get("is_sky_fill")
                and abs(float(it.get("fov") or 0.0) - fill_fov) < 0.02
            )

    extra_roots_with_skyfill: List[str] = []
    seen_roots = {os.path.normcase(os.path.abspath(root_cache_dir))}

    candidate_extras: List[Tuple[str, bool]] = []
    if download_dir_override:
        ov_root, _ = normalize_cache_dir_and_setup(download_dir_override)
        candidate_extras.append((ov_root, True))
    if fallback_local_cache_dir:
        candidate_extras.append((os.path.abspath(fallback_local_cache_dir), False))

    for ext_root, include_all_skyfill in candidate_extras:
        norm_ext = os.path.normcase(os.path.abspath(ext_root))
        if norm_ext in seen_roots or not os.path.isdir(ext_root):
            continue
        seen_roots.add(norm_ext)
        ext_items = list_cached_sky_images(
            cache_dir=ext_root,
            settings=settings,
            active_setup_hash=target_setup,
            fallback_fov=fill_fov,
        )
        matched_fill = [
            it
            for it in ext_items
            if it.get("is_sky_fill")
            and (
                include_all_skyfill
                or abs(float(it.get("fov") or 0.0) - fill_fov) < 0.02
            )
        ]
        if matched_fill:
            items.extend(matched_fill)
            extra_roots_with_skyfill.append(ext_root)

    return items, extra_roots_with_skyfill


async def _download_single_skyfill_tile(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    ra: float,
    dec: float,
    fov: float,
    resolution: int,
    source: str,
    filepath: str,
    object_id: str,
    max_attempts: int = 5,
) -> bool:
    """Download, auto-stretch, and atomically save one SkyView JPEG cutout."""
    if os.path.isfile(filepath) and os.path.getsize(filepath) > 0:
        return True

    params = {
        "Survey": source,
        "Position": f"{ra:.5f},{dec:.5f}",
        "Size": f"{max(fov, 0.25):.4f}",
        "Pixels": str(int(resolution)),
        "Return": "JPG",
    }
    base_delay = 1.8

    async with semaphore:
        if os.path.isfile(filepath) and os.path.getsize(filepath) > 0:
            return True

        for attempt in range(1, max_attempts + 1):
            try:
                resp = await client.get(SKYVIEW_CGI_URL, params=params)
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise RuntimeError(f"SkyView HTTP {resp.status_code}")
                resp.raise_for_status()

                ctype = resp.headers.get("Content-Type", "")
                if "text" in ctype.lower():
                    snippet = resp.text[:180]
                    if (
                        "too many processes" in snippet.lower()
                        or "busy" in snippet.lower()
                    ):
                        raise RuntimeError("SkyView busy ('Too Many Processes')")
                    raise ValueError(
                        f"SkyView returned unexpected HTML for {object_id}: {snippet[:90]}"
                    )

                stretched = await asyncio.to_thread(auto_stretch_image, resp.content)
                part_path = f"{filepath}.part.{os.getpid()}"
                try:
                    with open(part_path, "wb") as f:
                        f.write(stretched)
                    os.replace(part_path, filepath)
                except OSError:
                    try:
                        if os.path.exists(part_path):
                            os.remove(part_path)
                    except OSError:
                        pass
                    try:
                        with open(filepath, "wb") as f:
                            f.write(stretched)
                    except OSError as disk_exc:
                        raise FatalDiskWriteError(
                            f"Cannot write '{filepath}': {disk_exc}"
                        ) from disk_exc
                return True

            except FatalDiskWriteError:
                raise
            except (httpx.TransportError, RuntimeError) as exc:
                if attempt < max_attempts:
                    delay = base_delay * (2 ** (attempt - 1))
                    await asyncio.sleep(delay)
                    continue
                print(
                    f"  [WARN] Failed {object_id} after {max_attempts} attempts: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                return False
            except Exception as exc:
                print(
                    f"  [ERROR] Permanent failure for {object_id}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                return False

    return False


async def download_planned_tiles(
    tiles: List[Tuple[float, float]],
    root_cache_dir: str,
    setup_hash: str,
    fov: float,
    resolution: int,
    source: str,
    concurrency: int = 2,
    timeout: int = 120,
) -> Tuple[int, int, int]:
    """
    Download `(ra, dec)` gap-fill tiles into `root_cache_dir/setup_hash`.
    Returns `(downloaded_new, skipped_existing, failed_count)`.
    """
    if not tiles:
        return 0, 0, 0

    setup_dir = resolve_safe_cache_path(root_cache_dir, setup_hash)
    os.makedirs(setup_dir, exist_ok=True)

    pending: List[Tuple[float, float, str, str]] = []
    skipped = 0
    for ra, dec in tiles:
        obj_id = make_skyfill_object_id(ra, dec)
        _, filepath, _ = get_cache_info(obj_id, setup_hash, cache_dir=root_cache_dir)
        if os.path.isfile(filepath) and os.path.getsize(filepath) > 0:
            skipped += 1
        else:
            pending.append((ra, dec, obj_id, filepath))

    if not pending:
        return 0, skipped, 0

    total = len(pending)
    conc = max(1, min(6, int(concurrency)))
    semaphore = asyncio.Semaphore(conc)
    limits = httpx.Limits(max_connections=conc + 2, max_keepalive_connections=conc)

    downloaded = 0
    failed = 0
    done = 0
    t0 = time.perf_counter()

    async with httpx.AsyncClient(timeout=timeout, verify=True, limits=limits) as client:

        async def _worker(item: Tuple[float, float, str, str]) -> None:
            nonlocal downloaded, failed, done
            ra, dec, obj_id, fpath = item
            ok = await _download_single_skyfill_tile(
                client=client,
                semaphore=semaphore,
                ra=ra,
                dec=dec,
                fov=fov,
                resolution=resolution,
                source=source,
                filepath=fpath,
                object_id=obj_id,
            )
            done += 1
            if ok:
                downloaded += 1
            else:
                failed += 1

            elapsed = max(0.001, time.perf_counter() - t0)
            rate = done / elapsed
            rem_s = int(round((total - done) / max(rate, 1e-4)))
            mins, secs = divmod(rem_s, 60)
            hrs, mins = divmod(mins, 60)
            eta_str = f"{hrs:d}h {mins:02d}m" if hrs else f"{mins:02d}m {secs:02d}s"
            status_tag = "OK" if ok else "FAIL"
            if done == 1 or done == total or done % 5 == 0 or not ok:
                print(
                    f"  [{done:4d}/{total} | {100.0 * done / total:5.1f}% | "
                    f"{rate:4.2f} tiles/s | ETA {eta_str}] {status_tag} {obj_id} "
                    f"(RA {ra:7.3f}°, Dec {dec:+7.3f}°)",
                    flush=True,
                )

        tasks = [asyncio.create_task(_worker(it)) for it in pending]
        try:
            await asyncio.gather(*tasks)
        except (KeyboardInterrupt, asyncio.CancelledError, FatalDiskWriteError):
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    return downloaded, skipped, failed


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Analyze cached target cutouts, download missing SkyView patches at the exact same FOV, "
            "and rebuild the complete spherical Sky Atlas."
        )
    )
    parser.add_argument(
        "--cache-dir",
        default=None,
        help="Path to image_cache directory or setup subfolder (default: ./image_cache)",
    )
    parser.add_argument(
        "--download-dir",
        default=None,
        help=(
            "Optional writable cache directory for newly downloaded SKYFILL tiles "
            "(auto-detected if --cache-dir subfolder is read-only)"
        ),
    )
    parser.add_argument(
        "--setup",
        default=None,
        help="Optional explicit setup folder name inside --cache-dir (auto-detected by default)",
    )
    parser.add_argument(
        "--fov",
        type=float,
        default=None,
        help="Override tile FOV in degrees (default: auto-match existing downloaded tiles)",
    )
    parser.add_argument(
        "--resolution",
        type=int,
        default=None,
        help="Override pixel width/height per tile (default: auto-match existing tiles)",
    )
    parser.add_argument(
        "--source",
        choices=sorted(ALLOWED_IMAGE_SOURCES),
        default=None,
        help="Override SkyView survey source (default: auto-match existing tiles)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=2,
        help="Max concurrent NASA SkyView downloads (default: 2 to avoid 'Too Many Processes')",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="HTTP timeout in seconds per SkyView tile request (default: 120)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Optional max number of missing tiles to download in this run (0 = all)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute sky coverage and missing tile count without downloading or building",
    )
    parser.add_argument(
        "--skip-atlas",
        action="store_true",
        help="Download missing JPEG tiles only; do not rebuild sky_atlas sheets afterward",
    )
    parser.add_argument(
        "--output-dir",
        default=os.path.join(REPO_ROOT, "frontend", "public", "sky_atlas"),
        help="Primary Sky Atlas output directory (default: ./frontend/public/sky_atlas)",
    )
    parser.add_argument(
        "--quality",
        type=int,
        default=DEFAULT_WEBP_QUALITY,
        help=f"WebP quality 40..95 when rebuilding Sky Atlas (default: {DEFAULT_WEBP_QUALITY})",
    )
    parser.add_argument(
        "--include-custom",
        action="store_true",
        help="Also count personal custom RA/Dec framing cutouts toward existing sky coverage",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Proceed without interactive confirmation prompts",
    )
    args = parser.parse_args()

    explicit_cache_arg = args.cache_dir is not None
    default_local_cache = os.path.join(REPO_ROOT, CACHE_DIR)
    raw_cache_dir = args.cache_dir or default_local_cache

    settings = load_settings()
    setup_info = detect_target_setup(
        cache_dir=raw_cache_dir,
        settings=settings,
        setup_override=args.setup,
        fov_override=args.fov,
        resolution_override=args.resolution,
        source_override=args.source,
    )

    root_cache_dir = setup_info["root_cache_dir"]
    setup_filter = setup_info["setup_filter"]
    target_setup = setup_info["target_setup_hash"]
    fill_fov = setup_info["fov"]
    fill_res = setup_info["resolution"]
    fill_src = setup_info["source"]

    print(
        f"Target tile setup : {target_setup}\n"
        f"  Matched via     : {setup_info['detection_reason']}\n"
        f"  Tile parameters : FOV = {fill_fov:.4f} deg | {fill_res}x{fill_res} px | survey = {fill_src}\n"
        f"  Cache directory : {os.path.join(root_cache_dir, target_setup)}",
        flush=True,
    )

    t_plan0 = time.perf_counter()
    existing_items, extra_atlas_cache_dirs = _collect_existing_sky_items(
        root_cache_dir=root_cache_dir,
        setup_filter=setup_filter,
        target_setup=target_setup,
        fill_fov=fill_fov,
        settings=settings,
        download_dir_override=args.download_dir,
        fallback_local_cache_dir=default_local_cache,
    )

    plan = plan_missing_sky_tiles(
        existing_items=existing_items,
        fill_fov_deg=fill_fov,
        include_custom=args.include_custom,
    )
    plan_elapsed = time.perf_counter() - t_plan0

    planned_tiles = plan["planned_tiles"]
    print(
        f"\nSpherical Sky Coverage Analysis ({plan_elapsed:.2f}s):\n"
        f"  Existing cutouts : {plan['existing_cutouts_used']}\n"
        f"  Current coverage : {plan['initial_coverage_pct']:.2f}% of 41,253 sq.deg celestial sphere\n"
        f"  Missing tiles    : {len(planned_tiles)} tile(s) at {fill_fov:.4f} deg FOV "
        f"-> {plan['final_coverage_pct']:.2f}% full-sky coverage",
        flush=True,
    )

    # Warn if running on an almost-empty default ./image_cache when the real
    # 13k-cutout cache lives on another drive or Docker volume.
    low_default_cache_atlas_cnt = 0
    if (
        not explicit_cache_arg
        and plan["existing_cutouts_used"] < 50
        and len(planned_tiles) > 250
    ):
        _, _, atlas_summary = resolve_active_atlas_dir(root_cache_dir)
        atlas_src_cnt = (
            int((atlas_summary or {}).get("manifest", {}).get("source_count") or 0)
            if atlas_summary
            else 0
        )
        if atlas_src_cnt >= 500:
            low_default_cache_atlas_cnt = atlas_src_cnt
            print(
                f"\n[NOTICE] '{root_cache_dir}' currently contains only {plan['existing_cutouts_used']} "
                f"cutout(s), whereas your existing sky_atlas was built from {atlas_src_cnt} cutouts.\n"
                f"If your downloaded target tiles are stored in another folder or Docker volume, "
                f"pass `--cache-dir <path>` so only the missing gaps are downloaded.",
                flush=True,
            )

    if args.dry_run:
        print("\nDry run complete (no files downloaded).", flush=True)
        return 0

    if low_default_cache_atlas_cnt >= 500 and not args.yes and sys.stdin.isatty():
        ans = input("Continue downloading into this cache directory anyway? [y/N]: ")
        if ans.strip().lower() not in ("y", "yes"):
            print("Aborted.", flush=True)
            return 0

    tiles_to_fetch = (
        planned_tiles[: int(args.limit)]
        if args.limit and int(args.limit) > 0
        else planned_tiles
    )

    failed_count = 0
    dl_root_dir = root_cache_dir
    dl_setup_hash = target_setup

    if tiles_to_fetch:
        try:
            dl_root_dir, dl_setup_hash, fallback_note = resolve_writable_download_target(
                root_cache_dir=root_cache_dir,
                target_setup=target_setup,
                fov=fill_fov,
                resolution=fill_res,
                source=fill_src,
                download_dir_override=args.download_dir,
                fallback_local_cache_dir=default_local_cache,
            )
        except FatalDiskWriteError as exc:
            print(f"\n[ERROR] {exc}", file=sys.stderr, flush=True)
            return 1

        if fallback_note:
            print(f"\n[INFO] Write-permission fallback: {fallback_note}", flush=True)
        if os.path.normcase(os.path.abspath(dl_root_dir)) != os.path.normcase(
            os.path.abspath(root_cache_dir)
        ):
            if dl_root_dir not in extra_atlas_cache_dirs:
                extra_atlas_cache_dirs.append(dl_root_dir)

        print(
            f"\nDownloading {len(tiles_to_fetch)} missing sky patch tile(s) from NASA SkyView "
            f"(concurrency={max(1, min(6, args.concurrency))})...",
            flush=True,
        )
        try:
            dl_new, dl_skipped, failed_count = asyncio.run(
                download_planned_tiles(
                    tiles=tiles_to_fetch,
                    root_cache_dir=dl_root_dir,
                    setup_hash=dl_setup_hash,
                    fov=fill_fov,
                    resolution=fill_res,
                    source=fill_src,
                    concurrency=args.concurrency,
                    timeout=args.timeout,
                )
            )
        except FatalDiskWriteError as exc:
            print(
                f"\n[ERROR] Disk write error aborted download: {exc}",
                file=sys.stderr,
                flush=True,
            )
            return 1
        except KeyboardInterrupt:
            print(
                "\nInterrupted by user. Re-run the exact same command anytime to resume.",
                file=sys.stderr,
                flush=True,
            )
            return 130

        print(
            f"\nDownload summary: {dl_new} newly downloaded, {dl_skipped} already cached, "
            f"{failed_count} failed.",
            flush=True,
        )
    else:
        print("\nAll sky patches are already 100% covered on disk!", flush=True)

    if args.skip_atlas:
        return 0 if failed_count == 0 else 2

    if args.limit and int(args.limit) > 0 and len(tiles_to_fetch) < len(planned_tiles):
        print(
            f"\nSkipping automatic Sky Atlas rebuild because --limit={args.limit} stopped early "
            f"({len(planned_tiles) - len(tiles_to_fetch)} missing tiles remain). "
            f"Run without --limit or run `python scripts/build_sky_atlas.py --cache-dir \"{raw_cache_dir}\"` when ready.",
            flush=True,
        )
        return 0 if failed_count == 0 else 2

    print("\nRebuilding multi-LOD spherical Sky Atlas sheets...", flush=True)
    t_atlas0 = time.perf_counter()
    default_output_dir = os.path.join(REPO_ROOT, "frontend", "public", "sky_atlas")
    mirror_dirs: List[str] = []
    if os.path.normcase(os.path.abspath(args.output_dir)) == os.path.normcase(
        os.path.abspath(default_output_dir)
    ):
        mirror_dirs.append(os.path.join(root_cache_dir, ATLAS_CACHE_SUBDIR))
        dist_dir = os.path.join(REPO_ROOT, "frontend", "dist")
        if os.path.isdir(dist_dir):
            mirror_dirs.append(os.path.join(dist_dir, "sky_atlas"))

    result = build_sky_atlas(
        cache_dir=raw_cache_dir,
        output_dir=args.output_dir,
        mirror_dirs=mirror_dirs,
        extra_cache_dirs=extra_atlas_cache_dirs,
        settings=settings,
        active_setup_hash=target_setup,
        fallback_fov=fill_fov,
        lod0_tile_size=DEFAULT_LOD0_TILE_SIZE,
        lod0_step_deg=DEFAULT_LOD0_STEP_DEG,
        lod1_tile_size=DEFAULT_LOD1_TILE_SIZE,
        lod1_step_deg=DEFAULT_LOD1_STEP_DEG,
        lod2_tile_size=DEFAULT_LOD2_TILE_SIZE,
        lod2_step_deg=DEFAULT_LOD2_STEP_DEG,
        sheet_size=DEFAULT_SHEET_SIZE,
        quality=args.quality,
        include_custom=args.include_custom,
        progress_callback=lambda msg: print(msg, flush=True),
    )
    atlas_elapsed = time.perf_counter() - t_atlas0

    print(
        f"\nComplete Sky Atlas built in {atlas_elapsed:.2f}s:\n"
        f"  Cutouts composited : {result['source_count']}\n"
        f"  LOD 0 wide-sky     : {result['lod0_sectors']} sectors ({result['lod0_sheets']} sheet)\n"
        f"  LOD 1 mid-field    : {result['lod1_sectors']} sectors ({result['lod1_sheets']} sheets)\n"
        f"  LOD 2 close-up     : {result['lod2_sectors']} sectors ({result['lod2_sheets']} sheets)\n"
        f"  Total WebP + JSON  : {result['size_mb']} MB\n"
        f"  Written to         : {', '.join(result['written_dirs'])}",
        flush=True,
    )
    return 0 if failed_count == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
