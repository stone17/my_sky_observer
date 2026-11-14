import io
import json
import os
import sys
import zipfile

import pytest
from PIL import Image

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from backend.image_service import (
    get_cache_info,
    get_cache_status_info,
    get_setup_hash,
    list_cached_sky_images,
    purge_image_cache,
)
from backend.sky_atlas import (
    ATLAS_CACHE_SUBDIR,
    MANIFEST_FILENAME,
    build_sky_atlas,
    create_atlas_export_zip_bytes,
    get_sky_atlas_status,
    resolve_safe_dir_file,
    validate_atlas_filename,
)


def _write_dummy_cutout(path: str, color=(40, 80, 160), size=256):
    img = Image.new("RGB", (size, size), color=color)
    img.save(path, format="JPEG", quality=85)


def test_validate_atlas_filename_security():
    assert validate_atlas_filename("sky_atlas.json") == "sky_atlas.json"
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


def test_build_sky_atlas_incremental_and_cache_isolation(tmp_path):
    cache_dir = tmp_path / "image_cache"
    cache_dir.mkdir()
    public_dir = tmp_path / "public_atlas"

    setup_hash = get_setup_hash(1.68, 1.12, 1.15, 1024, "dss2r")
    _, m42_path, m42_dir = get_cache_info("M 42", setup_hash, cache_dir=str(cache_dir))
    _, m31_path, _ = get_cache_info("M 31", setup_hash, cache_dir=str(cache_dir))
    os.makedirs(m42_dir, exist_ok=True)

    # Create two synthetic catalog cutouts matching My Sky Observer cache layout
    _write_dummy_cutout(m42_path, color=(200, 40, 80))
    _write_dummy_cutout(m31_path, color=(60, 120, 220))

    atlas_cache_dir = cache_dir / ATLAS_CACHE_SUBDIR

    result = build_sky_atlas(
        cache_dir=str(cache_dir),
        output_dir=str(atlas_cache_dir),
        mirror_dirs=[str(public_dir)],
        settings={"catalogs": ["messier"]},
        active_setup_hash=setup_hash,
        fallback_fov=2.0,
        tile_size=128,
        sheet_size=512,
        quality=80,
    )

    assert result["status"] == "ok"
    assert result["count"] == 2
    assert result["sheets"] == 1
    assert (atlas_cache_dir / MANIFEST_FILENAME).is_file()
    assert (atlas_cache_dir / "sky_atlas_0.webp").is_file()
    assert (atlas_cache_dir / "LICENSE-ASSETS.md").is_file()
    # Verify mirrored public directory received identical artifacts
    assert (public_dir / MANIFEST_FILENAME).is_file()
    assert (public_dir / "sky_atlas_0.webp").is_file()

    manifest = json.loads((atlas_cache_dir / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    names = {item["name"] for item in manifest["items"]}
    assert len(names) == 2

    # Verify _sky_atlas directory is excluded from raw JPEG cache count & purge
    status_before = get_cache_status_info(str(cache_dir), setup_hash=setup_hash)
    assert status_before["count"] == 2
    assert status_before["setup_count"] == 2
    assert len(list_cached_sky_images(str(cache_dir), settings={"catalogs": ["messier"]})) == 2

    purge_image_cache(str(cache_dir), include_atlas=False)
    status_after = get_cache_status_info(str(cache_dir))
    assert status_after["count"] == 0
    assert (atlas_cache_dir / MANIFEST_FILENAME).is_file()

    # Add a new target after purge and verify incremental merge preserves M 31 & M 42
    _, m45_path, m45_dir = get_cache_info("M 45", setup_hash, cache_dir=str(cache_dir))
    os.makedirs(m45_dir, exist_ok=True)
    _write_dummy_cutout(m45_path, color=(30, 180, 210))
    merged = build_sky_atlas(
        cache_dir=str(cache_dir),
        output_dir=str(atlas_cache_dir),
        settings={"catalogs": ["messier"]},
        active_setup_hash=setup_hash,
        tile_size=128,
        sheet_size=512,
        merge_existing=True,
    )
    assert merged["count"] == 3
    assert merged["preserved_from_previous"] == 2

    # Verify status & ZIP export
    st = get_sky_atlas_status(str(cache_dir))
    assert st["exists"] is True
    assert st["count"] == 3

    zip_bytes = create_atlas_export_zip_bytes(str(cache_dir))
    assert zip_bytes is not None
    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
        archived = set(zf.namelist())
        assert "sky_atlas/sky_atlas.json" in archived
        assert "sky_atlas/sky_atlas_0.webp" in archived
        assert "sky_atlas/LICENSE-ASSETS.md" in archived

    # Verify resolve_safe_dir_file resolves valid artifact cleanly
    resolved = resolve_safe_dir_file(str(atlas_cache_dir), "sky_atlas_0.webp")
    assert os.path.isfile(resolved)
