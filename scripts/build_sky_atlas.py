#!/usr/bin/env python3
"""
Build pre-composited spherical Sky Atlas sector sheets (WebP) + coordinate manifest (JSON)
from downloaded sky survey images in `image_cache/`.

This offline developer script reprojects and seamlessly blends overlapping target cutouts
onto a 2-tier spherical RA/Dec tangent-plane grid:
  - LOD 0 (~12° sectors, 240x240 px): fits the entire sky onto a single 4096x4096 WebP sheet
    for instantaneous 60 FPS wide-field rendering with zero overlap.
  - LOD 1 (~7.5° sectors, 512x512 px): spatially grouped sheets loaded on demand when zoomed in.

Usage:
    python scripts/build_sky_atlas.py
    python scripts/build_sky_atlas.py --cache-dir ./image_cache --quality 80
"""

import argparse
import os
import sys
import time

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from backend.config import CACHE_DIR, load_settings
from backend.models import Camera, Telescope
from backend.sky_atlas import (
    ATLAS_CACHE_SUBDIR,
    DEFAULT_LOD0_STEP_DEG,
    DEFAULT_LOD0_TILE_SIZE,
    DEFAULT_LOD1_STEP_DEG,
    DEFAULT_LOD1_TILE_SIZE,
    DEFAULT_SHEET_SIZE,
    DEFAULT_WEBP_QUALITY,
    build_sky_atlas,
    normalize_cache_dir_and_setup,
)
from backend.stream_service import compute_fov_and_setup


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Composite cached sky survey cutouts into spherical RA/Dec WebP sector sheets + sky_atlas.json."
    )
    parser.add_argument(
        "--cache-dir",
        default=os.path.join(REPO_ROOT, CACHE_DIR),
        help="Path to image_cache directory containing downloaded survey cutouts (default: ./image_cache)",
    )
    parser.add_argument(
        "--output-dir",
        default=os.path.join(REPO_ROOT, "frontend", "public", "sky_atlas"),
        help="Primary output directory (default: ./frontend/public/sky_atlas)",
    )
    parser.add_argument(
        "--lod0-tile-size",
        type=int,
        default=DEFAULT_LOD0_TILE_SIZE,
        help=f"Pixel width/height per wide-field LOD 0 sector (default: {DEFAULT_LOD0_TILE_SIZE})",
    )
    parser.add_argument(
        "--lod0-step-deg",
        type=float,
        default=DEFAULT_LOD0_STEP_DEG,
        help=f"Angular step in degrees for LOD 0 sectors (default: {DEFAULT_LOD0_STEP_DEG})",
    )
    parser.add_argument(
        "--lod1-tile-size",
        type=int,
        default=DEFAULT_LOD1_TILE_SIZE,
        help=f"Pixel width/height per mid-field LOD 1 sector (default: {DEFAULT_LOD1_TILE_SIZE})",
    )
    parser.add_argument(
        "--lod1-step-deg",
        type=float,
        default=DEFAULT_LOD1_STEP_DEG,
        help=f"Angular step in degrees for LOD 1 sectors (default: {DEFAULT_LOD1_STEP_DEG})",
    )
    parser.add_argument(
        "--sheet-size",
        type=int,
        default=DEFAULT_SHEET_SIZE,
        help=f"Maximum pixel width/height per WebP sheet (default: {DEFAULT_SHEET_SIZE})",
    )
    parser.add_argument(
        "--quality",
        type=int,
        default=DEFAULT_WEBP_QUALITY,
        help=f"WebP quality 40..95 (default: {DEFAULT_WEBP_QUALITY})",
    )
    parser.add_argument(
        "--include-custom",
        action="store_true",
        help="Also include custom RA/Dec framing cutouts (excluded by default)",
    )
    args = parser.parse_args()

    t0 = time.perf_counter()
    settings = load_settings()
    active_setup_hash = None
    fallback_fov = 1.0
    try:
        telescope = Telescope(**settings.get("telescope", {"focal_length": 400}))
        camera = Camera(
            **settings.get("camera", {"sensor_width": 23.5, "sensor_height": 15.6})
        )
        fov_info = compute_fov_and_setup(telescope, camera, settings)
        active_setup_hash = fov_info.get("setup_hash")
        fallback_fov = float(fov_info.get("default_download_fov") or 1.0)
    except Exception:
        pass

    root_cache_dir, _ = normalize_cache_dir_and_setup(args.cache_dir)
    default_output_dir = os.path.join(REPO_ROOT, "frontend", "public", "sky_atlas")
    mirror_dirs = []
    if os.path.normcase(os.path.abspath(args.output_dir)) == os.path.normcase(
        os.path.abspath(default_output_dir)
    ):
        mirror_dirs.append(os.path.join(root_cache_dir, ATLAS_CACHE_SUBDIR))
        dist_dir = os.path.join(REPO_ROOT, "frontend", "dist")
        if os.path.isdir(dist_dir):
            mirror_dirs.append(os.path.join(dist_dir, "sky_atlas"))

    result = build_sky_atlas(
        cache_dir=args.cache_dir,
        output_dir=args.output_dir,
        mirror_dirs=mirror_dirs,
        settings=settings,
        active_setup_hash=active_setup_hash,
        fallback_fov=fallback_fov,
        lod0_tile_size=args.lod0_tile_size,
        lod0_step_deg=args.lod0_step_deg,
        lod1_tile_size=args.lod1_tile_size,
        lod1_step_deg=args.lod1_step_deg,
        sheet_size=args.sheet_size,
        quality=args.quality,
        include_custom=args.include_custom,
        progress_callback=lambda msg: print(msg, flush=True),
    )
    elapsed = time.perf_counter() - t0

    if result["source_count"] == 0:
        print(
            f"No cached sky survey cutouts found in '{args.cache_dir}'. Nothing written.",
            file=sys.stderr,
        )
        return 1

    print(
        f"Sky Atlas v2 sector map built in {elapsed:.2f}s:\n"
        f"  Cutouts composited : {result['source_count']}\n"
        f"  LOD 0 wide-sky     : {result['lod0_sectors']} sectors across {result['lod0_sheets']} sheet(s) "
        f"({args.lod0_tile_size}x{args.lod0_tile_size} px, ~{args.lod0_step_deg:g} deg)\n"
        f"  LOD 1 mid-field    : {result['lod1_sectors']} sectors across {result['lod1_sheets']} sheet(s) "
        f"({args.lod1_tile_size}x{args.lod1_tile_size} px, ~{args.lod1_step_deg:g} deg)\n"
        f"  Total WebP + JSON  : {result['size_mb']} MB\n"
        f"  Written to         : {', '.join(result['written_dirs'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
