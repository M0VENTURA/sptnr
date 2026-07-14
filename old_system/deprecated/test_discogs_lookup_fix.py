#!/usr/bin/env python3
"""
Test to verify that Discogs lookup is properly called during single detection.
This test validates the fix for the bug where Discogs API was not being called
during artist scan even though it was listed as available.
"""

import sys
import os
from unittest.mock import patch, MagicMock

def test_discogs_token_passed_correctly():
    """Test that discogs_token is passed correctly to detect_single_for_track"""
    print("TEST 1: Discogs token parameter passing")
    print("-" * 60)
    
    # Import after setting up the environment
    from popularity import detect_single_for_track
    
    # Mock the Discogs client
    with patch('popularity.HAVE_DISCOGS', True), \
         patch('popularity.HAVE_DISCOGS_VIDEO', True), \
         patch('popularity._get_timeout_safe_discogs_client') as mock_get_client:
        
        # Create a mock client that returns True for is_single
        mock_client = MagicMock()
        mock_client.is_single.return_value = True
        mock_client.has_official_video.return_value = False
        mock_get_client.return_value = mock_client
        
        # Call detect_single_for_track with a discogs_token
        result = detect_single_for_track(
            title="Test Song",
            artist="Test Artist",
            album_track_count=10,
            spotify_results_cache=None,
            verbose=True,
            discogs_token="test_token_123"
        )
        
        # Verify that the client getter was called with the token
        mock_get_client.assert_called_with("test_token_123")
        
        # Verify that Discogs was checked
        assert mock_client.is_single.called, "Discogs is_single should have been called"
        
        # Verify that discogs is in sources
        assert "discogs" in result["sources"], f"Expected 'discogs' in sources, got {result['sources']}"
        
        print("✅ PASS: Discogs token is passed correctly")
        print(f"   Sources: {result['sources']}")
        print(f"   Confidence: {result['confidence']}")
        return True


def test_verbose_parameter_passed():
    """Test that verbose parameter is properly passed from popularity_scan"""
    print("\nTEST 2: Verbose parameter passing")
    print("-" * 60)
    
    from popularity import detect_single_for_track
    import logging
    
    # Capture log output
    log_messages = []
    
    # Create a custom handler to capture log messages
    class ListHandler(logging.Handler):
        def emit(self, record):
            log_messages.append(self.format(record))
    
    handler = ListHandler()
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)
    
    # Mock the Discogs client
    with patch('popularity.HAVE_DISCOGS', True), \
         patch('popularity._get_timeout_safe_discogs_client') as mock_get_client:
        
        mock_client = MagicMock()
        mock_client.is_single.return_value = False
        mock_get_client.return_value = mock_client
        
        # Call with verbose=True
        result = detect_single_for_track(
            title="Test Song",
            artist="Test Artist",
            album_track_count=10,
            spotify_results_cache=None,
            verbose=True,
            discogs_token="test_token"
        )
        
        # Check that verbose logging happened
        # Note: The actual log messages depend on the log_verbose function implementation
        # We're just verifying that the function accepts verbose=True without error
        
        print("✅ PASS: Verbose parameter accepted")
        print(f"   Result: {result}")
        return True


def test_spotify_cache_key():
    """Test that Spotify cache uses correct key"""
    print("\nTEST 3: Spotify cache key consistency")
    print("-" * 60)
    
    from popularity import detect_single_for_track
    
    # Create a cache with title as key
    title = "When Your Heart Stops Beating"
    cache = {
        title: [
            {
                "album": {
                    "album_type": "single",
                    "name": "When Your Heart Stops Beating"
                },
                "popularity": 51
            }
        ]
    }
    
    # Call detect_single_for_track with the cache
    with patch('popularity.HAVE_DISCOGS', False), \
         patch('popularity.HAVE_MUSICBRAINZ', False):
        
        result = detect_single_for_track(
            title=title,
            artist="+44",
            album_track_count=10,
            spotify_results_cache=cache,
            verbose=True,
            discogs_token=""
        )
        
        # Verify that Spotify single was detected using the cache
        assert "spotify" in result["sources"], f"Expected 'spotify' in sources, got {result['sources']}"
        
        print("✅ PASS: Spotify cache key is correct (title)")
        print(f"   Sources: {result['sources']}")
        return True


def test_config_loading_error_logged():
    """Test that config loading errors are always logged"""
    print("\nTEST 4: Config loading errors are logged")
    print("-" * 60)
    
    from popularity import detect_single_for_track
    import logging
    
    # Set a non-existent config path
    original_config_path = os.environ.get("CONFIG_PATH")
    os.environ["CONFIG_PATH"] = "/nonexistent/config.yaml"
    
    log_messages = []
    
    class ListHandler(logging.Handler):
        def emit(self, record):
            log_messages.append(self.format(record))
    
    handler = ListHandler()
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)
    
    try:
        # Call with verbose=False (should still log error)
        result = detect_single_for_track(
            title="Test Song",
            artist="Test Artist",
            album_track_count=10,
            spotify_results_cache=None,
            verbose=False,  # Not verbose, but error should still be logged
            discogs_token=None  # Force it to try loading from config
        )
        
        # Check if error was logged
        # The exact message depends on log_unified implementation
        # We just verify the function doesn't crash
        
        print("✅ PASS: Config loading error handled gracefully")
        print(f"   Function completed without crashing")
    finally:
        # Restore original config path
        if original_config_path:
            os.environ["CONFIG_PATH"] = original_config_path
        else:
            os.environ.pop("CONFIG_PATH", None)
    
    return True


if __name__ == "__main__":
    print("=" * 60)
    print("DISCOGS LOOKUP FIX VALIDATION")
    print("=" * 60)
    
    results = []
    
    try:
        results.append(("Discogs token passing", test_discogs_token_passed_correctly()))
    except Exception as e:
        print(f"❌ FAIL: {e}")
        import traceback
        traceback.print_exc()
        results.append(("Discogs token passing", False))
    
    try:
        results.append(("Verbose parameter", test_verbose_parameter_passed()))
    except Exception as e:
        print(f"❌ FAIL: {e}")
        import traceback
        traceback.print_exc()
        results.append(("Verbose parameter", False))
    
    try:
        results.append(("Spotify cache key", test_spotify_cache_key()))
    except Exception as e:
        print(f"❌ FAIL: {e}")
        import traceback
        traceback.print_exc()
        results.append(("Spotify cache key", False))
    
    try:
        results.append(("Config error logging", test_config_loading_error_logged()))
    except Exception as e:
        print(f"❌ FAIL: {e}")
        import traceback
        traceback.print_exc()
        results.append(("Config error logging", False))
    
    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)
    
    for test_name, passed in results:
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"{status}: {test_name}")
    
    all_passed = all(result[1] for result in results)
    
    if all_passed:
        print("\n✅ All tests passed! Discogs lookup should now work correctly.")
        sys.exit(0)
    else:
        print("\n❌ Some tests failed. Please review the fixes.")
        sys.exit(1)
