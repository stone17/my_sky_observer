import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import httpx
import json
import asyncio
import shutil
import subprocess
import time
import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch, AsyncMock, Mock

# It's better to test the app directly using the TestClient
from backend.main import app, load_settings, save_settings, CACHE_DIR
client = TestClient(app)

# --- Test Data ---
TEST_SETTINGS = {
    "telescope": {"focal_length": 800},
    "camera": {"sensor_width": 17.5, "sensor_height": 13},
    "location": {"latitude": 40.7128, "longitude": -74.0060}, # New York
    "catalogs": ["messier"],
    "min_altitude": 20.0,
    "sort_key": "size"
}

@pytest.fixture(scope="session", autouse=True)
def setup_and_teardown():
    # Back up user settings so running tests never clobbers the user's real settings_user.yaml
    backup_file = "settings_user.yaml.pytest_bak"
    had_user_settings = os.path.exists("settings_user.yaml")
    if had_user_settings:
        shutil.copy2("settings_user.yaml", backup_file)

    if os.path.exists("settings.json"):
        os.remove("settings.json")

    # Save test settings
    save_settings(TEST_SETTINGS)

    yield

    # Restore original user settings after tests finish
    if had_user_settings and os.path.exists(backup_file):
        shutil.move(backup_file, "settings_user.yaml")
    elif os.path.exists("settings_user.yaml"):
        os.remove("settings_user.yaml")
    if os.path.exists(backup_file):
        os.remove(backup_file)

def test_get_presets():
    """Tests the /api/presets endpoint."""
    response = client.get("/api/presets")
    assert response.status_code == 200
    data = response.json()
    assert "cameras" in data
    assert "ASI1600MM Pro" in data["cameras"]

def test_get_settings():
    """Tests reading settings via /api/settings."""
    response = client.get("/api/settings")
    assert response.status_code == 200
    retrieved_settings = response.json()
    # Compare focal length as a key indicator
    assert retrieved_settings["telescope"]["focal_length"] == TEST_SETTINGS["telescope"]["focal_length"]

def test_set_settings():
    """Tests writing settings via /api/settings."""
    new_settings = TEST_SETTINGS.copy()
    new_settings["telescope"]["focal_length"] = 1200
    
    response = client.post("/api/settings", json=new_settings)
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    
    # Verify the settings were actually saved
    loaded_settings = load_settings()
    assert loaded_settings["telescope"]["focal_length"] == 1200
    
    # Restore original test settings
    save_settings(TEST_SETTINGS)

def test_geocode_city():
    """Tests the /api/geocode endpoint with a valid city."""
    response = client.get("/api/geocode?city=London")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) > 0
    # Check for expected keys in the first result
    assert "name" in data[0]
    assert "country" in data[0]
    assert "latitude" in data[0]
    assert "longitude" in data[0]
    assert data[0]['name'] == 'London'

def test_geocode_city_invalid():
    """Tests the /api/geocode endpoint with a short/invalid query."""
    response = client.get("/api/geocode?city=X")
    assert response.status_code == 400
    assert "error" in response.json()

def test_nina_framing_endpoint():
    """Tests the /api/nina/framing endpoint validation."""
    # Test invalid request (missing fields)
    response = client.post("/api/nina/framing", json={})
    assert response.status_code == 422

    # Test valid request structure but likely fails connection (since NINA isn't running)
    # We expect a 502 Bad Gateway or similar if it tries to connect and fails
    payload = {
        "ra": "05h 34m 31.9s",
        "dec": "+22d 00m 52.2s",
        "rotation": 45.0
    }
    
    # The endpoint is async and attempts to contact localhost:1888.
    # In this test environment, nothing is listening on 1888.
    response = client.post("/api/nina/framing", json=payload)
    assert response.status_code == 502
    assert "Could not connect to N.I.N.A" in response.json()['detail']

# This is the most complex test. We need to run the server in a separate process
# because the streaming endpoint uses asyncio.to_thread which can cause issues
# with the TestClient's event loop management in some complex async scenarios.

def run_server_for_test():
    """Function to run the server for integration testing."""
    import uvicorn
    uvicorn.run("backend.main:app", host="127.0.0.1", port=8000, log_level="info")

