
import sys
import os
import yaml
import shutil

sys.path.append(os.getcwd())
# We need to mock FastAPI app or just test logic functions by importing them
from backend.main import get_presets, load_settings, save_settings, SETTINGS_USER_FILE

def test_presets():
    print("Testing presets...")
    presets = get_presets()
    if 'cameras' not in presets:
        print("FAIL: 'cameras' missing from presets")
        return False
    if 'ASI1600MM Pro' not in presets['cameras']:
        print("FAIL: Specific camera missing")
        return False
    if 'telescopes' not in presets:
        print("FAIL: 'telescopes' missing from presets")
        return False
    print("PASS: Presets loaded correctly")
    return True

def test_active_profile_persistence():
    print("Testing active profile persistence...")
    
    # 1. Clean user settings
    if os.path.exists(SETTINGS_USER_FILE): os.remove(SETTINGS_USER_FILE)
    
    # 2. Load defaults (active_profile should be null)
    settings = load_settings()
    if settings.get('active_profile') is not None:
        print(f"FAIL: Default active_profile is not None. Got: {settings.get('active_profile')}")
        return False
        
    # 3. Save active profile
    settings['active_profile'] = "TestProfile"
    save_settings(settings)
    
    # 4. Reload
    new_settings = load_settings()
    if new_settings.get('active_profile') != "TestProfile":
        print(f"FAIL: active_profile not saved. Got: {new_settings.get('active_profile')}")
        return False
    
    print("PASS: Active profile persistence successful")
    return True

if __name__ == "__main__":
    if os.path.exists(SETTINGS_USER_FILE): shutil.copy(SETTINGS_USER_FILE, SETTINGS_USER_FILE + ".bak")
    
    try:
        r1 = test_presets()
        r2 = test_active_profile_persistence()
        if r1 and r2:
            print("ALL TESTS PASSED")
        else:
            print("TESTS FAILED")
    except Exception as e:
        print(f"ERROR: {e}")
    finally:
         if os.path.exists(SETTINGS_USER_FILE + ".bak"):
            shutil.copy(SETTINGS_USER_FILE + ".bak", SETTINGS_USER_FILE)
            os.remove(SETTINGS_USER_FILE + ".bak")
