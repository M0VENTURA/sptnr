#!/usr/bin/env python3
"""
Test for Discogs single detection fix.

This test verifies that album tracks are not incorrectly marked as singles
when their separate single release exists in Discogs.

Specifically tests the fix for the issue where songs like Coldplay's "Viva la Vida"
from the album "Viva la Vida or Death and All His Friends" were incorrectly detected
as singles because Discogs has a separate single release of "Viva la Vida".
"""

import sys
from unittest.mock import Mock, patch

def test_album_track_not_detected_as_single():
    """
    Test that an album track is NOT detected as a single even when
    a separate single release exists in Discogs.
    """
    print("\n" + "="*80)
    print("DISCOGS ALBUM TRACK FIX TEST")
    print("="*80)
    
    from api_clients.discogs import DiscogsClient
    
    # Simulate the scenario:
    # - Searching for "Viva la Vida" by "Coldplay"
    # - Discogs returns the single release (1-2 tracks)
    # - The old logic would mark this as a single
    # - The new logic should NOT mark it as a single (because it lacks explicit "Single" format)
    
    mock_search_response = {
        "results": [
            {
                "id": 999999,
                "title": "Viva la Vida - Coldplay"
            }
        ]
    }
    
    # This is a mock of what Discogs returns for the "Viva la Vida" single release
    # It has 2 tracks but does NOT explicitly say "Single" in the formats
    mock_release_data = {
        "id": 999999,
        "title": "Viva la Vida",
        "formats": [
            {
                "name": "CD",  # NOT "Single" - just CD
                "descriptions": [""]  # No "Single" description
            }
        ],
        "tracklist": [
            {"position": "1", "title": "Viva la Vida", "duration": "4:01"},
            {"position": "2", "title": "Life in Technicolor ii", "duration": "4:05"}
        ],
        "artists": [{"name": "Coldplay"}],
        "videos": []  # No official video
    }
    
    client = DiscogsClient(token="test_token", enabled=True)
    
    with patch.object(client.session, 'get') as mock_get:
        # Setup mock responses
        def side_effect(url, *args, **kwargs):
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.raise_for_status = Mock()
            
            if '/database/search' in url:
                mock_response.json.return_value = mock_search_response
            elif '/releases/' in url:
                mock_response.json.return_value = mock_release_data
            
            return mock_response
        
        mock_get.side_effect = side_effect
        
        # Test the is_single method
        result = client.is_single(
            title="Viva la Vida",
            artist="Coldplay"
        )
        
        print(f"\nTest Case: Album track with separate single release")
        print(f"Track: Viva la Vida by Coldplay")
        print(f"Scenario: Discogs has a 2-track release (no explicit 'Single' format)")
        print(f"Expected: False (should NOT be detected as single)")
        print(f"Actual: {result}")
        
        if result:
            print("❌ FAIL: Album track incorrectly detected as single!")
            print("   The structural fallback is still matching 1-2 track releases.")
            return False
        else:
            print("✅ PASS: Album track correctly NOT detected as single")
            return True


