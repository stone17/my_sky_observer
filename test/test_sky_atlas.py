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
    assert "targets" not in manifest
    assert "lods" not in manifest
    sample_sec = manifest["lod0"]["sectors"][0]
    assert isinstance(sample_sec, list) and len(sample_sec) == 6

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


def test_fill_sky_gaps_auto_setup_and_complete_coverage(tmp_path):
    from backend.sky_atlas import _generate_sector_grid
    from scripts.fill_sky_gaps import (
        detect_target_setup,
        make_skyfill_object_id,
        plan_missing_sky_tiles,
    )

    cache_dir = tmp_path / "image_cache"
    cache_dir.mkdir()
    out_dir = tmp_path / "complete_atlas"

    # Simulate an existing equipment setup with wide FOV (18 deg) so full-sky test runs in <0.5s
    other_setup = "fov_0.84_0.62_p1.15_r1024_dss2r"
    dominant_setup = "fov_12.00_8.00_p1.50_r512_dss2b"  # effective FOV = 18.0 deg

    _, p_small, d_small = get_cache_info("M 31", other_setup, cache_dir=str(cache_dir))
    os.makedirs(d_small, exist_ok=True)
    _write_dummy_cutout(p_small, color=(50, 90, 150), size=128)

    for target_name in ("M 42", "M 45", "M 13"):
        _, p_dom, d_dom = get_cache_info(
            target_name, dominant_setup, cache_dir=str(cache_dir)
        )
        os.makedirs(d_dom, exist_ok=True)
        _write_dummy_cutout(p_dom, color=(190, 70, 110), size=128)

    # 1. Auto-detect setup from root cache_dir picks the dominant folder & exact FOV/res/source
    detected = detect_target_setup(str(cache_dir))
    assert detected["target_setup_hash"] == dominant_setup
    assert abs(detected["fov"] - 18.0) < 1e-4
    assert detected["resolution"] == 512
    assert detected["source"] == "dss2b"

    # Also auto-detects when pointing directly at the setup subfolder
    detected_sub = detect_target_setup(os.path.join(str(cache_dir), dominant_setup))
    assert detected_sub["target_setup_hash"] == dominant_setup
    assert abs(detected_sub["fov"] - 18.0) < 1e-4

    # 2. Plan missing sky tiles at that exact 18.0 deg FOV
    existing = list_cached_sky_images(
        str(cache_dir), setup_filter=dominant_setup, active_setup_hash=dominant_setup
    )
    assert len(existing) == 3

    plan1 = plan_missing_sky_tiles(
        existing_items=existing, fill_fov_deg=detected["fov"], sample_step_deg=2.0
    )
    assert 0.0 < plan1["initial_coverage_pct"] < 25.0
    assert plan1["final_coverage_pct"] == 100.0
    assert len(plan1["planned_tiles"]) > 20

    # 3. Write SKYFILL cutouts into the same setup folder and verify 100% coverage (0 remaining gaps)
    for ra, dec in plan1["planned_tiles"]:
        obj_id = make_skyfill_object_id(ra, dec)
        _, fpath, sdir = get_cache_info(
            obj_id, dominant_setup, cache_dir=str(cache_dir)
        )
        os.makedirs(sdir, exist_ok=True)
        _write_dummy_cutout(fpath, color=(40, 110, 170), size=128)

    after_items = list_cached_sky_images(
        str(cache_dir), setup_filter=dominant_setup, active_setup_hash=dominant_setup
    )
    skyfill_items = [it for it in after_items if it.get("is_sky_fill")]
    assert len(skyfill_items) == len(plan1["planned_tiles"])
    assert all(not it["is_custom"] for it in skyfill_items)

    plan2 = plan_missing_sky_tiles(
        existing_items=after_items, fill_fov_deg=detected["fov"], sample_step_deg=2.0
    )
    assert plan2["initial_coverage_pct"] == 100.0
    assert plan2["planned_tiles"] == []

    # 4. Rebuild Sky Atlas without --include-custom -> every LOD0 sector on the sphere is populated
    expected_lod0_total = len(_generate_sector_grid(20.0, 64))
    atlas_res = build_sky_atlas(
        cache_dir=os.path.join(str(cache_dir), dominant_setup),
        output_dir=str(out_dir),
        lod0_tile_size=64,
        lod0_step_deg=20.0,
        lod1_tile_size=64,
        lod1_step_deg=15.0,
        lod2_tile_size=0,
        lod2_step_deg=0.0,
        sheet_size=512,
        include_custom=False,
    )
    assert atlas_res["status"] == "ok"
    assert atlas_res["source_count"] == 3 + len(plan1["planned_tiles"])
    assert atlas_res["lod0_sectors"] == expected_lod0_total


