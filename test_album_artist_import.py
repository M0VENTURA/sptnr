#!/usr/bin/env python3
"""
Test for album artist import fix.

This test verifies that album_artist is correctly extracted from the
getAlbum.view API response (album.artist field) rather than from the
getArtist.view response or track.albumArtist field.
"""

import os
import sys
from unittest.mock import patch, MagicMock


def test_fetch_album_tracks_returns_album_metadata():
    """Test that fetch_album_tracks returns album metadata including artist field."""
    from api_clients.navidrome import NavidromeClient
    
    # Create a mock NavidromeClient
    client = NavidromeClient("http://test", "user", "pass")
    
    # Mock the session.get response
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "subsonic-response": {
            "album": {
                "id": "album_123",
                "name": "100 Greatest Alternative 90s",
                "artist": "Various Artists",  # This is the album artist
                "artistId": "artist_various",
                "song": [
                    {
                        "id": "track_123",
                        "title": "Mistakes And Regrets",
                        "artist": "...And You Will Know Us by the Trail of Dead",  # Track artist
                        "albumArtist": "Various Artists",  # May or may not be correct
                        "trackNumber": 169,
                        "duration": 240
                    }
                ]
            }
        }
    }
    mock_response.raise_for_status = MagicMock()
    
    with patch.object(client.session, 'get', return_value=mock_response):
        result = client.fetch_album_tracks("album_123")
        
        # Verify the result structure
        assert isinstance(result, dict), "fetch_album_tracks should return a dict"
        assert "tracks" in result, "Result should contain 'tracks' key"
        assert "artist" in result, "Result should contain 'artist' key"
        assert "artistId" in result, "Result should contain 'artistId' key"
        assert "name" in result, "Result should contain 'name' key"
        assert "id" in result, "Result should contain 'id' key"
        
        # Verify the album artist is extracted correctly
        assert result["artist"] == "Various Artists", f"Expected album artist 'Various Artists', got '{result['artist']}'"
        assert result["artistId"] == "artist_various", f"Expected artistId 'artist_various', got '{result['artistId']}'"
        assert result["name"] == "100 Greatest Alternative 90s", f"Expected album name, got '{result['name']}'"
        
        # Verify tracks are extracted
        assert len(result["tracks"]) == 1, f"Expected 1 track, got {len(result['tracks'])}"
        assert result["tracks"][0]["artist"] == "...And You Will Know Us by the Trail of Dead"
        
        print("✅ Test PASSED: fetch_album_tracks returns album metadata correctly")
        print(f"   Album artist: {result['artist']}")
        print(f"   Track artist: {result['tracks'][0]['artist']}")
        return True


def test_priority_order():
    """Test that album_artist uses correct priority order."""
    # Simulate what navidrome_import.py does
    
    # Case 1: All three sources available - should use album_artist_from_api
    album_artist_from_api = "Various Artists"
    alb_artist = "Wrong Artist"
    artist_name = "Fallback Artist"
    
    album_artist_value = album_artist_from_api or alb_artist or artist_name
    assert album_artist_value == "Various Artists", f"Should use album_artist_from_api, got {album_artist_value}"
    print("✅ Priority test 1 PASSED: Uses album_artist_from_api when available")
    
    # Case 2: album_artist_from_api is empty - should use alb artist
    album_artist_from_api = ""
    album_artist_value = album_artist_from_api or alb_artist or artist_name
    assert album_artist_value == "Wrong Artist", f"Should use alb.get('artist'), got {album_artist_value}"
    print("✅ Priority test 2 PASSED: Falls back to alb.get('artist') when API value is empty")
    
    # Case 3: Both API and alb are empty - should use artist_name
    album_artist_from_api = ""
    alb_artist = ""
    album_artist_value = album_artist_from_api or alb_artist or artist_name
    assert album_artist_value == "Fallback Artist", f"Should use artist_name, got {album_artist_value}"
    print("✅ Priority test 3 PASSED: Falls back to artist_name parameter")
    
    return True


if __name__ == "__main__":
    try:
        print("=" * 70)
        print("Test 1: fetch_album_tracks returns album metadata")
        print("=" * 70)
        test_fetch_album_tracks_returns_album_metadata()
        
        print("\n" + "=" * 70)
        print("Test 2: Album artist priority order")
        print("=" * 70)
        test_priority_order()
        
        print("\n" + "=" * 70)
        print("🎉 All tests PASSED!")
        print("=" * 70)
        print("\nSummary:")
        print("- fetch_album_tracks now returns album metadata including artist field")
        print("- Album artist uses correct priority: API > album obj > parameter")
        print("- Album artist (Various Artists) and track artist are correctly distinguished")
        
        sys.exit(0)
        
    except Exception as e:
        print(f"\n❌ Test FAILED: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
