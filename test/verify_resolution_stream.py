import requests
import asyncio
import json

BASE_URL = "http://127.0.0.1:8000"

def test_stream_resolution():
    print("--- Testing Stream Resolution Parameter ---")
    
    # Construct stream URL with image_resolution=1024
    params = {
        "focal_length": 600,
        "sensor_width": 23.5,
        "sensor_height": 15.7,
        "latitude": 51.5,
        "longitude": 0.12,
        "catalogs": "messier",
        "sort_key": "time",
        "image_resolution": 1024, # TARGET
        "image_timeout": 60,
        "image_source": "dss2r",
        "min_altitude": -90, # Ensure objects
    }
    
    print("Requesting stream with image_resolution=1024...")
    try:
        with requests.get(f"{BASE_URL}/api/stream-objects", params=params, stream=True, timeout=5) as r:
            print(f"Status Code: {r.status_code}")
            # We can't easily parse the stream here to see the internal download URL used by worker.
            # But if the backend logs (which user sees) show 1024, it works.
            # We will just verify it connects.
            
            # However, we can check if 'setup_hash' in object_details contains '_r1024_'
            for line in r.iter_lines():
                if line:
                    decoded = line.decode('utf-8')
                    if decoded.startswith("event: object_details"):
                        # Read next line for data
                        continue 
                    if decoded.startswith("data: "):
                        try:
                            data = json.loads(decoded[6:])
                            if 'setup_hash' in data:
                                print(f"Received setup_hash: {data['setup_hash']}")
                                if "_r1024_" in data['setup_hash']:
                                    print("SUCCESS: setup_hash contains resolution 1024")
                                    return
                                else:
                                    print(f"FAILURE: setup_hash does not contain 1024. Got: {data['setup_hash']}")
                                    return
                        except:
                            pass
    except Exception as e:
        print(f"Stream error: {e}")

if __name__ == "__main__":
    test_stream_resolution()