@pytest.mark.asyncio
async def test_stream_and_download():
    """
    Integration test for the /api/stream-objects endpoint.
    """
    # 1. CLEANUP: Force delete cache to ensure download triggers
    if os.path.exists(CACHE_DIR):
        shutil.rmtree(CACHE_DIR)
    os.makedirs(CACHE_DIR, exist_ok=True)

    TEST_PORT = 8001
    # Start server in subprocess
    server_process = subprocess.Popen(
        ["python", "-c", f"import sys; sys.path.insert(0, '.'); import uvicorn; uvicorn.run('backend.main:app', host='127.0.0.1', port={TEST_PORT})"],
        stdout=subprocess.DEVNULL, # Suppress server noise in test output
        stderr=subprocess.DEVNULL
    )

    try:
        # Wait for the server to start
        is_server_ready = False
        for _ in range(20):
            try:
                async with httpx.AsyncClient() as async_client:
                    response = await async_client.get(f"http://127.0.0.1:{TEST_PORT}/api/catalogs")
                    if response.status_code == 200:
                        is_server_ready = True
                        break
            except Exception:
                await asyncio.sleep(0.5)

        if not is_server_ready:
            pytest.fail("Server failed to start.")

        # Test Logic
        download_verified = False
        params = {
            "focal_length": 1200, "sensor_width": 17.5, "sensor_height": 13,
            "latitude": 40.7, "longitude": -74.0, "catalogs": "messier",
            "sort_key": "size", "download_mode": "all" # Force downloads
        }

        async with httpx.AsyncClient(timeout=30) as http_client:
            async with http_client.stream("GET", f"http://127.0.0.1:{TEST_PORT}/api/stream-objects", params=params) as response:
                assert response.status_code == 200
                
                async for line in response.aiter_lines():
                    if not line.strip(): continue
                    
                    if line.startswith("event: image_status"):
                        # We found the event type, now get the data
                        continue 
                        
                    if line.startswith("data:"):
                        data_str = line.split(":", 1)[1].strip()
                        if not data_str: continue
                        
                        try:
                            data = json.loads(data_str)
                            # Check if this is the success event we want
                            if data.get("status") == "cached" and "url" in data:
                                print(f"Verified download for: {data['name']}")
                                download_verified = True
                                break
                        except:
                            pass

    finally:
        server_process.terminate()
        server_process.wait()

    assert download_verified, "Did not receive 'cached' status event for any image."

def test_fetch_custom_image_failure_html():
    """
    Tests that a 200 OK response containing HTML (SkyView error) raises a proper error.
    FIX: Mocks httpx instead of requests.
    """
    
    # We mock the return value of the async get call
    mock_resp = Mock()
    mock_resp.status_code = 200
    mock_resp.headers = {"Content-Type": "text/html"}
    mock_resp.text = "<html>Error: No data found</html>"
    mock_resp.raise_for_status = Mock()

    # Patch httpx.AsyncClient.get
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = mock_resp
        
        req = {"ra": "00h 00m 00s", "dec": "+00d 00m 00s", "fov": 1.0}
        resp = client.post("/api/fetch-custom-image", json=req)

        # Should be 500 because we raise ValueError -> HTTPException(500)
        assert resp.status_code == 500
        assert "SkyView returned text/html" in resp.json()["detail"]

def test_nina_framing_endpoint_mock():
    """Tests the NINA endpoint with mocked httpx for success."""
    from unittest.mock import patch, AsyncMock

    payload = {
        "ra": "05h 34m 31.9s",
        "dec": "+22d 00m 52.2s",
        "rotation": 45.0
    }

    # Ensure we patch where it is used, or httpx.AsyncClient directly if used as context manager
    # The backend code does: async with httpx.AsyncClient() as client:
    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {"Success": True}

        # Use the global client defined at the top level
        response = client.post("/api/nina/framing", json=payload)
        assert response.status_code == 200
        assert response.json() == {"status": "success", "message": "Sent to N.I.N.A"}