def test_legitimate_single_still_detected():
    """
    Test that a legitimate single IS still detected when it has
    explicit "Single" format or other strong indicators.
    """
    print("\n" + "="*80)
    print("LEGITIMATE SINGLE DETECTION TEST")
    print("="*80)
    
    from api_clients.discogs import DiscogsClient
    
    mock_search_response = {
        "results": [
            {
                "id": 888888,
                "title": "Bohemian Rhapsody - Queen"
            }
        ]
    }
    
    # This release has explicit "Single" in the format
    mock_release_data = {
        "id": 888888,
        "title": "Bohemian Rhapsody",
        "formats": [
            {
                "name": "Vinyl",
                "descriptions": ["7\"", "Single", "45 RPM"]  # Explicit "Single"
            }
        ],
        "tracklist": [
            {"position": "A", "title": "Bohemian Rhapsody", "duration": "5:55"},
            {"position": "B", "title": "I'm In Love With My Car", "duration": "3:05"}
        ],
        "artists": [{"name": "Queen"}],
        "videos": []
    }
    
    client = DiscogsClient(token="test_token", enabled=True)
    
    with patch.object(client.session, 'get') as mock_get:
        def side_effect(url, *args, **kwargs):
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.raise_for_status = Mock()
            
            if '/database/search' in url:
                mock_response.json.return_value = mock_search_response
            elif '/releases/' in url:
                mock_response.json.return_value = mock_release_data
            
            return mock_response
        
        mock_get.side_effect = side_effect
        
        result = client.is_single(
            title="Bohemian Rhapsody",
            artist="Queen"
        )
        
        print(f"\nTest Case: Legitimate single with explicit format")
        print(f"Track: Bohemian Rhapsody by Queen")
        print(f"Scenario: Discogs has explicit 'Single' in format descriptions")
        print(f"Expected: True (should be detected as single)")
        print(f"Actual: {result}")
        
        if result:
            print("✅ PASS: Legitimate single correctly detected")
            return True
        else:
            print("❌ FAIL: Legitimate single NOT detected!")
            print("   The strong path detection may be broken.")
            return False


def test_ep_with_first_track_match():
    """
    Test that an EP with first track match is still detected.
    """
    print("\n" + "="*80)
    print("EP FIRST TRACK MATCH TEST")
    print("="*80)
    
    from api_clients.discogs import DiscogsClient
    
    mock_search_response = {
        "results": [
            {
                "id": 777777,
                "title": "Example Track - Artist"
            }
        ]
    }
    
    # EP with the matching track as first track
    mock_release_data = {
        "id": 777777,
        "title": "Example EP",
        "formats": [
            {
                "name": "CD",
                "descriptions": ["EP"]  # Explicit EP
            }
        ],
        "tracklist": [
            {"position": "1", "title": "Example Track", "duration": "3:30"},
            {"position": "2", "title": "Another Track", "duration": "3:45"},
            {"position": "3", "title": "Third Track", "duration": "4:00"}
        ],
        "artists": [{"name": "Artist"}],
        "videos": []
    }
    
    client = DiscogsClient(token="test_token", enabled=True)
    
    with patch.object(client.session, 'get') as mock_get:
        def side_effect(url, *args, **kwargs):
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.raise_for_status = Mock()
            
            if '/database/search' in url:
                mock_response.json.return_value = mock_search_response
            elif '/releases/' in url:
                mock_response.json.return_value = mock_release_data
            
            return mock_response
        
        mock_get.side_effect = side_effect
        
        result = client.is_single(
            title="Example Track",
            artist="Artist"
        )
        
        print(f"\nTest Case: EP with first track match")
        print(f"Track: Example Track by Artist")
        print(f"Scenario: EP with matching track as first track")
        print(f"Expected: True (should be detected as single)")
        print(f"Actual: {result}")
        
        if result:
            print("✅ PASS: EP first track correctly detected")
            return True
        else:
            print("❌ FAIL: EP first track NOT detected!")
            return False


if __name__ == "__main__":
    print("\nRunning Discogs Single Detection Fix Tests")
    print("=" * 80)
    
    test1_passed = test_album_track_not_detected_as_single()
    test2_passed = test_legitimate_single_still_detected()
    test3_passed = test_ep_with_first_track_match()
    
    print("\n" + "="*80)
    print("TEST SUMMARY")
    print("="*80)
    print(f"Album track fix: {'✅ PASS' if test1_passed else '❌ FAIL'}")
    print(f"Legitimate single: {'✅ PASS' if test2_passed else '❌ FAIL'}")
    print(f"EP first track: {'✅ PASS' if test3_passed else '❌ FAIL'}")
    
    all_passed = test1_passed and test2_passed and test3_passed
    
    if all_passed:
        print("\n🎉 All tests passed!")
        sys.exit(0)
    else:
        print("\n❌ Some tests failed")
        sys.exit(1)
