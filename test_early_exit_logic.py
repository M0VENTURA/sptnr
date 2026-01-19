#!/usr/bin/env python3
"""
Test script to verify early exit logic in single detection.
Tests that Discogs is checked first and returns early when confirmed.
Tests that lookups stop after 2 confirmations.
"""

import json
import logging
from unittest.mock import MagicMock, patch, call

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

def test_discogs_early_exit():
    """Test that Discogs detection causes early exit when confirmed"""
    print("\n" + "="*60)
    print("TEST 1: Discogs Early Exit")
    print("="*60)
    
    from single_detector import rate_track_single_detection
    
    track = {
        "id": "test1",
        "title": "Test Single",
        "is_spotify_single": False,
        "spotify_total_tracks": None
    }
    
    # Mock the API calls - patch where they are imported/used
    with patch('api_clients.discogs.is_discogs_single') as mock_discogs, \
         patch('api_clients.musicbrainz.is_musicbrainz_single') as mock_mb, \
         patch('api_clients.lastfm.LastFmClient') as mock_lastfm, \
         patch('api_clients.discogs.has_discogs_video') as mock_video, \
         patch('config_loader.get_api_key') as mock_api_key, \
         patch('config_loader.is_api_enabled') as mock_enabled:
        
        # Setup mocks
        mock_api_key.return_value = "test_token"
        mock_enabled.return_value = True
        mock_discogs.return_value = True  # Discogs confirms single
        mock_mb.return_value = True  # Should NOT be called
        mock_video.return_value = False  # Should NOT be called
        
        # Run the function
        result = rate_track_single_detection(
            track=track,
            artist_name="Test Artist",
            album_ctx={},
            config={},
            verbose=True
        )
        
        # Verify Discogs was called
        assert mock_discogs.called, "Discogs should be called first"
        
        # Verify MusicBrainz was NOT called (early exit)
        assert not mock_mb.called, "MusicBrainz should NOT be called after Discogs confirms"
        
        # Verify video was NOT called (early exit)
        assert not mock_video.called, "Discogs video should NOT be called after Discogs confirms"
        
        # Verify result
        assert result["is_single"] == True, "Track should be marked as single"
        assert result["single_confidence"] == "high", "Confidence should be high"
        sources = json.loads(result["single_sources"])
        assert "discogs" in sources, "Discogs should be in sources"
        
        print("✅ PASSED: Discogs early exit works correctly")
        print(f"   Sources: {sources}")
        print(f"   Confidence: {result['single_confidence']}")

def test_two_confirmations_early_exit():
    """Test that lookups stop after 2 confirmations"""
    print("\n" + "="*60)
    print("TEST 2: Two Confirmations Early Exit")
    print("="*60)
    
    from single_detector import rate_track_single_detection
    
    track = {
        "id": "test2",
        "title": "Test Single",
        "is_spotify_single": True,  # First confirmation
        "spotify_total_tracks": 2
    }
    
    # Mock the API calls
    with patch('api_clients.discogs.is_discogs_single') as mock_discogs, \
         patch('api_clients.musicbrainz.is_musicbrainz_single') as mock_mb, \
         patch('api_clients.lastfm.LastFmClient') as mock_lastfm, \
         patch('api_clients.discogs.has_discogs_video') as mock_video, \
         patch('config_loader.get_api_key') as mock_api_key, \
         patch('config_loader.is_api_enabled') as mock_enabled:
        
        # Setup mocks
        mock_api_key.return_value = "test_token"
        mock_enabled.return_value = True
        mock_discogs.return_value = False  # Discogs does not confirm
        mock_mb.return_value = True  # MusicBrainz confirms (2nd confirmation with spotify)
        mock_video.return_value = False  # Should NOT be called
        
        # Run the function
        result = rate_track_single_detection(
            track=track,
            artist_name="Test Artist",
            album_ctx={},
            config={},
            verbose=True
        )
        
        # Verify Discogs was called
        assert mock_discogs.called, "Discogs should be called first"
        
        # Verify MusicBrainz was called
        assert mock_mb.called, "MusicBrainz should be called after Discogs fails"
        
        # Verify video was NOT called (early exit after 2 confirmations)
        assert not mock_video.called, "Discogs video should NOT be called after 2 confirmations"
        
        # Verify Last.fm was NOT called (early exit after 2 confirmations)
        assert not mock_lastfm.called, "Last.fm should NOT be called after 2 confirmations"
        
        # Verify result
        assert result["is_single"] == True, "Track should be marked as single"
        assert result["single_confidence"] == "high", "Confidence should be high"
        sources = json.loads(result["single_sources"])
        
        # Should have at least 2 sources (spotify, short_release, musicbrainz)
        assert len(sources) >= 2, f"Should have at least 2 sources, got {len(sources)}: {sources}"
        
        print("✅ PASSED: Two confirmations early exit works correctly")
        print(f"   Sources: {sources}")
        print(f"   Confidence: {result['single_confidence']}")

def test_all_lookups_when_no_early_exit():
    """Test that all lookups are performed when no early exit conditions are met"""
    print("\n" + "="*60)
    print("TEST 3: All Lookups When No Early Exit")
    print("="*60)
    
    from single_detector import rate_track_single_detection
    
    track = {
        "id": "test3",
        "title": "Album Track",
        "is_spotify_single": False,
        "spotify_total_tracks": 12
    }
    
    # Mock the API calls
    with patch('api_clients.discogs.is_discogs_single') as mock_discogs, \
         patch('api_clients.musicbrainz.is_musicbrainz_single') as mock_mb, \
         patch('api_clients.lastfm.LastFmClient') as mock_lastfm, \
         patch('api_clients.discogs.has_discogs_video') as mock_video, \
         patch('config_loader.get_api_key') as mock_api_key, \
         patch('config_loader.is_api_enabled') as mock_enabled:
        
        # Setup mocks
        mock_api_key.return_value = "test_token"
        mock_enabled.return_value = True
        mock_discogs.return_value = False
        mock_mb.return_value = False
        mock_video.return_value = False
        
        # Mock Last.fm client
        mock_lastfm_instance = MagicMock()
        mock_lastfm_instance.get_track_info.return_value = {"toptags": {"tag": []}}
        mock_lastfm.return_value = mock_lastfm_instance
        
        # Run the function
        result = rate_track_single_detection(
            track=track,
            artist_name="Test Artist",
            album_ctx={},
            config={},
            use_lastfm_single=True,
            verbose=True
        )
        
        # Verify all lookups were called
        assert mock_discogs.called, "Discogs should be called"
        assert mock_mb.called, "MusicBrainz should be called"
        assert mock_video.called, "Discogs video should be called"
        assert mock_lastfm.called, "Last.fm should be called"
        
        # Verify result
        assert result["is_single"] == False, "Track should NOT be marked as single"
        sources = json.loads(result["single_sources"])
        assert len(sources) == 0, "Should have no sources"
        
        print("✅ PASSED: All lookups performed when no early exit")
        print(f"   Sources: {sources}")
        print(f"   is_single: {result['is_single']}")

if __name__ == "__main__":
    try:
        test_discogs_early_exit()
        test_two_confirmations_early_exit()
        test_all_lookups_when_no_early_exit()
        
        print("\n" + "="*60)
        print("ALL TESTS PASSED ✅")
        print("="*60)
    except AssertionError as e:
        print(f"\n❌ TEST FAILED: {e}")
        import sys
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        import sys
        sys.exit(1)
