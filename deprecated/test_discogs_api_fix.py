#!/usr/bin/env python3
"""
Simple test for Discogs API lookup functionality.
Tests that the missing functions are now available and working.
"""

import sys
import os


def test_imports():
    """Test that the missing functions can now be imported."""
    print("="*60)
    print("Testing Discogs API Imports")
    print("="*60)
    
    try:
        from popularity import _discogs_search, _get_discogs_session
        print("✓ Successfully imported _discogs_search")
        print("✓ Successfully imported _get_discogs_session")
        return True
    except ImportError as e:
        print(f"✗ Import failed: {e}")
        return False


def test_session_creation():
    """Test that we can create a session."""
    print("\n" + "="*60)
    print("Testing Session Creation")
    print("="*60)
    
    try:
        from popularity import _get_discogs_session
        session = _get_discogs_session()
        print(f"✓ Session created: {type(session)}")
        return True
    except Exception as e:
        print(f"✗ Session creation failed: {e}")
        return False


def test_discogs_search_structure():
    """Test the search function structure (without making actual API call)."""
    print("\n" + "="*60)
    print("Testing Discogs Search Function Structure")
    print("="*60)
    
    try:
        from popularity import _discogs_search, _get_discogs_session
        import inspect
        
        # Check function signature
        sig = inspect.signature(_discogs_search)
        params = list(sig.parameters.keys())
        expected_params = ['session', 'headers', 'query', 'kind', 'per_page', 'timeout']
        
        print(f"Function parameters: {params}")
        
        # Verify all expected parameters exist
        for param in expected_params:
            if param in params:
                print(f"  ✓ Parameter '{param}' exists")
            else:
                print(f"  ✗ Parameter '{param}' missing")
                return False
        
        return True
    except Exception as e:
        print(f"✗ Structure test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_app_imports():
    """Test that app.py can now import the functions."""
    print("\n" + "="*60)
    print("Testing app.py Integration")
    print("="*60)
    
    try:
        # This simulates what app.py does
        from popularity import _discogs_search, _get_discogs_session
        
        session = _get_discogs_session()
        headers = {"User-Agent": "Sptnr/1.0"}
        
        # We won't actually call the API without a token, but we can verify the structure
        print("✓ Can create session and headers as app.py does")
        print("✓ Functions are ready to be called by app.py")
        return True
    except Exception as e:
        print(f"✗ Integration test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    print("\n" + "="*60)
    print("DISCOGS API LOOKUP FIX VALIDATION")
    print("="*60 + "\n")
    
    results = []
    results.append(("Imports", test_imports()))
    results.append(("Session Creation", test_session_creation()))
    results.append(("Search Function Structure", test_discogs_search_structure()))
    results.append(("App Integration", test_app_imports()))
    
    print("\n" + "="*60)
    print("TEST SUMMARY")
    print("="*60)
    
    all_passed = True
    for test_name, passed in results:
        status = "✓ PASS" if passed else "✗ FAIL"
        print(f"{status}: {test_name}")
        if not passed:
            all_passed = False
    
    print("="*60)
    
    if all_passed:
        print("\n✅ All tests passed! Discogs API lookup should now work.")
        sys.exit(0)
    else:
        print("\n❌ Some tests failed.")
        sys.exit(1)
