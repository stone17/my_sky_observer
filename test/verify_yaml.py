
import sys
import os
import json
import yaml
import shutil

sys.path.append(os.getcwd())
from backend.main import load_settings, save_settings, SETTINGS_USER_FILE, SETTINGS_JSON_LEGACY

def test_yaml_migration():
    print("Testing YAML migration...")
    
    # 1. Clean state
    if os.path.exists(SETTINGS_USER_FILE): os.remove(SETTINGS_USER_FILE)
    if os.path.exists(SETTINGS_JSON_LEGACY): os.remove(SETTINGS_JSON_LEGACY)
    
    # 2. Create legacy JSON
    legacy_data = {
        "telescope": {"focal_length": 999},
        "client_settings": {"min_hours": 3.0}
    }
    with open(SETTINGS_JSON_LEGACY, 'w') as f:
        json.dump(legacy_data, f)
        
    # 3. Load settings (should trigger migration)
    settings = load_settings()
    
    # Verify migration
    if not os.path.exists(SETTINGS_USER_FILE):
        print("FAIL: settings_user.yaml not created")
        return False
        
    if settings['telescope']['focal_length'] != 999:
        print(f"FAIL: Legacy value not loaded. Got {settings['telescope']['focal_length']}")
        return False
        
    if settings.get('client_settings', {}).get('min_hours') != 3.0:
        print("FAIL: Legacy nested value not loaded")
        return False
        
    # Verify file content
    with open(SETTINGS_USER_FILE, 'r') as f:
        user_yaml = yaml.safe_load(f)
        if user_yaml['telescope']['focal_length'] != 999:
            print("FAIL: YAML file content incorrect")
            return False
            
    print("PASS: Migration successful")
    return True

def test_user_override():
    print("Testing user override...")
    
    # Update settings via save (mocking UI update)
    settings = load_settings()
    settings['telescope']['focal_length'] = 500
    save_settings(settings)
    
    # Reload
    new_settings = load_settings()
    if new_settings['telescope']['focal_length'] != 500:
        print("FAIL: User override not saved/loaded")
        return False
        
    # Verify default is NOT 500 (read default file directly)
    with open("settings_default.yaml", 'r') as f:
        defaults = yaml.safe_load(f)
        if defaults['telescope']['focal_length'] == 500:
            print("FAIL: Default file was modified!")
            return False
            
    print("PASS: User override successful")
    return True

if __name__ == "__main__":
    # Backup
    if os.path.exists(SETTINGS_USER_FILE): shutil.copy(SETTINGS_USER_FILE, SETTINGS_USER_FILE + ".bak")
    if os.path.exists(SETTINGS_JSON_LEGACY): shutil.copy(SETTINGS_JSON_LEGACY, SETTINGS_JSON_LEGACY + ".bak")
    
    try:
        r1 = test_yaml_migration()
        r2 = test_user_override()
        if r1 and r2:
            print("ALL TESTS PASSED")
        else:
            print("TESTS FAILED")
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # Restore
        if os.path.exists(SETTINGS_USER_FILE + ".bak"):
            shutil.copy(SETTINGS_USER_FILE + ".bak", SETTINGS_USER_FILE)
            os.remove(SETTINGS_USER_FILE + ".bak")
        if os.path.exists(SETTINGS_JSON_LEGACY + ".bak"):
            shutil.copy(SETTINGS_JSON_LEGACY + ".bak", SETTINGS_JSON_LEGACY)
            os.remove(SETTINGS_JSON_LEGACY + ".bak")
