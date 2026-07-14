#!/usr/bin/env python3
"""
Test to verify that the Discogs token field name in config is correctly used.

This test validates the fix for the bug where the code was checking for
'personal_token' field but the config.yaml uses 'token' field.
"""
import sys
import yaml


def test_discogs_config_field_name():
    """Test that config.yaml uses 'token' field for Discogs, not 'personal_token'."""
    print("Testing Discogs config field name...")
    
    # Load config.yaml
    config_path = "/home/runner/work/sptnr/sptnr/config/config.yaml"
    try:
        with open(config_path, 'r') as f:
            config = yaml.safe_load(f)
    except Exception as e:
        print(f"❌ Failed to load config.yaml: {e}")
        return False
    
    # Check that Discogs config exists
    discogs_config = config.get("api_integrations", {}).get("discogs", {})
    assert discogs_config, "Discogs config not found in api_integrations"
    print("✓ Discogs config found in api_integrations")
    
    # Check that 'token' field exists
    assert "token" in discogs_config, "Field 'token' not found in Discogs config"
    print("✓ Field 'token' found in Discogs config")
    
    # Check that 'personal_token' field does NOT exist (old bug)
    assert "personal_token" not in discogs_config, "Field 'personal_token' should not exist in Discogs config"
    print("✓ Field 'personal_token' correctly not used in Discogs config")
    
    # Verify the correct structure
    expected_fields = ["enabled", "token"]
    for field in expected_fields:
        assert field in discogs_config, f"Expected field '{field}' not found in Discogs config"
    print(f"✓ All expected fields present: {expected_fields}")
    
    print("\n✅ Discogs config field name test passed!")
    return True


def test_popularity_code_uses_token():
    """Test that popularity.py code uses 'token' field, not 'personal_token'."""
    print("\nTesting popularity.py uses correct field name...")
    
    with open("/home/runner/work/sptnr/sptnr/popularity.py", 'r') as f:
        content = f.read()
    
    # Check that 'personal_token' is NOT used for Discogs
    assert 'discogs_config.get("personal_token")' not in content, \
        "popularity.py should not check for 'personal_token' field"
    print("✓ popularity.py does not use 'personal_token' field")
    
    # Check that 'token' IS used for Discogs
    assert 'discogs_config.get("token")' in content, \
        "popularity.py should check for 'token' field"
    print("✓ popularity.py correctly uses 'token' field")
    
    print("\n✅ popularity.py field name test passed!")
    return True


if __name__ == '__main__':
    try:
        test_discogs_config_field_name()
        test_popularity_code_uses_token()
        print("\n" + "="*60)
        print("ALL TESTS PASSED")
        print("="*60)
        sys.exit(0)
    except AssertionError as e:
        print(f"\n❌ Test failed: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