def test_fetch_custom_image_fov():
    """
    Tests that downloading images with different FOVs results in different images.
    Mocks the network call to avoid SkyView rate limits.
    """
    from unittest.mock import patch, Mock
    from PIL import Image
    import io

    # Generate valid JPEG bytes
    def create_dummy_jpeg(color, size=(10, 10)):
        img = Image.new('L', size, color=color)
        buf = io.BytesIO()
        img.save(buf, format='JPEG')
        return buf.getvalue()

    dummy_img_1 = create_dummy_jpeg(0)   # Black
    dummy_img_2 = create_dummy_jpeg(255) # White

    mock_resp = Mock()
    mock_resp.status_code = 200
    mock_resp.headers = {"Content-Type": "image/jpeg"}
    mock_resp.raise_for_status = Mock()

    def side_effect(url, timeout):
        # Return different content based on FOV/URL (simplified check)
        if "Size=5.0000" in url:
            mock_resp.content = dummy_img_2
        else:
            mock_resp.content = dummy_img_1
        return mock_resp

    with patch("requests.get", side_effect=side_effect):
        # M45 coordinates
        ra = "03h 47m 24s"
        dec = "+24d 07m 00s"

        # Request 1: FOV 1.0 degree
        req1 = {"ra": ra, "dec": dec, "fov": 1.0}
        resp1 = client.post("/api/fetch-custom-image", json=req1)
        assert resp1.status_code == 200
        url1 = resp1.json()["url"]

        # Request 2: FOV 5.0 degrees
        req2 = {"ra": ra, "dec": dec, "fov": 5.0}
        resp2 = client.post("/api/fetch-custom-image", json=req2)
        assert resp2.status_code == 200
        url2 = resp2.json()["url"]

        # Get the file paths from the URLs
        path1 = url1.replace("/cache/", "image_cache/", 1)
        path2 = url2.replace("/cache/", "image_cache/", 1)

        assert os.path.exists(path1)
        assert os.path.exists(path2)

        with open(path1, "rb") as f1: data1 = f1.read()
        with open(path2, "rb") as f2: data2 = f2.read()

        assert data1 != data2, "Images with different FOVs should differ in content"

def test_fetch_custom_image_with_special_chars():
    """
    Tests that downloading images with special characters in RA/Dec works (sanitization).
    Mocks network.
    """
    from unittest.mock import patch, Mock
    from PIL import Image
    import io

    # Generate valid JPEG bytes
    img = Image.new('L', (10, 10), color=128)
    buf = io.BytesIO()
    img.save(buf, format='JPEG')
    dummy_img = buf.getvalue()

    mock_resp = Mock()
    mock_resp.status_code = 200
    mock_resp.headers = {"Content-Type": "image/jpeg"}
    mock_resp.content = dummy_img
    mock_resp.raise_for_status = Mock()

    with patch("requests.get", return_value=mock_resp):
        # Coordinates with special chars
        ra = "17h 17m 07.3s"
        dec = "+43° 08' 11\""

        req = {"ra": ra, "dec": dec, "fov": 1.0}
        resp = client.post("/api/fetch-custom-image", json=req)
        assert resp.status_code == 200, f"Request failed: {resp.text}"
        url = resp.json()["url"]

        path = url.replace("/cache/", "image_cache/", 1)

        assert os.path.exists(path)
        assert '"' not in path
        assert '°' not in path


def test_stream_objects_preloads_altitude_graphs_even_when_saved_types_none():
    """
    Tests that passing selected_types='' ('All' types) overrides any saved ['__NONE__']
    in settings and streams preloaded altitude_graph data in object_details events.
    """
    from types import SimpleNamespace
    from backend.stream_service import apply_stream_query_overrides

    settings = {
        "selected_types": ["__NONE__"],
        "client_settings": {"selected_types": ["__NONE__"]},
    }
    dummy_req = SimpleNamespace(query_params={"selected_types": ""})
    apply_stream_query_overrides(
        settings,
        dummy_req,
        400.0,
        23.5,
        15.6,
        52.5,
        13.4,
        "messier",
        "time",
    )
    assert settings["selected_types"] == []
    assert settings["client_settings"]["selected_types"] == []

    params = (
        "focal_length=400&sensor_width=23.5&sensor_height=15.6"
        "&latitude=40.7&longitude=-74.0&catalogs=messier&sort_key=time"
        "&selected_types=&download_mode=none"
    )
    with client.stream("GET", f"/api/stream-objects?{params}") as response:
        assert response.status_code == 200
        details_events = []
        current_event = None
        for line in response.iter_lines():
            if not line:
                continue
            if line.startswith("event:"):
                current_event = line.split("event:", 1)[1].strip()
            elif line.startswith("data:") and current_event == "object_details":
                data_str = line.split("data:", 1)[1].strip()
                details_events.append(json.loads(data_str))
                if len(details_events) >= 3:
                    break

        assert len(details_events) >= 1, "Expected object_details events to be streamed"
        first_detail = details_events[0]
        assert "altitude_graph" in first_detail and len(first_detail["altitude_graph"]) > 0
        assert "moon_graph" in first_detail and len(first_detail["moon_graph"]) > 0
        assert "sun_graph" in first_detail and len(first_detail["sun_graph"]) > 0
        assert "hours_above_min" in first_detail


