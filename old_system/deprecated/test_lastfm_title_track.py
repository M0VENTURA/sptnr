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


def test_has_title_track_featured_artist_stripped():
    """Test has_title_track strips featured artists before lookup"""
    print("\n" + "="*60)
    print("TEST: has_title_track - Featured Artist Stripped")
    print("="*60)
    
    from requests.exceptions import HTTPError
    
    # Create a mock Last.fm client
    client = LastFmClient(api_key="test_api_key")
    
    # First call: 404 for "dArtagnan"
    mock_404 = Mock()
    mock_404.status_code = 404
    mock_404.raise_for_status.side_effect = HTTPError(response=mock_404)
    
    # Second call: 200 for "dArtagnan feat. Melissa Bonny" (fallback)
    mock_200 = Mock()
    mock_200.json.return_value = {
        "album": {
            "name": "Herzblut",
            "tracks": {
                "track": [
                    {"name": "Herzblut"},
                    {"name": "Bonus Track"}
                ]
            }
        }
    }
    mock_200.raise_for_status = Mock()
    
    call_count = 0
    def mock_get(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        params = kwargs.get('params', {})
        lookup_artist = params.get('artist', '')
        
        if call_count == 1:
            assert lookup_artist == "dArtagnan", f"Expected 'dArtagnan' but got '{lookup_artist}'"
            return mock_404
        else:
            assert lookup_artist == "dArtagnan feat. Melissa Bonny", f"Expected 'dArtagnan feat. Melissa Bonny' but got '{lookup_artist}'"
            return mock_200
    
    client.session = Mock()
    client.session.get.side_effect = mock_get
    
    # Test
    result = client.has_title_track("dArtagnan feat. Melissa Bonny", "Herzblut")
    
    if result == True and call_count == 2:
        print("  ✅ Correctly stripped featured artist and fell back to full artist")
        return True
    else:
        print(f"  ❌ Failed: result={result}, call_count={call_count}")
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
        test_has_title_track_case_insensitive,
        test_has_title_track_featured_artist_stripped
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
