import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import httpx
import json
import asyncio
import subprocess
import time
import pytest
from fastapi.testclient import TestClient

# It's better to test the app directly using the TestClient
from backend.main import app, load_settings, save_settings

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
    # Before tests run, create a known state
    # Ensure no old cache or settings interfere
    if os.path.exists("settings.json"):
        os.remove("settings.json")
    
    # Save test settings
    save_settings(TEST_SETTINGS)

    # Clear the image cache for a clean test run
    if os.path.exists("image_cache"):
        for setup_dir in os.listdir("image_cache"):
            full_setup_dir = os.path.join("image_cache", setup_dir)
            if os.path.isdir(full_setup_dir):
                for f in os.listdir(full_setup_dir):
                    os.remove(os.path.join(full_setup_dir, f))
                os.rmdir(full_setup_dir)
    
    yield
    # Teardown (after tests) is not strictly necessary here, 
    # but you could add cleanup if needed.

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
    - Starts the real server in a subprocess.
    - Connects to the streaming endpoint.
    - Verifies that object data is received.
    - Verifies that an image download is triggered and completes.
    - Verifies that the cached image is accessible.
    """
    # Use a different port to avoid conflicts with running instances
    TEST_PORT = 8001
    server_process = subprocess.Popen(["python", "-c", f"import sys; sys.path.insert(0, '.'); import uvicorn; uvicorn.run('backend.main:app', host='127.0.0.1', port={TEST_PORT})"])
    
    # Wait for the server to start
    is_server_ready = False
    for _ in range(20): # Try for 10 seconds
        try:
            async with httpx.AsyncClient() as async_client:
                response = await async_client.get(f"http://127.0.0.1:{TEST_PORT}/")
                if response.status_code == 200:
                    is_server_ready = True
                    break
        except httpx.ConnectError:
            await asyncio.sleep(0.5)

    if not is_server_ready:
        server_process.terminate()
        server_process.wait()
        pytest.fail(f"Server failed to start on port {TEST_PORT} within the timeout period.")

    download_verified = False
    try:
        params = {
            "focal_length": TEST_SETTINGS["telescope"]["focal_length"],
            "sensor_width": TEST_SETTINGS["camera"]["sensor_width"],
            "sensor_height": TEST_SETTINGS["camera"]["sensor_height"],
            "latitude": TEST_SETTINGS["location"]["latitude"],
            "longitude": TEST_SETTINGS["location"]["longitude"],
            "catalogs": ",".join(TEST_SETTINGS["catalogs"]),
            "sort_key": TEST_SETTINGS["sort_key"],
            "min_altitude": TEST_SETTINGS["min_altitude"],
        }
        
        async with httpx.AsyncClient(timeout=180) as client: # Generous timeout
            async with client.stream("GET", f"http://127.0.0.1:{TEST_PORT}/api/stream-objects", params=params) as response:
                response.raise_for_status()
                
                event_name = None
                async for line in response.aiter_lines():
                    if line.startswith("event:"):
                        event_name = line.split(":", 1)[1].strip()
                        continue
                    
                    if not line.startswith("data:"):
                        continue
                    
                    data_str = line.split(":", 1)[1].strip()
                    if data_str == "Stream complete":
                        break
                        
                    event_data = json.loads(data_str)

                    # We are looking for the image_status event for a cached image
                    if event_name == "image_status" and event_data.get("status") == "cached":
                        print(f"-> Verified cached status for {event_data['name']}")
                        image_url = event_data.get("url")
                        assert image_url is not None

                        # 1. Verify the file exists on disk
                        # URL is like /cache/hash/filename.jpg
                        file_path = image_url.replace("/cache/", "image_cache/", 1)
                        assert os.path.exists(file_path), f"Cached file not found at {file_path}"
                        print(f"   - SUCCESS: File found at {file_path}")

                        # 2. Verify we can fetch the image from the cache endpoint
                        img_response = await client.get(f"http://127.0.0.1:{TEST_PORT}{image_url}")
                        assert img_response.status_code == 200, f"Cache endpoint returned {img_response.status_code}"
                        assert len(img_response.content) > 1000 # Check it's a real image
                        print(f"   - SUCCESS: Endpoint {image_url} is accessible.")

                        download_verified = True
                        break # Exit after one successful verification
    
    finally:
        # Ensure the server is terminated
        server_process.terminate()
        server_process.wait()

    assert download_verified, "Failed to verify image download and caching within the test."

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

def test_fetch_custom_image_failure_html():
    """
    Tests that a 200 OK response containing HTML (SkyView error) raises a proper error.
    """
    from unittest.mock import patch, Mock

    # Mock requests.get to return HTML
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.headers = {"Content-Type": "text/html"}
    mock_response.text = "<html>Error: No data found</html>"
    mock_response.content = b"<html>Error: No data found</html>"
    mock_response.raise_for_status = Mock()

    with patch("requests.get", return_value=mock_response):
        req = {"ra": "00h 00m 00s", "dec": "+00d 00m 00s", "fov": 1.0}
        resp = client.post("/api/fetch-custom-image", json=req)

        # It should fail with 500 (since we raise ValueError which bubbles up)
        # Ideally we could catch it and return 400/422, but currently it's 500.
        assert resp.status_code == 500
        assert "SkyView returned non-image data" in resp.json()['detail']