def test_smart_catalog_collation_and_aliases():
    """
    Tests that cross-catalog objects at the same coordinates or inside the same nebula
    complex (e.g., M 31 / NGC 0224 / UGC 00454 and Rosette Nebula NGC 2237 / 2238 / 2239 / 2244 / Sh2-275)
    are collated into a single primary target with all same-names in `aliases` and distinct
    sub-components in `sub_objects`.
    """
    from backend.data_manager import CatalogManager

    catalogs = CatalogManager()
    df = catalogs.get_all_objects(["messier", "ngc", "ic", "sharpless", "collinder", "ugc"])
    assert not df.empty

    # M 31, M 32, and M 110 should collate their NGC synonyms while remaining distinct Messier entries
    m31_rows = df[df["id"] == "M 31"]
    assert len(m31_rows) == 1
    assert "NGC 224" in m31_rows.iloc[0]["aliases"]
    assert df[df["id"] == "NGC 0224"].empty
    assert len(df[df["id"] == "M 32"]) == 1
    assert "NGC 221" in df[df["id"] == "M 32"].iloc[0]["aliases"]
    assert len(df[df["id"] == "M 110"]) == 1
    assert "NGC 205" in df[df["id"] == "M 110"].iloc[0]["aliases"]

    # Rosette Nebula (NGC 2237) should collate NGC 2238, NGC 2239, NGC 2244, NGC 2246, LBN 948
    rosette = df[df["id"].str.contains("2237")]
    assert len(rosette) == 1
    rosette_row = rosette.iloc[0]
    for expected_alias in ("NGC 2238", "NGC 2239", "NGC 2244", "NGC 2246", "LBN 948"):
        assert expected_alias in rosette_row["aliases"]
    assert isinstance(rosette_row["sub_objects"], list)
    assert len(rosette_row["sub_objects"]) >= 2

    # Separate Messier objects M 42 and M 43 must remain distinct primary targets
    assert len(df[df["id"] == "M 42"]) == 1
    assert len(df[df["id"] == "M 43"]) == 1


def test_time_window_hour_range_filter():
    """
    Tests the Hour Range (Time Window) filter in AstroCalculator and stream query overrides:
    - 6pm to midnight (18:00 to 00:00) window restricts visible hours
    - Fallback to astronomical night start/end when window strings are empty
    - Query param overrides for filter_active_time_window, time_window_start, time_window_end, tz_offset
    """
    from types import SimpleNamespace
    from astropy.time import Time
    from backend.astro_utils import AstroCalculator
    from backend.models import AltitudePoint
    from backend.stream_service import apply_stream_query_overrides

    calc = AstroCalculator()
    assert calc.parse_hhmm_to_minutes("18:00") == 1080
    assert calc.parse_hhmm_to_minutes("00:00") == 0
    assert calc.parse_hhmm_to_minutes("24:00") == 0
    assert calc.parse_hhmm_to_minutes("invalid") is None

    # Create a 13-point hourly grid from 16:00 UTC to 04:00 UTC next day (tz_offset=0)
    times = Time(
        [
            "2026-11-07T16:00:00Z",
            "2026-11-07T17:00:00Z",
            "2026-11-07T18:00:00Z",
            "2026-11-07T19:00:00Z",
            "2026-11-07T20:00:00Z",
            "2026-11-07T21:00:00Z",
            "2026-11-07T22:00:00Z",
            "2026-11-07T23:00:00Z",
            "2026-11-08T00:00:00Z",
            "2026-11-08T01:00:00Z",
            "2026-11-08T02:00:00Z",
            "2026-11-08T03:00:00Z",
            "2026-11-08T04:00:00Z",
        ]
    )

    # 18:00 to 00:00 (6pm till midnight) -> indices 2..8 (7 points: 18, 19, 20, 21, 22, 23, 00)
    mask_6pm_midnight = calc.build_time_window_mask(
        times=times,
        start_str="18:00",
        end_str="00:00",
        tz_offset_minutes=0,
    )
    assert list(mask_6pm_midnight) == [
        False,
        False,
        True,
        True,
        True,
        True,
        True,
        True,
        True,
        False,
        False,
        False,
        False,
    ]

    # Fallback to astronomical night when start_str and end_str are empty
    twilight = {"night": ["2026-11-07T19:00:00Z", "2026-11-08T02:00:00Z"]}
    mask_astro_default = calc.build_time_window_mask(
        times=times,
        start_str="",
        end_str="",
        tz_offset_minutes=0,
        twilight_periods=twilight,
    )
    assert list(mask_astro_default) == [
        False,
        False,
        False,
        True,
        True,
        True,
        True,
        True,
        True,
        True,
        True,
        False,
        False,
    ]

    # Check calculate_time_above_altitude with and without time window
    points = [AltitudePoint(time=t.isot + "Z", altitude=45.0) for t in times]
    full_hours = calc.calculate_time_above_altitude(points, min_altitude=30.0)
    window_hours = calc.calculate_time_above_altitude(
        points,
        min_altitude=30.0,
        time_window_active=True,
        time_window_start="18:00",
        time_window_end="00:00",
        tz_offset_minutes=0,
    )
    assert full_hours == 13.0
    assert window_hours == 7.0

    # Check query param overrides
    settings = {"client_settings": {}}
    dummy_req = SimpleNamespace(
        query_params={
            "filter_active_time_window": "true",
            "time_window_start": "18:00",
            "time_window_end": "00:00",
            "tz_offset": "-60",
        }
    )
    apply_stream_query_overrides(
        settings,
        dummy_req,
        400.0,
        23.5,
        15.6,
        52.5,
        13.4,
        "messier",
        "time",
    )
    assert settings["filter_active_time_window"] is True
    assert settings["client_settings"]["filter_active_time_window"] is True
    assert settings["time_window_start"] == "18:00"
    assert settings["time_window_end"] == "00:00"
    assert settings["tz_offset"] == -60.0


