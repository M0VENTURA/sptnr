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
    """Test when a track exists as a single on Last.fm with < 6 tracks"""
    client = LastFmClient(api_key="test_key")
    
    # Mock response for a track that exists as a single with 2 tracks (< 6)
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
        assert result is True, "Should return True when track exists as single with < 6 tracks"
        print("✓ Test 1 passed: Track exists as single with < 6 tracks")


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
    
    # Mock response with different case and < 6 tracks
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


def test_track_exists_but_too_many_tracks():
    """Test when track exists as album title but has >= 6 tracks (should be FALSE)"""
    client = LastFmClient(api_key="test_key")
    
    # Mock response for a track that matches album name but has 10 tracks (>= 6)
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "album": {
            "name": "Greatest Hits",
            "artist": "Test Artist",
            "tracks": {
                "track": [
                    {"name": "Greatest Hits"},
                    {"name": "Track 2"},
                    {"name": "Track 3"},
                    {"name": "Track 4"},
                    {"name": "Track 5"},
                    {"name": "Track 6"},
                    {"name": "Track 7"},
                    {"name": "Track 8"},
                    {"name": "Track 9"},
                    {"name": "Track 10"}
                ]
            }
        }
    }
    
    with patch.object(client.session, 'get', return_value=mock_response):
        result = client.check_track_as_single("Test Artist", "Greatest Hits")
        assert result is False, "Should return False when track exists but has >= 6 tracks"
        print("✓ Test 6 passed: Track with >= 6 tracks correctly rejected")


def test_track_exists_with_exactly_5_tracks():
    """Test when track exists with exactly 5 tracks (should be TRUE)"""
    client = LastFmClient(api_key="test_key")
    
    # Mock response for a track that matches album name with exactly 5 tracks
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "album": {
            "name": "EP Release",
            "artist": "Test Artist",
            "tracks": {
                "track": [
                    {"name": "EP Release"},
                    {"name": "Track 2"},
                    {"name": "Track 3"},
                    {"name": "Track 4"},
                    {"name": "Track 5"}
                ]
            }
        }
    }
    
    with patch.object(client.session, 'get', return_value=mock_response):
        result = client.check_track_as_single("Test Artist", "EP Release")
        assert result is True, "Should return True when track exists with exactly 5 tracks"
        print("✓ Test 7 passed: Track with exactly 5 tracks correctly accepted")


def test_featured_artist_stripped():
    """Test that featured artists are stripped before Last.fm lookup"""
    client = LastFmClient(api_key="test_key")
    
    from requests.exceptions import HTTPError
    
    # First call: 404 for "dArtagnan feat. Melissa Bonny"
    mock_404 = Mock()
    mock_404.status_code = 404
    mock_404.raise_for_status.side_effect = HTTPError(response=mock_404)
    
    # Second call: 200 for "dArtagnan" (primary artist)
    mock_200 = Mock()
    mock_200.status_code = 200
    mock_200.json.return_value = {
        "album": {
            "name": "Herzblut",
            "artist": "dArtagnan",
            "tracks": {
                "track": [
                    {"name": "Herzblut"},
                    {"name": "Herzblut (Acoustic)"}
                ]
            }
        }
    }
    
    call_count = 0
    def mock_get(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        params = kwargs.get('params', {})
        lookup_artist = params.get('artist', '')
        
        # First call should be with the primary artist
        if call_count == 1:
            assert lookup_artist == "dArtagnan", f"Expected 'dArtagnan' but got '{lookup_artist}'"
            return mock_200
        else:
            # Fallback call with full artist string
            assert lookup_artist == "dArtagnan feat. Melissa Bonny", f"Expected 'dArtagnan feat. Melissa Bonny' but got '{lookup_artist}'"
            return mock_404
    
    with patch.object(client.session, 'get', side_effect=mock_get):
        result = client.check_track_as_single("dArtagnan feat. Melissa Bonny", "Herzblut")
        assert result is True, "Should find single after stripping featured artist"
        assert call_count == 1, f"Should only need 1 call (primary artist found), but made {call_count}"
        print("✓ Test 8 passed: Featured artist stripped, single found on first try")


def test_featured_artist_fallback():
    """Test fallback to full artist when primary artist doesn't match"""
    client = LastFmClient(api_key="test_key")
    
    from requests.exceptions import HTTPError
    
    # First call: 404 for "dArtagnan"
    mock_404 = Mock()
    mock_404.status_code = 404
    mock_404.raise_for_status.side_effect = HTTPError(response=mock_404)
    
    # Second call: 200 for "dArtagnan feat. Melissa Bonny"
    mock_200 = Mock()
    mock_200.status_code = 200
    mock_200.json.return_value = {
        "album": {
            "name": "Herzblut",
            "artist": "dArtagnan feat. Melissa Bonny",
            "tracks": {
                "track": [
                    {"name": "Herzblut"}
                ]
            }
        }
    }
    
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
    
    with patch.object(client.session, 'get', side_effect=mock_get):
        result = client.check_track_as_single("dArtagnan feat. Melissa Bonny", "Herzblut")
        assert result is True, "Should find single after falling back to full artist"
        assert call_count == 2, f"Should need 2 calls (fallback), but made {call_count}"
        print("✓ Test 9 passed: Featured artist stripped, fallback to full artist works")


if __name__ == "__main__":
    print("Running Last.fm track single detection tests...\n")
    
    test_track_exists_as_single()
    test_track_not_exists_as_single()
    test_track_exists_but_name_mismatch()
    test_case_insensitive_matching()
    test_no_api_key()
    test_track_exists_but_too_many_tracks()
    test_track_exists_with_exactly_5_tracks()
    test_featured_artist_stripped()
    test_featured_artist_fallback()
    
    print("\n✅ All 9 tests passed!")
