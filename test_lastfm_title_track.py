#!/usr/bin/env python3
"""
Test Last.fm Title Track Detection
===================================

Tests the new Last.fm title track detection for singles released as EPs.
"""

import os
import sys
import logging
from unittest.mock import Mock, MagicMock

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Configure logging
logging.basicConfig(level=logging.DEBUG, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

from api_clients.lastfm import LastFmClient


def test_has_title_track_single_track():
    """Test has_title_track with a single track matching album name"""
    print("\n" + "="*60)
    print("TEST: has_title_track - Single Track Match")
    print("="*60)
    
    # Create a mock Last.fm client
    client = LastFmClient(api_key="test_api_key")
    
    # Mock the session.get response
    mock_response = Mock()
    mock_response.json.return_value = {
        "album": {
            "name": "Test Single",
            "tracks": {
                "track": {
                    "name": "Test Single"
                }
            }
        }
    }
    mock_response.raise_for_status = Mock()
    
    client.session = Mock()
    client.session.get.return_value = mock_response
    
    # Test
    result = client.has_title_track("Test Artist", "Test Single")
    
    if result == True:
        print("  ✅ Correctly detected title track in single-track album")
        return True
    else:
        print("  ❌ Failed to detect title track")
        return False


def test_has_title_track_multiple_tracks_with_match():
    """Test has_title_track with multiple tracks, one matching album name"""
    print("\n" + "="*60)
    print("TEST: has_title_track - Multiple Tracks with Match")
    print("="*60)
    
    # Create a mock Last.fm client
    client = LastFmClient(api_key="test_api_key")
    
    # Mock the session.get response
    mock_response = Mock()
    mock_response.json.return_value = {
        "album": {
            "name": "The Hit Single",
            "tracks": {
                "track": [
                    {"name": "The Hit Single"},
                    {"name": "B-Side Track"},
                    {"name": "Bonus Track"}
                ]
            }
        }
    }
    mock_response.raise_for_status = Mock()
    
    client.session = Mock()
    client.session.get.return_value = mock_response
    
    # Test
    result = client.has_title_track("Test Artist", "The Hit Single")
    
    if result == True:
        print("  ✅ Correctly detected title track in multi-track album")
        return True
    else:
        print("  ❌ Failed to detect title track")
        return False


def test_has_title_track_no_match():
    """Test has_title_track with no matching track"""
    print("\n" + "="*60)
    print("TEST: has_title_track - No Match")
    print("="*60)
    
    # Create a mock Last.fm client
    client = LastFmClient(api_key="test_api_key")
    
    # Mock the session.get response
    mock_response = Mock()
    mock_response.json.return_value = {
        "album": {
            "name": "Album Name",
            "tracks": {
                "track": [
                    {"name": "Track One"},
                    {"name": "Track Two"},
                    {"name": "Track Three"}
                ]
            }
        }
    }
    mock_response.raise_for_status = Mock()
    
    client.session = Mock()
    client.session.get.return_value = mock_response
    
    # Test
    result = client.has_title_track("Test Artist", "Album Name")
    
    if result == False:
        print("  ✅ Correctly returned False for no title track")
        return True
    else:
        print("  ❌ Incorrectly detected title track")
        return False


def test_has_title_track_case_insensitive():
    """Test has_title_track with case-insensitive matching"""
    print("\n" + "="*60)
    print("TEST: has_title_track - Case Insensitive")
    print("="*60)
    
    # Create a mock Last.fm client
    client = LastFmClient(api_key="test_api_key")
    
    # Mock the session.get response
    mock_response = Mock()
    mock_response.json.return_value = {
        "album": {
            "name": "TITLE TRACK",
            "tracks": {
                "track": [
                    {"name": "title track"},
                    {"name": "Other Track"}
                ]
            }
        }
    }
    mock_response.raise_for_status = Mock()
    
    client.session = Mock()
    client.session.get.return_value = mock_response
    
    # Test
    result = client.has_title_track("Test Artist", "title track")
    
    if result == True:
        print("  ✅ Correctly matched title track with different case")
        return True
    else:
        print("  ❌ Failed case-insensitive matching")
        return False


def run_all_tests():
    """Run all tests"""
    print("\n" + "="*70)
    print("RUNNING LAST.FM TITLE TRACK DETECTION TESTS")
    print("="*70)
    
    tests = [
        test_has_title_track_single_track,
        test_has_title_track_multiple_tracks_with_match,
        test_has_title_track_no_match,
        test_has_title_track_case_insensitive
    ]
    
    passed = 0
    failed = 0
    
    for test in tests:
        try:
            if test():
                passed += 1
            else:
                failed += 1
        except Exception as e:
            print(f"  ❌ Test {test.__name__} raised exception: {e}")
            failed += 1
    
    print("\n" + "="*70)
    print(f"RESULTS: {passed} passed, {failed} failed")
    print("="*70)
    
    return failed == 0


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