def test_cache_status_setup_hash_and_download_progress(tmp_path):
    """Tests setup-specific cache status counting and download_progress already_cached reporting."""
    from backend.image_service import get_cache_status_info

    setup_a = tmp_path / "fl400_sw23.5_sh15.6_px1024_DSS2Red"
    setup_b = tmp_path / "fov_custom_1.500_px1024_DSS2Red"
    setup_a.mkdir()
    setup_b.mkdir()

    (setup_a / "M31.jpg").write_bytes(b"a" * 1024)
    (setup_a / "M42.jpg").write_bytes(b"b" * 1024)
    (setup_b / "M45.jpg").write_bytes(b"c" * 1024)

    info_a = get_cache_status_info(str(tmp_path), setup_hash="fl400_sw23.5_sh15.6_px1024_DSS2Red")
    assert info_a["count"] == 3
    assert info_a["setup_count"] == 2
    assert info_a["setup_hash"] == "fl400_sw23.5_sh15.6_px1024_DSS2Red"

    info_b = get_cache_status_info(str(tmp_path), setup_hash="fov_custom_1.500_px1024_DSS2Red")
    assert info_b["count"] == 3
    assert info_b["setup_count"] == 1


def test_min_hours_altitude_limit():
    """
    Tests that min_hours_altitude controls the lower altitude limit for visible hours
    independently of min_altitude (peak altitude threshold).
    """
    from types import SimpleNamespace
    from backend.models import Camera, Location, Telescope
    from backend.stream_service import (
        _extract_min_hours_altitude,
        apply_stream_query_overrides,
        get_sorted_objects,
    )

    # 1. Query override parsing
    settings = {"min_altitude": 45.0, "client_settings": {}}
    dummy_req = SimpleNamespace(
        query_params={
            "min_altitude": "45.0",
            "min_hours": "2.0",
            "min_hours_altitude": "20.0",
        }
    )
    apply_stream_query_overrides(
        settings,
        dummy_req,
        400.0,
        23.5,
        15.6,
        40.7,
        -74.0,
        "messier",
        "hours_above",
    )
    assert settings["min_altitude"] == 45.0
    assert settings["min_hours"] == 2.0
    assert settings["min_hours_altitude"] == 20.0
    assert settings["client_settings"]["min_hours_altitude"] == 20.0
    assert _extract_min_hours_altitude(settings) == 20.0

    # 2. Fallback to min_altitude when min_hours_altitude is absent
    assert _extract_min_hours_altitude({"min_altitude": 35.0, "client_settings": {}}) == 35.0

    # 3. Verify get_sorted_objects computes more (or equal) visible hours at 15° than at 65°
    loc = Location(latitude=40.7128, longitude=-74.0060)
    tel = Telescope(focal_length=400.0)
    cam = Camera(sensor_width=23.5, sensor_height=15.6)

    low_limit_objs = get_sorted_objects(
        {
            "catalogs": ["messier"],
            "min_altitude": 40.0,
            "min_hours_altitude": 15.0,
            "client_settings": {"min_hours_altitude": 15.0},
        },
        loc,
        tel,
        cam,
    )
    high_limit_objs = get_sorted_objects(
        {
            "catalogs": ["messier"],
            "min_altitude": 40.0,
            "min_hours_altitude": 65.0,
            "client_settings": {"min_hours_altitude": 65.0},
        },
        loc,
        tel,
        cam,
    )

    low_map = {o["id"]: o["hours_visible"] for o in low_limit_objs}
    high_map = {o["id"]: o["hours_visible"] for o in high_limit_objs}
    assert any(low_map[k] > high_map[k] for k in low_map)




