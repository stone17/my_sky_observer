import requests
import yaml
import json
import time

BASE_URL = "http://127.0.0.1:8000"

def test_settings_persistence():
    print("--- Testing Settings Persistence ---")
    
    # 1. Read current settings
    res = requests.get(f"{BASE_URL}/api/settings")
    if res.status_code != 200:
        print("Failed to get settings")
        return
    
    current_settings = res.json()
    print(f"Current Resolution: {current_settings.get('image_server', {}).get('resolution')}")
    
    # 2. Update Resolution to 1024
    new_settings = current_settings.copy()
    if 'image_server' not in new_settings:
        new_settings['image_server'] = {}
    
    new_settings['image_server']['resolution'] = 1024
    new_settings['image_server']['timeout'] = 120
    
    print("Sending update with Resolution 1024...")
    res = requests.post(f"{BASE_URL}/api/settings", json=new_settings)
    if res.status_code != 200:
        print(f"Failed to save settings: {res.text}")
        return
        
    # Wait for file write
    time.sleep(1)
    
    # 3. Verify via API
    res = requests.get(f"{BASE_URL}/api/settings")
    updated_settings = res.json()
    res_val = updated_settings.get('image_server', {}).get('resolution')
    print(f"Updated Resolution (via API): {res_val}")
    
    if res_val == 1024:
        print("SUCCESS: Backend saved resolution correctly.")
    else:
        print("FAILURE: Backend did not save resolution.")

if __name__ == "__main__":
    test_settings_persistence()
