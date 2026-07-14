#!/usr/bin/env python3
"""
Test that Discogs single detection uses optimized specific track search.

This test verifies that:
1. The optimized specific search is used (artist + track parameters)
2. Only 1 API call is made instead of 100+
3. Singles are correctly identified from search results
"""

import unittest
from unittest.mock import Mock, patch, MagicMock
from api_clients.discogs import DiscogsClient


class TestDiscogsOptimizedSearch(unittest.TestCase):
    """Test that Discogs uses optimized specific track search."""
    
    def test_specific_search_uses_track_parameter(self):
        """Test that specific search includes both artist and track parameters."""
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
        
        # Verify the call includes artist and track parameters
        first_call_args = mock_session.get.call_args_list[0]
        params = first_call_args[1]['params']
        
        self.assertIn('artist', params, "Search should include artist parameter")
        self.assertIn('track', params, "Search should include track parameter")
        self.assertEqual(params['artist'], "Coldplay", "Artist should be 'Coldplay'")
        self.assertEqual(params['track'], "Viva la Vida", "Track should be 'Viva la Vida'")
    
    def test_optimized_search_single_api_call(self):
        """Test that only one API call is made for the optimized search."""
        # Create a mock session
        mock_session = Mock()
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "results": [{
                "id": 123,
                "title": "Viva la Vida - Coldplay",
                "format": ["Vinyl", "7\"", "Single"]
            }]
        }
        mock_session.get.return_value = mock_response
        
        # Create client with mock session
        client = DiscogsClient(token="test_token", http_session=mock_session, enabled=True)
        
        # Call is_single
        result = client.is_single("Viva la Vida", "Coldplay")
        
        # Verify only one API call was made (the optimized specific search)
        self.assertEqual(mock_session.get.call_count, 1, "Should make only 1 API call (optimized)")
        self.assertTrue(result, "Should detect as single")


class TestDiscogsSingleMatching(unittest.TestCase):
    """Test Discogs single matching with real-world scenarios."""
    
    def test_single_found_via_specific_search(self):
        """Test that a single is found via the optimized specific search."""
        mock_session = Mock()
        
        # Search returns a single
        search_response = Mock()
        search_response.status_code = 200
        search_response.json.return_value = {
            "results": [{
                "id": 456,
                "title": "Viva la Vida - Coldplay",
                "format": ["Vinyl", "Single", "7\""]
            }]
        }
        
        mock_session.get.return_value = search_response
        
        # Create client with mock session
        client = DiscogsClient(token="test_token", http_session=mock_session, enabled=True)
        
        # Call is_single - should find the single with only 1 API call
        result = client.is_single("Viva la Vida", "Coldplay")
        
        # Verify it was found
        self.assertTrue(result, "Should detect 'Viva la Vida' as a single")
        self.assertEqual(mock_session.get.call_count, 1, "Should only make 1 API call")


if __name__ == '__main__':
    unittest.main()
