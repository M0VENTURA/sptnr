#!/usr/bin/env python3
"""
Test Discogs track matching with punctuation in titles.

This test verifies that tracks with apostrophes and other punctuation
can be correctly matched when searching Discogs (e.g., "Janie's Got A Gun").
"""

import sys
from unittest.mock import Mock, patch
from api_clients.discogs import DiscogsClient
from discogs_singles_cache import normalize_track_title


def test_normalize_track_title_with_apostrophe():
    """Test that normalize_track_title handles apostrophes correctly."""
    print("\n" + "="*80)
    print("TEST: normalize_track_title with apostrophes")
    print("="*80)
    
    test_cases = [
        ("Janie's Got A Gun", "janies got a gun"),
        ("Don't Stop Believin'", "dont stop believin"),
        ("I'm Alive", "im alive"),
        ("You've Got A Friend", "youve got a friend"),
        ("What's Going On", "whats going on"),
    ]
    
    for original, expected in test_cases:
        normalized = normalize_track_title(original)
        print(f"  '{original}' -> '{normalized}'")
        assert normalized == expected, f"Expected '{expected}', got '{normalized}'"
    
    print("✓ All normalizations correct")
    return True


def test_discogs_search_with_apostrophe():
    """Test that Discogs search works with apostrophes in track titles."""
    print("\n" + "="*80)
    print("TEST: Discogs search with apostrophe in track title")
    print("="*80)
    
    # Mock Discogs API response
    mock_search_response = {
        "results": [
            {
                "id": 123456,
                "type": "release",
                "title": "Aerosmith - Janie's Got A Gun",
                "format": ["Vinyl", "7\"", "Single"]
            }
        ]
    }
    
    client = DiscogsClient(token="test_token", enabled=True)
    
    with patch.object(client.session, 'get') as mock_get:
        # Setup mock response
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.raise_for_status = Mock()
        mock_response.json.return_value = mock_search_response
        mock_get.return_value = mock_response
        
        # Test the search
        print("\n  Testing: is_single('Janie's Got A Gun', 'Aerosmith')")
        result = client.is_single(title="Janie's Got A Gun", artist="Aerosmith")
        
        # Verify the API was called correctly
        assert mock_get.called, "API was not called"
        call_args = mock_get.call_args
        
        print(f"  API called: {call_args[0][0]}")
        print(f"  Params: {call_args[1]['params']}")
        
        # Check that the track parameter was passed
        params = call_args[1]['params']
        assert 'track' in params, "track parameter not passed"
        print(f"  Track param: '{params['track']}'")
        
        # Verify the result
        print(f"  Result: {result}")
        assert result == True, f"Expected True (single found), got {result}"
    
    print("✓ Discogs search with apostrophe works correctly")
    return True


def test_discogs_search_with_various_punctuation():
    """Test that Discogs search works with various punctuation marks."""
    print("\n" + "="*80)
    print("TEST: Discogs search with various punctuation")
    print("="*80)
    
    test_cases = [
        ("Janie's Got A Gun", "Aerosmith - Janie's Got A Gun"),
        ("Don't Stop Believin'", "Journey - Don't Stop Believin'"),
        ("What's Love Got to Do with It", "Tina Turner - What's Love Got to Do with It"),
        ("You've Got A Friend", "James Taylor - You've Got A Friend"),
    ]
    
    client = DiscogsClient(token="test_token", enabled=True)
    
    for track_title, result_title in test_cases:
        mock_search_response = {
            "results": [
                {
                    "id": 123456,
                    "type": "release",
                    "title": result_title,
                    "format": ["Vinyl", "7\"", "Single"]
                }
            ]
        }
        
        with patch.object(client.session, 'get') as mock_get:
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.raise_for_status = Mock()
            mock_response.json.return_value = mock_search_response
            mock_get.return_value = mock_response
            
            artist = result_title.split(" - ")[0]
            print(f"\n  Testing: '{track_title}' by '{artist}'")
            result = client.is_single(title=track_title, artist=artist)
            print(f"    Result: {result}")
            
            # Verify the API call
            params = mock_get.call_args[1]['params']
            print(f"    Track param sent: '{params['track']}'")
            
            assert result == True, f"Expected True for '{track_title}', got {result}"
    
    print("\n✓ All punctuation cases handled correctly")
    return True


def run_all_tests():
    """Run all tests."""
    try:
        test_normalize_track_title_with_apostrophe()
        test_discogs_search_with_apostrophe()
        test_discogs_search_with_various_punctuation()
        
        print("\n" + "="*80)
        print("✅ ALL TESTS PASSED")
        print("="*80)
        return True
    except Exception as e:
        print(f"\n❌ TEST FAILED: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    result = run_all_tests()
    sys.exit(0 if result else 1)
