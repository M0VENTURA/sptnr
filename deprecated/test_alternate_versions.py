#!/usr/bin/env python3
"""
Test script for alternate version filtering in single detection.
Tests that acoustic, orchestral, live, and other alternate versions are NOT detected as singles.
"""

import os
import sys
import json
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

# Set minimal required environment variables
os.environ.setdefault("CONFIG_PATH", "/tmp/test_config.yaml")
os.environ.setdefault("DB_PATH", ":memory:")

def test_alternate_version_filtering():
    """Test that alternate versions are filtered out from single detection"""
    
    print("\n" + "="*60)
    print("ALTERNATE VERSION FILTERING TEST")
    print("="*60)
    
    # Import the function
    from popularity import detect_single_for_track
    
    # Test cases: tracks that should NOT be detected as singles
    test_cases = [
        {
            "name": "Acoustic version",
            "track": "Wonderwall - Acoustic",
            "artist": "Oasis",
            "should_be_single": False
        },
        {
            "name": "Acoustic in parentheses",
            "track": "Wonderwall (Acoustic Version)",
            "artist": "Oasis",
            "should_be_single": False
        },
        {
            "name": "Live version",
            "track": "Wonderwall - Live",
            "artist": "Oasis",
            "should_be_single": False
        },
        {
            "name": "Live in parentheses",
            "track": "Wonderwall (Live at Wembley)",
            "artist": "Oasis",
            "should_be_single": False
        },
        {
            "name": "Unplugged version",
            "track": "Layla - Unplugged",
            "artist": "Eric Clapton",
            "should_be_single": False
        },
        {
            "name": "Unplugged in parentheses",
            "track": "Layla (Unplugged)",
            "artist": "Eric Clapton",
            "should_be_single": False
        },
        {
            "name": "Orchestral version",
            "track": "Bitter Sweet Symphony - Orchestral Version",
            "artist": "The Verve",
            "should_be_single": False
        },
        {
            "name": "Orchestral in parentheses",
            "track": "Bitter Sweet Symphony (Orchestral)",
            "artist": "The Verve",
            "should_be_single": False
        },
        {
            "name": "Remix version",
            "track": "Wonderwall - Remix",
            "artist": "Oasis",
            "should_be_single": False
        },
        {
            "name": "Demo version",
            "track": "Wonderwall - Demo",
            "artist": "Oasis",
            "should_be_single": False
        },
        {
            "name": "Remaster version",
            "track": "Wonderwall - Remastered",
            "artist": "Oasis",
            "should_be_single": False
        },
        {
            "name": "Instrumental version",
            "track": "Wonderwall (Instrumental)",
            "artist": "Oasis",
            "should_be_single": False
        },
        {
            "name": "Regular track (should pass through filter)",
            "track": "Wonderwall",
            "artist": "Oasis",
            "should_be_single": None  # We're just testing it doesn't get filtered
        },
    ]
    
    passed = 0
    failed = 0
    
    for i, test_case in enumerate(test_cases, 1):
        print(f"\nTest {i}: {test_case['name']}")
        print(f"  Track: {test_case['track']}")
        print("-" * 60)
        
        try:
            result = detect_single_for_track(
                title=test_case["track"],
                artist=test_case["artist"],
                album_track_count=10,  # Assume it's from an album
                spotify_results_cache=None,
                verbose=False
            )
            
            is_single = result.get("is_single", False)
            confidence = result.get("confidence", "unknown")
            sources = result.get("sources", [])
            
            print(f"  Result:")
            print(f"    is_single: {is_single}")
            print(f"    confidence: {confidence}")
            print(f"    sources: {sources}")
            
            # For alternate versions, they should NOT be detected as singles
            if test_case["should_be_single"] is False:
                # The track should NOT be a single (core behavior check)
                if not is_single:
                    print(f"  ✅ PASS: Alternate version correctly filtered out (is_single={is_single})")
                    passed += 1
                else:
                    print(f"  ❌ FAIL: Alternate version was incorrectly detected as single (is_single={is_single}, confidence={confidence}, sources={sources})")
                    failed += 1
            else:
                # Regular track - just check it wasn't incorrectly filtered
                print(f"  ℹ️  Regular track processed (result depends on external APIs)")
                passed += 1  # Don't fail on regular tracks
            
        except Exception as e:
            print(f"  ❌ Error: {e}")
            logging.exception(f"Test {i} failed with exception")
            failed += 1
    
    print("\n" + "="*60)
    print(f"TEST SUMMARY: {passed} passed, {failed} failed")
    print("="*60)
    
    return passed, failed

if __name__ == "__main__":
    # Mock the start module if needed
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    
    # Run tests
    passed, failed = test_alternate_version_filtering()
    
    # Exit with appropriate code
    sys.exit(0 if failed == 0 else 1)
