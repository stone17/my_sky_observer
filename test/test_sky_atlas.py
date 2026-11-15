import json
import os
import sys

import pytest
from fastapi.testclient import TestClient
from PIL import Image

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.image_service import (
    get_cache_info,
    get_cache_status_info,
    get_setup_hash,
    list_cached_sky_images,
    purge_image_cache,
)
from backend.main import app
from backend.sky_atlas import (
    ATLAS_CACHE_SUBDIR,
    MANIFEST_FILENAME,
    build_sky_atlas,
    resolve_safe_dir_file,
    validate_atlas_filename,
)


def _write_dummy_cutout(path: str, color=(40, 80, 160), size=256):
    img = Image.new("RGB", (size, size), color=color)
    img.save(path, format="JPEG", quality=85)


def test_validate_atlas_filename_security():
    assert validate_atlas_filename("sky_atlas.json") == "sky_atlas.json"
    assert validate_atlas_filename("sky_atlas_lod0.webp") == "sky_atlas_lod0.webp"
    assert validate_atlas_filename("sky_atlas_lod0_1.webp") == "sky_atlas_lod0_1.webp"
    assert validate_atlas_filename("sky_atlas_lod1_0.webp") == "sky_atlas_lod1_0.webp"
    assert validate_atlas_filename("sky_atlas_lod2_0.webp") == "sky_atlas_lod2_0.webp"
    assert validate_atlas_filename("sky_atlas_0.webp") == "sky_atlas_0.webp"
    assert validate_atlas_filename("LICENSE-ASSETS.md") == "LICENSE-ASSETS.md"

    for bad in [
        "../secret.txt",
        "sky_atlas_0.png",
        "sky_atlas.json.bak",
        "sub/sky_atlas.json",
        "",
    ]:
        with pytest.raises(ValueError):
            validate_atlas_filename(bad)


