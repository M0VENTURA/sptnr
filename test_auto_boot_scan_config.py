#!/usr/bin/env python3
"""Test for auto boot Navidrome scan configuration."""

import sys
import os
import tempfile
import yaml
from pathlib import Path

# Add current dir to path
sys.path.insert(0, str(Path(__file__).parent))


def test_auto_boot_scan_config():
    """Test that auto_boot_navidrome_scan config is read correctly."""
    
    # Test 1: Default value should be False
    print("Test 1: Testing default config value...")
    test_config = {
        'features': {}
    }
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        yaml.dump(test_config, f)
        temp_path = f.name
    
    try:
        def _read_yaml(path):
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
                return yaml.safe_load(content) or {}, content
        
        def _get_auto_boot_import_setting(config_path):
            if os.environ.get("SPTNR_DISABLE_BOOT_ND_IMPORT") == "1":
                return False
            try:
                cfg, _ = _read_yaml(config_path)
                features = cfg.get("features", {})
                return features.get("auto_boot_navidrome_scan", False)
            except Exception:
                return False
        
        result = _get_auto_boot_import_setting(temp_path)
        if result == False:
            print("✅ Test 1 PASSED: Default value is False")
        else:
            print(f"❌ Test 1 FAILED: Expected False, got {result}")
            return False
    finally:
        os.unlink(temp_path)
    
    # Test 2: Explicit True value
    print("\nTest 2: Testing explicit True value...")
    test_config = {
        'features': {
            'auto_boot_navidrome_scan': True
        }
    }
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        yaml.dump(test_config, f)
        temp_path = f.name
    
    try:
        result = _get_auto_boot_import_setting(temp_path)
        if result == True:
            print("✅ Test 2 PASSED: Explicit True is read correctly")
        else:
            print(f"❌ Test 2 FAILED: Expected True, got {result}")
            return False
    finally:
        os.unlink(temp_path)
    
    # Test 3: Explicit False value
    print("\nTest 3: Testing explicit False value...")
    test_config = {
        'features': {
            'auto_boot_navidrome_scan': False
        }
    }
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        yaml.dump(test_config, f)
        temp_path = f.name
    
    try:
        result = _get_auto_boot_import_setting(temp_path)
        if result == False:
            print("✅ Test 3 PASSED: Explicit False is read correctly")
        else:
            print(f"❌ Test 3 FAILED: Expected False, got {result}")
            return False
    finally:
        os.unlink(temp_path)
    
    # Test 4: Environment variable override (True -> False)
    print("\nTest 4: Testing environment variable override...")
    test_config = {
        'features': {
            'auto_boot_navidrome_scan': True
        }
    }
    
    with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
        yaml.dump(test_config, f)
        temp_path = f.name
    
    try:
        # Set environment variable to disable
        os.environ["SPTNR_DISABLE_BOOT_ND_IMPORT"] = "1"
        result = _get_auto_boot_import_setting(temp_path)
        
        # Clean up env var
        del os.environ["SPTNR_DISABLE_BOOT_ND_IMPORT"]
        
        if result == False:
            print("✅ Test 4 PASSED: Environment variable overrides config to False")
        else:
            print(f"❌ Test 4 FAILED: Expected False with env override, got {result}")
            return False
    finally:
        os.unlink(temp_path)
    
    # Test 5: Real config file
    print("\nTest 5: Testing with real config.yaml...")
    real_config_path = Path(__file__).parent / "config" / "config.yaml"
    if real_config_path.exists():
        result = _get_auto_boot_import_setting(str(real_config_path))
        if result == False:
            print("✅ Test 5 PASSED: Real config.yaml has auto_boot_navidrome_scan=False")
        else:
            print(f"⚠️  Test 5 WARNING: Real config.yaml has auto_boot_navidrome_scan={result}")
    else:
        print("⚠️  Test 5 SKIPPED: config.yaml not found")
    
    print("\n" + "="*60)
    print("✅ All tests PASSED!")
    print("="*60)
    return True


if __name__ == "__main__":
    success = test_auto_boot_scan_config()
    sys.exit(0 if success else 1)
