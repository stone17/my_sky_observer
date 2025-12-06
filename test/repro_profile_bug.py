
import sys
import os
import yaml
import shutil
import json

sys.path.append(os.getcwd())
# We need to mock FastAPI app or just test logic functions by importing them
from backend.main import get_presets, load_settings, save_settings, SETTINGS_USER_FILE, SETTINGS_DEFAULT_FILE

def test_profile_persistence_logic():
    print("Testing profile persistence logic...")
    
    # 1. Setup clean env
    if os.path.exists(SETTINGS_USER_FILE): os.remove(SETTINGS_USER_FILE)
    
    # 2. Add Profile A
    settings = load_settings()
    if 'profiles' not in settings or settings['profiles'] is None:
        settings['profiles'] = {}
    settings['profiles']['ProfileA'] = {"foo": "bar"}
    save_settings(settings)
    
    # 3. Reload
    after_a = load_settings()
    if 'ProfileA' not in after_a.get('profiles', {}):
        print("FAIL: Profile A not saved")
        return False
        
    # 4. Add Profile B
    if 'profiles' not in after_a: after_a['profiles'] = {}
    after_a['profiles']['ProfileB'] = {"baz": "qux"}
    save_settings(after_a)
    
    # 5. Reload
    after_b = load_settings()
    profiles = after_b.get('profiles', {})
    
    print(f"Profiles found: {list(profiles.keys())}")
    
    if 'ProfileA' not in profiles:
        print("FAIL: Profile A lost after adding Profile B")
        return False
    if 'ProfileB' not in profiles:
        print("FAIL: Profile B not saved")
        return False
        
    print("PASS: Profile persistence verified")
    return True

if __name__ == "__main__":
    # Backup
    if os.path.exists(SETTINGS_USER_FILE): shutil.copy(SETTINGS_USER_FILE, SETTINGS_USER_FILE + ".bak")
    
    try:
        if test_profile_persistence_logic():
            print("ALL TESTS PASSED")
        else:
            print("TESTS FAILED")
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
    finally:
         if os.path.exists(SETTINGS_USER_FILE + ".bak"):
            shutil.copy(SETTINGS_USER_FILE + ".bak", SETTINGS_USER_FILE)
            os.remove(SETTINGS_USER_FILE + ".bak")
