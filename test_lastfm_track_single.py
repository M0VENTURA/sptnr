"""
Test Last.fm check_track_as_single method.

This test verifies that the Last.fm client can correctly identify
when a track exists as a single/album on Last.fm by checking if
an album with the same name as the track exists.
"""

import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from api_clients.lastfm import LastFmClient
from unittest.mock import Mock, patch
import json


def test_track_exists_as_single():
    """Test when a track exists as a single on Last.fm"""
    client = LastFmClient(api_key="test_key")
    
    # Mock response for a track that exists as a single
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "album": {
            "name": "Shape of You",
            "artist": "Ed Sheeran",
            "tracks": {
                "track": [
                    {"name": "Shape of You"},
                    {"name": "Shape of You (Acoustic)"}
                ]
            }
        }
    }
    
    with patch.object(client.session, 'get', return_value=mock_response):
        result = client.check_track_as_single("Ed Sheeran", "Shape of You")
        assert result is True, "Should return True when track exists as single"
        print("✓ Test 1 passed: Track exists as single")


def test_track_not_exists_as_single():
    """Test when a track does NOT exist as a single on Last.fm (404 response)"""
    client = LastFmClient(api_key="test_key")
    
    # Mock 404 response
    mock_response = Mock()
    mock_response.status_code = 404
    
    from requests.exceptions import HTTPError
    mock_response.raise_for_status.side_effect = HTTPError(response=mock_response)
    
    with patch.object(client.session, 'get', return_value=mock_response):
        result = client.check_track_as_single("Adema", "The Losers")
        assert result is False, "Should return False when track doesn't exist as single"
        print("✓ Test 2 passed: Track doesn't exist as single (404)")


def test_track_exists_but_name_mismatch():
    """Test when album exists but name doesn't match track title"""
    client = LastFmClient(api_key="test_key")
    
    # Mock response where album name doesn't match track title
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "album": {
            "name": "Kill the Headlights",  # Different from track title
            "artist": "Adema",
            "tracks": {
                "track": [
                    {"name": "The Losers"},
                    {"name": "Other Track"}
                ]
            }
        }
    }
    
    with patch.object(client.session, 'get', return_value=mock_response):
        result = client.check_track_as_single("Adema", "The Losers")
        assert result is False, "Should return False when album name doesn't match track title"
        print("✓ Test 3 passed: Album name doesn't match track title")


def test_case_insensitive_matching():
    """Test that matching is case-insensitive"""
    client = LastFmClient(api_key="test_key")
    
    # Mock response with different case
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "album": {
            "name": "SHAPE OF YOU",  # Different case
            "artist": "Ed Sheeran",
            "tracks": {
                "track": [{"name": "Shape of You"}]
            }
        }
    }
    
    with patch.object(client.session, 'get', return_value=mock_response):
        result = client.check_track_as_single("Ed Sheeran", "shape of you")  # Lower case
        assert result is True, "Should match case-insensitively"
        print("✓ Test 4 passed: Case-insensitive matching")


def test_no_api_key():
    """Test when API key is missing"""
    client = LastFmClient(api_key=None)
    
    result = client.check_track_as_single("Artist", "Track")
    assert result is False, "Should return False when API key is missing"
    print("✓ Test 5 passed: Missing API key handled")


if __name__ == "__main__":
    print("Running Last.fm track single detection tests...\n")
    
    test_track_exists_as_single()
    test_track_not_exists_as_single()
    test_track_exists_but_name_mismatch()
    test_case_insensitive_matching()
    test_no_api_key()
    
    print("\n✅ All 5 tests passed!")