def test_fill_sky_gaps_readonly_nas_fallback_and_resume(tmp_path, monkeypatch):
    import scripts.fill_sky_gaps as fsg
    from backend.sky_atlas import _generate_sector_grid

    nas_cache = tmp_path / "nas_00_SkyCache"
    nas_cache.mkdir()
    local_cache = tmp_path / "local_image_cache"
    out_dir_sib = tmp_path / "atlas_from_sibling"
    out_dir_extra = tmp_path / "atlas_from_extra"

    # Match exact fractional FOV case (2.15 * 1.50 = 3.2250 deg, scaled up to 16.1250 deg for fast test)
    ro_setup = "fov_10.75_7.20_p1.50_r512_dss2r"  # effective FOV = 16.1250 deg
    expected_sib_setup = "fov_custom_16.1250_r512_dss2r"

    for target_name in ("M 42", "M 45"):
        _, fpath, sdir = get_cache_info(target_name, ro_setup, cache_dir=str(nas_cache))
        os.makedirs(sdir, exist_ok=True)
        _write_dummy_cutout(fpath, color=(180, 75, 115), size=128)

    detected = fsg.detect_target_setup(str(nas_cache))
    assert detected["target_setup_hash"] == ro_setup
    assert abs(detected["fov"] - 16.125) < 1e-4

    real_probe = fsg._probe_dir_writable
    ro_setup_dir_norm = os.path.normcase(os.path.abspath(str(nas_cache / ro_setup)))

    # Case A: Docker-created setup subfolder is root:root (read-only), parent NAS share is writable
    def mock_probe_sub_readonly(dir_path: str):
        if os.path.normcase(os.path.abspath(dir_path)) == ro_setup_dir_norm:
            return False, "[Errno 13] Permission denied"
        return real_probe(dir_path)

    monkeypatch.setattr(fsg, "_probe_dir_writable", mock_probe_sub_readonly)

    dl_root, dl_setup, note = fsg.resolve_writable_download_target(
        root_cache_dir=str(nas_cache),
        target_setup=ro_setup,
        fov=detected["fov"],
        resolution=detected["resolution"],
        source=detected["source"],
        fallback_local_cache_dir=str(local_cache),
    )
    assert dl_root == str(nas_cache)
    assert dl_setup == expected_sib_setup
    assert note and "writable sibling" in note

    existing, _ = fsg._collect_existing_sky_items(
        root_cache_dir=str(nas_cache),
        setup_filter=None,
        target_setup=ro_setup,
        fill_fov=detected["fov"],
        settings=None,
        fallback_local_cache_dir=str(local_cache),
    )
    plan = fsg.plan_missing_sky_tiles(
        existing_items=existing, fill_fov_deg=detected["fov"], sample_step_deg=2.0
    )
    assert len(plan["planned_tiles"]) > 20

    # Write SKYFILL tiles into the writable sibling folder (many more files than ro_setup's 2 files)
    for ra, dec in plan["planned_tiles"]:
        obj_id = fsg.make_skyfill_object_id(ra, dec)
        _, fpath, sdir = get_cache_info(
            obj_id, expected_sib_setup, cache_dir=str(nas_cache)
        )
        os.makedirs(sdir, exist_ok=True)
        _write_dummy_cutout(fpath, color=(45, 115, 175), size=128)

    # Re-running (Ctrl+C resume) still detects the original equipment setup (`ro_setup`), NOT `fov_custom_*`,
    # and recognizes 100% sky coverage across `ro_setup` + `expected_sib_setup`
    detected_resume = fsg.detect_target_setup(str(nas_cache))
    assert detected_resume["target_setup_hash"] == ro_setup
    resumed_items, _ = fsg._collect_existing_sky_items(
        root_cache_dir=str(nas_cache),
        setup_filter=None,
        target_setup=ro_setup,
        fill_fov=detected["fov"],
        settings=None,
    )
    plan_resumed = fsg.plan_missing_sky_tiles(
        existing_items=resumed_items, fill_fov_deg=detected["fov"], sample_step_deg=2.0
    )
    assert plan_resumed["initial_coverage_pct"] == 100.0
    assert plan_resumed["planned_tiles"] == []

    # Building Sky Atlas from either root NAS cache or direct ro_setup subfolder composites all tiles
    expected_lod0 = len(_generate_sector_grid(20.0, 64))
    res_sib = build_sky_atlas(
        cache_dir=str(nas_cache / ro_setup),
        output_dir=str(out_dir_sib),
        active_setup_hash=ro_setup,
        fallback_fov=detected["fov"],
        lod0_tile_size=64,
        lod0_step_deg=20.0,
        lod1_tile_size=64,
        lod1_step_deg=15.0,
        lod2_tile_size=0,
        lod2_step_deg=0.0,
        sheet_size=512,
    )
    assert res_sib["source_count"] == 2 + len(plan["planned_tiles"])
    assert res_sib["lod0_sectors"] == expected_lod0

    # Case B: Entire NAS share is read-only -> falls back to local_cache and merges via extra_cache_dirs
    import shutil

    shutil.rmtree(nas_cache / expected_sib_setup)
    nas_root_norm = os.path.normcase(os.path.abspath(str(nas_cache)))

    def mock_probe_entire_nas_readonly(dir_path: str):
        abs_norm = os.path.normcase(os.path.abspath(dir_path))
        if abs_norm == nas_root_norm or abs_norm.startswith(nas_root_norm + os.sep):
            return False, "[Errno 13] Permission denied"
        return real_probe(dir_path)

    monkeypatch.setattr(fsg, "_probe_dir_writable", mock_probe_entire_nas_readonly)

    dl_root2, dl_setup2, note2 = fsg.resolve_writable_download_target(
        root_cache_dir=str(nas_cache),
        target_setup=ro_setup,
        fov=detected["fov"],
        resolution=detected["resolution"],
        source=detected["source"],
        fallback_local_cache_dir=str(local_cache),
    )
    assert dl_root2 == os.path.abspath(str(local_cache))
    assert dl_setup2 == ro_setup
    assert note2 and "local cache" in note2

    for ra, dec in plan["planned_tiles"]:
        obj_id = fsg.make_skyfill_object_id(ra, dec)
        _, fpath, sdir = get_cache_info(
            obj_id, dl_setup2, cache_dir=dl_root2
        )
        os.makedirs(sdir, exist_ok=True)
        _write_dummy_cutout(fpath, color=(55, 125, 185), size=128)

    merged_items, extra_dirs = fsg._collect_existing_sky_items(
        root_cache_dir=str(nas_cache),
        setup_filter=None,
        target_setup=ro_setup,
        fill_fov=detected["fov"],
        settings=None,
        fallback_local_cache_dir=str(local_cache),
    )
    assert os.path.abspath(str(local_cache)) in extra_dirs
    plan_merged = fsg.plan_missing_sky_tiles(
        existing_items=merged_items, fill_fov_deg=detected["fov"], sample_step_deg=2.0
    )
    assert plan_merged["planned_tiles"] == []

    res_extra = build_sky_atlas(
        cache_dir=str(nas_cache),
        extra_cache_dirs=extra_dirs,
        output_dir=str(out_dir_extra),
        active_setup_hash=ro_setup,
        fallback_fov=detected["fov"],
        lod0_tile_size=64,
        lod0_step_deg=20.0,
        lod1_tile_size=64,
        lod1_step_deg=15.0,
        lod2_tile_size=0,
        lod2_step_deg=0.0,
        sheet_size=512,
    )
    assert res_extra["source_count"] == 2 + len(plan["planned_tiles"])
    assert res_extra["lod0_sectors"] == expected_lod0


