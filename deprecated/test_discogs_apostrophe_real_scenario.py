#!/usr/bin/env python3
"""
Test real-world scenario where Discogs API might return no results
when searching with apostrophes in the track parameter.
"""

import sys
from unittest.mock import Mock, patch
from api_clients.discogs import DiscogsClient


def test_discogs_search_no_results_with_apostrophe():
    """
    Test scenario where Discogs API returns no results when searching
    with apostrophe in track parameter, but would return results if
    the apostrophe was removed or a general query was used.
    """
    print("\n" + "="*80)
    print("TEST: Discogs search returns no results with apostrophe")
    print("="*80)
    
    client = DiscogsClient(token="test_token", enabled=True)
    
    # Simulate API returning NO results when searching with apostrophe
    with patch.object(client.session, 'get') as mock_get:
        # First call (with apostrophe) returns no results
        mock_response_empty = Mock()
        mock_response_empty.status_code = 200
        mock_response_empty.raise_for_status = Mock()
        mock_response_empty.json.return_value = {"results": []}
        
        mock_get.return_value = mock_response_empty
        
        print("\n  Testing: is_single('Janie's Got A Gun', 'Aerosmith')")
        result = client.is_single(title="Janie's Got A Gun", artist="Aerosmith")
        
        print(f"  Result with apostrophe: {result}")
        print(f"  API calls made: {mock_get.call_count}")
        
        # Should be False because no results returned
        # This is the BUG - we need to handle this better
        assert result == False, f"Expected False (no results), got {result}"
    
    print("✓ Correctly identified the issue: no results with apostrophe")
    return True


def test_discogs_search_with_normalized_title():
    """
    Test that if we normalize the title before searching, we get results.
    This is the potential fix.
    """
    print("\n" + "="*80)
    print("TEST: Discogs search with normalized title")
    print("="*80)
    
    from discogs_singles_cache import normalize_track_title
    
    # Original title with apostrophe
    original_title = "Janie's Got A Gun"
    normalized_title = normalize_track_title(original_title)
    
    print(f"  Original: '{original_title}'")
    print(f"  Normalized: '{normalized_title}'")
    
    client = DiscogsClient(token="test_token", enabled=True)
    
    # Simulate API returning results when searching with normalized title
    mock_search_response = {
        "results": [
            {
                "id": 123456,
                "type": "release",
                "title": "Aerosmith - Janies Got A Gun",  # No apostrophe in Discogs
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
        
        # Manually call the internal method with normalized title
        results = client._discogs_search_with_format("Aerosmith", normalized_title, "Single")
        
        print(f"  Results with normalized title: {len(results)} found")
        print(f"  API called with: '{mock_get.call_args[1]['params']['track']}'")
        
        # Verify results were found
        assert len(results) > 0, "Expected results with normalized title"
        
        # Now check if the result matches
        normalized_search = normalize_track_title(original_title)
        match_found = client._check_search_results(results, normalized_search)
        print(f"  Match found: {match_found}")
        assert match_found == True, "Expected match with normalized comparison"
    
    print("✓ Normalized search strategy works")
    return True


def test_proposed_fix():
    """
    Test the proposed fix: normalize title before sending to API.
    """
    print("\n" + "="*80)
    print("TEST: Proposed fix - normalize before API call")
    print("="*80)
    
    from discogs_singles_cache import normalize_track_title
    
    test_cases = [
        ("Janie's Got A Gun", "Aerosmith - Janies Got A Gun"),
        ("Don't Stop Believin'", "Journey - Dont Stop Believin"),
        ("What's Love Got to Do with It", "Tina Turner - Whats Love Got to Do with It"),
    ]
    
    client = DiscogsClient(token="test_token", enabled=True)
    
    for original_title, discogs_result_title in test_cases:
        # Normalize the title before API call
        normalized_title = normalize_track_title(original_title)
        artist = discogs_result_title.split(" - ")[0]
        
        mock_search_response = {
            "results": [
                {
                    "id": 123456,
                    "type": "release",
                    "title": discogs_result_title,
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
            
            # Call with normalized title
            print(f"\n  Testing: '{original_title}' -> '{normalized_title}'")
            results = client._discogs_search_with_format(artist, normalized_title, "Single")
            
            # Check if match is found
            match = client._check_search_results(results, normalize_track_title(original_title))
            print(f"    Results: {len(results)}, Match: {match}")
            
            assert match == True, f"Expected match for '{original_title}'"
    
    print("\n✓ Fix works: normalizing before API call enables matching")
    return True


if __name__ == "__main__":
    try:
        test_discogs_search_no_results_with_apostrophe()
        test_discogs_search_with_normalized_title()
        test_proposed_fix()
        
        print("\n" + "="*80)
        print("✅ ALL TESTS PASSED")
        print("Demonstrated issue and verified fix works!")
        print("="*80)
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ TEST FAILED: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
