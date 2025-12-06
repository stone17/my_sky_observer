
import sys
import os
import json
import shutil

# Mock constants or import
sys.path.append(os.getcwd())
from backend.main import load_settings, SETTINGS_FILE

def test_settings_migration():
    print("Testing settings migration...")
    
    # Create a mock settings file with old structure
    old_settings = {
        "telescope": {"focal_length": 600},
        "min_hours": 5.0, # Old location
        "client_settings": {
            "max_magnitude": 10
        }
    }
    
    with open(SETTINGS_FILE, 'w') as f:
        json.dump(old_settings, f)
        
    # Load settings
    settings = load_settings()
    
    # Verify migration
    if 'min_hours' in settings:
        print("FAIL: min_hours still at root")
        return False
        
    if settings.get('client_settings', {}).get('min_hours') != 5.0:
        print(f"FAIL: min_hours not moved to client_settings correctly. Got: {settings.get('client_settings', {}).get('min_hours')}")
        return False
        
    print("PASS: Migration successful")
    return True

def test_defaults():
    print("Testing defaults...")
    if os.path.exists(SETTINGS_FILE):
        os.remove(SETTINGS_FILE)
        
    settings = load_settings()
    
    if settings['client_settings']['min_hours'] != 0.0:
        print("FAIL: Default min_hours incorrect")
        return False
        
    print("PASS: Defaults successful")
    return True

if __name__ == "__main__":
    # Backup existing settings
    if os.path.exists(SETTINGS_FILE):
        shutil.copy(SETTINGS_FILE, SETTINGS_FILE + ".bak")
        
    try:
        r1 = test_settings_migration()
        r2 = test_defaults()
        if r1 and r2:
            print("ALL TESTS PASSED")
        else:
            print("TESTS FAILED")
    finally:
        # Restore settings
        if os.path.exists(SETTINGS_FILE + ".bak"):
            shutil.copy(SETTINGS_FILE + ".bak", SETTINGS_FILE)
            os.remove(SETTINGS_FILE + ".bak")
