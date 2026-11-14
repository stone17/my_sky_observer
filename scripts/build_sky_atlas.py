#!/usr/bin/env python3
"""
Build pre-baked Sky Atlas sprite sheets (WebP) + coordinate manifest (JSON)
from downloaded sky survey images in `image_cache/`.

Usage:
    python scripts/build_sky_atlas.py
    python scripts/build_sky_atlas.py --tile-size 128 --quality 78
    python scripts/build_sky_atlas.py --from-scratch
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
    DEFAULT_SHEET_SIZE,
    DEFAULT_TILE_SIZE,
    DEFAULT_WEBP_QUALITY,
    build_sky_atlas,
)
from backend.stream_service import compute_fov_and_setup


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pack cached sky survey images into WebP sprite sheet(s) + sky_atlas.json."
    )
    parser.add_argument(
        "--cache-dir",
        default=os.path.join(REPO_ROOT, CACHE_DIR),
        help="Path to image_cache directory (default: ./image_cache)",
    )
    parser.add_argument(
        "--output-dir",
        default=os.path.join(REPO_ROOT, "frontend", "public", "sky_atlas"),
        help="Primary output directory (default: ./frontend/public/sky_atlas)",
    )
    parser.add_argument(
        "--tile-size",
        type=int,
        default=DEFAULT_TILE_SIZE,
        help=f"Pixel width/height per target tile (default: {DEFAULT_TILE_SIZE})",
    )
    parser.add_argument(
        "--sheet-size",
        type=int,
        default=DEFAULT_SHEET_SIZE,
        help=f"Maximum pixel width/height per sprite sheet (default: {DEFAULT_SHEET_SIZE})",
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
    parser.add_argument(
        "--from-scratch",
        action="store_true",
        help="Rebuild strictly from current image_cache without preserving earlier atlas tiles",
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

    mirror_dirs = [os.path.join(args.cache_dir, ATLAS_CACHE_SUBDIR)]
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
        tile_size=args.tile_size,
        sheet_size=args.sheet_size,
        quality=args.quality,
        include_custom=args.include_custom,
        merge_existing=not args.from_scratch,
    )
    elapsed = time.perf_counter() - t0

    print(
        f"Sky Atlas built in {elapsed:.2f}s:\n"
        f"  Targets packed : {result['count']} "
        f"(fresh from cache: {result['fresh_cached_used']}, preserved: {result['preserved_from_previous']})\n"
        f"  Sprite sheets  : {result['sheets']} ({result['tile_size']}x{result['tile_size']} px tiles)\n"
        f"  Total size     : {result['size_mb']} MB\n"
        f"  Output         : {args.output_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
