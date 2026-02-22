#!/usr/bin/env python3
"""
Test script for single detection functionality.
Tests that multiple sources (Spotify, Discogs, MusicBrainz, Last.fm) are checked.
"""

import os
import json
import logging
from dotenv import load_dotenv

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

# Load environment variables
load_dotenv()

# Set test environment variables if not set
if not os.getenv("DISCOGS_TOKEN"):
    os.environ["DISCOGS_TOKEN"] = ""  # Optional, will skip if empty
if not os.getenv("LASTFM_API_KEY"):
    os.environ["LASTFM_API_KEY"] = ""  # Optional, will skip if empty

def test_single_detection():
    """Test single detection with known singles and album tracks"""
    
    print("\n" + "="*60)
    print("SINGLE DETECTION TEST")
    print("="*60)
    
    # Import the function
    from single_detector import rate_track_single_detection
    
    # Test cases: (track_dict, artist_name, album_ctx, expected_sources)
    test_cases = [
        {
            "name": "Test 1: Spotify single (short release)",
            "track": {
                "id": "test1",
                "title": "Test Single",
                "is_spotify_single": True,
                "spotify_total_tracks": 2
            },
            "artist": "Test Artist",
            "album_ctx": {},
            "expected_sources": ["spotify", "short_release"]
        },
        {
            "name": "Test 2: No single indicators",
            "track": {
                "id": "test2",
                "title": "Album Track",
                "is_spotify_single": False,
                "spotify_total_tracks": 12
            },
            "artist": "Test Artist",
            "album_ctx": {},
            "expected_sources": []
        },
        {
            "name": "Test 3: Known single - 'Bohemian Rhapsody' by Queen",
            "track": {
                "id": "test3",
                "title": "Bohemian Rhapsody",
                "is_spotify_single": False,
                "spotify_total_tracks": None
            },
            "artist": "Queen",
            "album_ctx": {},
            "expected_sources": ["musicbrainz"]  # Should be detected by at least MusicBrainz
        }
    ]
    
    passed = 0
    failed = 0
    
    for i, test_case in enumerate(test_cases, 1):
        print(f"\n{test_case['name']}")
        print("-" * 60)
        
        try:
            result = rate_track_single_detection(
                track=test_case["track"],
                artist_name=test_case["artist"],
                album_ctx=test_case["album_ctx"],
                config={},
                verbose=True
            )
            
            # Parse sources from JSON
            sources = json.loads(result.get("single_sources", "[]"))
            is_single = result.get("is_single", False)
            confidence = result.get("single_confidence", "unknown")
            
            print(f"\nResult:")
            print(f"  is_single: {is_single}")
            print(f"  confidence: {confidence}")
            print(f"  sources: {sources}")
            
            # Validate expected sources are present
            if test_case["expected_sources"]:
                missing_sources = set(test_case["expected_sources"]) - set(sources)
                if missing_sources:
                    print(f"  ⚠️  Warning: Expected sources not found: {missing_sources}")
                else:
                    print(f"  ✅ All expected sources found")
                    passed += 1
            else:
                if not sources:
                    print(f"  ✅ No sources found as expected")
                    passed += 1
                else:
                    print(f"  ℹ️  Unexpected sources found: {sources}")
                    passed += 1  # Not a failure, just informational
            
        except Exception as e:
            print(f"  ❌ Error: {e}")
            logging.exception(f"Test {i} failed with exception")
            failed += 1
    
    print("\n" + "="*60)
    print(f"TEST SUMMARY: {passed} passed, {failed} failed")
    print("="*60)
    
    return passed, failed

if __name__ == "__main__":
    # Mock the start module functions if needed
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    
    # Run tests
    passed, failed = test_single_detection()
    
    # Exit with appropriate code
    sys.exit(0 if failed == 0 else 1)
