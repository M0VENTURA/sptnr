#!/usr/bin/env python3
"""
Test that Discogs single detection prioritizes Single/EP releases over albums.

This test verifies the fix for the issue where Discogs `is_single()` was missing
known singles because it was searching without a format filter first, causing
album results to appear before single results.
"""

import unittest
from unittest.mock import Mock, patch, MagicMock
from api_clients.discogs import DiscogsClient


class TestDiscogsSearchPriority(unittest.TestCase):
    """Test that Discogs searches prioritize singles/EPs."""
    
    def test_search_uses_format_filter_first(self):
        """Test that format filter is applied in the first search."""
        # Create a mock session
        mock_session = Mock()
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"results": []}
        mock_session.get.return_value = mock_response
        
        # Create client with mock session
        client = DiscogsClient(token="test_token", http_session=mock_session, enabled=True)
        
        # Call is_single
        result = client.is_single("Viva la Vida", "Coldplay")
        
        # Verify the first call includes format filter
        first_call_args = mock_session.get.call_args_list[0]
        params = first_call_args[1]['params']
        
        self.assertIn('format', params, "First search should include format filter")
        self.assertEqual(params['format'], "Single, EP", "Format should be 'Single, EP'")
    
    def test_fallback_to_unfiltered_search(self):
        """Test that unfiltered search is used as fallback if filtered search returns nothing."""
        # Create a mock session that returns nothing on first call, results on second
        mock_session = Mock()
        
        # First call (with filter): no results
        first_response = Mock()
        first_response.status_code = 200
        first_response.json.return_value = {"results": []}
        
        # Second call (without filter): has results but no singles
        second_response = Mock()
        second_response.status_code = 200
        second_response.json.return_value = {
            "results": [{
                "id": 123,
                "title": "Viva la Vida",
                "format": ["Album"]
            }]
        }
        
        # Third call (fetching release details)
        third_response = Mock()
        third_response.status_code = 200
        third_response.json.return_value = {
            "formats": [{"name": "Album", "descriptions": []}],
            "tracklist": []
        }
        
        mock_session.get.side_effect = [first_response, second_response, third_response]
        
        # Create client with mock session
        client = DiscogsClient(token="test_token", http_session=mock_session, enabled=True)
        
        # Call is_single
        result = client.is_single("Viva la Vida", "Coldplay")
        
        # Verify two searches were made
        self.assertEqual(mock_session.get.call_count, 3, "Should make 2 search calls + 1 release fetch")
        
        # Verify first search had format filter
        first_call_params = mock_session.get.call_args_list[0][1]['params']
        self.assertIn('format', first_call_params)
        
        # Verify second search does NOT have format filter
        second_call_params = mock_session.get.call_args_list[1][1]['params']
        self.assertNotIn('format', second_call_params, "Fallback search should not have format filter")


class TestDiscogsSingleMatching(unittest.TestCase):
    """Test Discogs single matching with real-world scenarios."""
    
    def test_single_found_in_first_search(self):
        """Test that a single is found when format filter returns it."""
        mock_session = Mock()
        
        # Search returns a single
        search_response = Mock()
        search_response.status_code = 200
        search_response.json.return_value = {
            "results": [{
                "id": 456,
                "title": "Viva la Vida"
            }]
        }
        
        # Release details show it's a single
        release_response = Mock()
        release_response.status_code = 200
        release_response.json.return_value = {
            "formats": [{"name": "Vinyl", "descriptions": ["Single", "7\""]}],
            "tracklist": [
                {"position": "A", "title": "Viva la Vida"},
                {"position": "B", "title": "Life in Technicolor"}
            ]
        }
        
        mock_session.get.side_effect = [search_response, release_response]
        
        # Create client with mock session
        client = DiscogsClient(token="test_token", http_session=mock_session, enabled=True)
        
        # Call is_single - should find the single
        result = client.is_single("Viva la Vida", "Coldplay")
        
        # Verify it was found
        self.assertTrue(result, "Should detect 'Viva la Vida' as a single")


if __name__ == '__main__':
    unittest.main()
