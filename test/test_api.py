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
    server_process = subprocess.Popen(["python", "-c", "import sys; sys.path.insert(0, '.'); import uvicorn; uvicorn.run('backend.main:app', host='127.0.0.1', port=8000)"])
    
    # Wait for the server to start
    is_server_ready = False
    for _ in range(10): # Try for 5 seconds
        try:
            async with httpx.AsyncClient() as async_client:
                response = await async_client.get("http://127.0.0.1:8000/")
                if response.status_code == 200:
                    is_server_ready = True
                    break
        except httpx.ConnectError:
            await asyncio.sleep(0.5)

    assert is_server_ready, "Server failed to start within the timeout period."

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
            async with client.stream("GET", "http://127.0.0.1:8000/api/stream-objects", params=params) as response:
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
                        img_response = await client.get(f"http://127.0.0.1:8000{image_url}")
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