def test_build_sky_atlas_sectors_overlap_and_cache_isolation(tmp_path):
    cache_dir = tmp_path / "image_cache"
    cache_dir.mkdir()
    public_dir = tmp_path / "public_atlas"

    setup_hash = get_setup_hash(1.68, 1.12, 1.15, 1024, "dss2r")
    # M 42 and M 43 overlap in Orion (~0.12 deg apart), M 31 is in Andromeda
    _, m42_path, m42_dir = get_cache_info("M 42", setup_hash, cache_dir=str(cache_dir))
    _, m43_path, m43_dir = get_cache_info("M 43", setup_hash, cache_dir=str(cache_dir))
    _, m31_path, m31_dir = get_cache_info("M 31", setup_hash, cache_dir=str(cache_dir))
    os.makedirs(m42_dir, exist_ok=True)
    os.makedirs(m43_dir, exist_ok=True)
    os.makedirs(m31_dir, exist_ok=True)

    _write_dummy_cutout(m42_path, color=(200, 40, 80))
    _write_dummy_cutout(m43_path, color=(220, 90, 60))
    _write_dummy_cutout(m31_path, color=(60, 120, 220))

    atlas_cache_dir = cache_dir / ATLAS_CACHE_SUBDIR

    result = build_sky_atlas(
        cache_dir=str(cache_dir),
        output_dir=str(atlas_cache_dir),
        mirror_dirs=[str(public_dir)],
        settings={"catalogs": ["messier"]},
        active_setup_hash=setup_hash,
        fallback_fov=2.0,
        lod0_tile_size=128,
        lod0_step_deg=15.0,
        lod1_tile_size=256,
        lod1_step_deg=8.0,
        lod2_tile_size=256,
        lod2_step_deg=4.0,
        sheet_size=1024,
        quality=80,
    )

    assert result["status"] == "ok"
    assert result["version"] == 2
    assert result["source_count"] == 3
    assert result["lod0_sectors"] >= 2
    assert result["lod1_sectors"] >= 2
    assert result["lod2_sectors"] >= 2
    assert result["lod0_sheets"] == 1
    assert result["lod1_sheets"] >= 1
    assert result["lod2_sheets"] >= 1

    assert (atlas_cache_dir / MANIFEST_FILENAME).is_file()
    assert (atlas_cache_dir / "sky_atlas_lod0.webp").is_file()
    assert (atlas_cache_dir / "sky_atlas_lod1_0.webp").is_file()
    assert (atlas_cache_dir / "sky_atlas_lod2_0.webp").is_file()
    assert (atlas_cache_dir / "LICENSE-ASSETS.md").is_file()

    # Verify mirrored public directory received identical artifacts
    assert (public_dir / MANIFEST_FILENAME).is_file()
    assert (public_dir / "sky_atlas_lod0.webp").is_file()
    assert (public_dir / "sky_atlas_lod1_0.webp").is_file()
    assert (public_dir / "sky_atlas_lod2_0.webp").is_file()

    manifest = json.loads(
        (atlas_cache_dir / MANIFEST_FILENAME).read_text(encoding="utf-8")
    )
    assert manifest["version"] == 2
    assert manifest["mode"] == "sectors"
    assert manifest["lod_count"] == 3
    assert manifest["source_count"] == 3
    assert len(manifest["lod0"]["sectors"]) == result["lod0_sectors"]
    assert len(manifest["lod1"]["sectors"]) == result["lod1_sectors"]
    assert len(manifest["lod2"]["sectors"]) == result["lod2_sectors"]
    assert len(manifest["targets"]) == 3
    target_names = {entry[0] for entry in manifest["targets"]}
    assert target_names == {"M 42", "M 43", "M 31"}

    # Verify _sky_atlas directory is excluded from raw JPEG cache count & purge
    status_before = get_cache_status_info(str(cache_dir), setup_hash=setup_hash)
    assert status_before["count"] == 3
    assert status_before["setup_count"] == 3
    assert (
        len(list_cached_sky_images(str(cache_dir), settings={"catalogs": ["messier"]}))
        == 3
    )

    purge_image_cache(str(cache_dir), include_atlas=False)
    status_after = get_cache_status_info(str(cache_dir))
    assert status_after["count"] == 0
    assert (atlas_cache_dir / MANIFEST_FILENAME).is_file()
    assert (atlas_cache_dir / "sky_atlas_lod0.webp").is_file()

    resolved = resolve_safe_dir_file(str(atlas_cache_dir), "sky_atlas_lod0.webp")
    assert os.path.isfile(resolved)


def test_build_sky_atlas_accepts_direct_setup_subfolder(tmp_path):
    cache_dir = tmp_path / "00_SkyCache"
    cache_dir.mkdir()
    out_dir = tmp_path / "out_atlas"

    setup_hash = "fov_2.15_1.44_p1.50_r1024_dss2r"
    _, m42_path, setup_subdir = get_cache_info(
        "M 42", setup_hash, cache_dir=str(cache_dir)
    )
    os.makedirs(setup_subdir, exist_ok=True)
    _write_dummy_cutout(m42_path, color=(180, 60, 120))

    # Passing the setup subfolder directly (`.../fov_2.15_1.44_p1.50_r1024_dss2r`) should work seamlessly
    result = build_sky_atlas(
        cache_dir=str(setup_subdir),
        output_dir=str(out_dir),
        lod0_tile_size=128,
        lod1_tile_size=256,
        lod2_tile_size=256,
        sheet_size=1024,
    )
    assert result["status"] == "ok"
    assert result["source_count"] == 1
    assert (out_dir / "sky_atlas_lod0.webp").is_file()
    assert (out_dir / "sky_atlas_lod1_0.webp").is_file()
    assert (out_dir / "sky_atlas_lod2_0.webp").is_file()


def test_user_facing_atlas_builder_routes_removed():
    client = TestClient(app)
    assert client.post("/api/cache/sky-atlas/build").status_code in (404, 405)
    assert client.get("/api/cache/sky-atlas/status").status_code == 404
    assert client.get("/api/cache/sky-atlas/export.zip").status_code == 404
