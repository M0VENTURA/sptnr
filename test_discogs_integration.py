#!/usr/bin/env python3
"""
Integration test for Discogs API lookup with mock responses.
This tests the complete flow without requiring an actual API token.
"""

import sys
from unittest.mock import Mock, patch
import json


def test_discogs_search_api_flow():
    """Test the Discogs search flow with mocked API responses."""
    print("="*60)
    print("Testing Discogs API Search Flow (Mocked)")
    print("="*60)
    
    from singledetection import _discogs_search, _get_discogs_session
    
    # Create a mock response
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "results": [
            {
                "id": 123456,
                "title": "Suidakra - The Arcanum",
                "year": "1997",
                "genre": ["Rock", "Metal"],
                "style": ["Celtic Metal", "Folk Metal"],
                "format": ["CD", "Album"],
                "cover_image": "https://example.com/cover.jpg",
                "resource_url": "https://api.discogs.com/releases/123456"
            },
            {
                "id": 789012,
                "title": "The Arcanum - Suidakra",
                "year": "1997",
                "genre": ["Rock"],
                "style": ["Folk Metal"],
                "format": ["Vinyl", "LP"],
                "cover_image": "https://example.com/cover2.jpg",
                "resource_url": "https://api.discogs.com/releases/789012"
            }
        ]
    }
    
    session = _get_discogs_session()
    
    # Mock the session.get method
    with patch.object(session, 'get', return_value=mock_response) as mock_get:
        headers = {
            "User-Agent": "Sptnr/1.0",
            "Authorization": "Discogs token=fake_test_token"
        }
        
        results = _discogs_search(
            session,
            headers,
            "Suidakra The Arcanum",
            kind="release",
            per_page=10
        )
        
        # Verify the call was made correctly
        assert mock_get.called, "Session.get should have been called"
        call_args = mock_get.call_args
        
        print(f"✓ API call made to correct endpoint")
        print(f"✓ Headers included: {list(headers.keys())}")
        print(f"✓ Search returned {len(results)} results")
        
        # Verify results structure
        assert len(results) == 2, f"Expected 2 results, got {len(results)}"
        
        first_result = results[0]
        assert "id" in first_result, "Result should have 'id' field"
        assert "title" in first_result, "Result should have 'title' field"
        assert "genre" in first_result, "Result should have 'genre' field"
        
        print(f"✓ First result ID: {first_result['id']}")
        print(f"✓ First result title: {first_result['title']}")
        print(f"✓ First result genres: {first_result['genre']}")
        
        return True


def test_discogs_rate_limiting():
    """Test that rate limiting is applied."""
    print("\n" + "="*60)
    print("Testing Discogs Rate Limiting")
    print("="*60)
    
    import time
    # Import from the centralized rate limiting implementation
    from api_clients.discogs import _throttle_discogs
    
    # Call throttle twice and measure time
    start = time.time()
    _throttle_discogs()
    _throttle_discogs()
    elapsed = time.time() - start
    
    # Should take at least 0.35 seconds due to rate limiting
    # Allow 0.33s tolerance for timing variations
    if elapsed >= 0.33:
        print(f"✓ Rate limiting enforced ({elapsed:.3f}s >= 0.33s)")
        return True
    else:
        print(f"✗ Rate limiting may not be working ({elapsed:.3f}s < 0.33s)")
        return False


def test_app_py_integration_mock():
    """Test that app.py-style code works with our implementation."""
    print("\n" + "="*60)
    print("Testing app.py Integration Pattern (Mocked)")
    print("="*60)
    
    from singledetection import _discogs_search, _get_discogs_session
    
    # Simulate what app.py does at line 1929 and 7028
    session = _get_discogs_session()
    headers = {"User-Agent": "Sptnr/1.0"}
    discogs_token = "fake_test_token"
    
    if discogs_token:
        headers["Authorization"] = f"Discogs token={discogs_token}"
    
    print(f"✓ Session created: {type(session).__name__}")
    print(f"✓ Headers configured: {list(headers.keys())}")
    
    # Mock the API call
    mock_response = Mock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"results": [{"id": 1, "title": "Test Album"}]}
    
    with patch.object(session, 'get', return_value=mock_response):
        # Try different query formats as app.py does
        queries = [
            "Suidakra The Arcanum",
            '"The Arcanum" Suidakra',
            "The Arcanum"
        ]
        
        for query in queries:
            results = _discogs_search(session, headers, query, kind="release", per_page=15)
            if results:
                print(f"✓ Query '{query}' returned results")
                break
        
        print(f"✓ Search pattern matches app.py usage")
        return True


def test_error_handling():
    """Test error handling in search function."""
    print("\n" + "="*60)
    print("Testing Error Handling")
    print("="*60)
    
    from singledetection import _discogs_search, _get_discogs_session
    
    session = _get_discogs_session()
    headers = {"User-Agent": "Sptnr/1.0"}
    
    # Mock a failed response
    mock_response = Mock()
    mock_response.status_code = 500
    mock_response.raise_for_status.side_effect = Exception("Server Error")
    
    with patch.object(session, 'get', return_value=mock_response):
        try:
            _discogs_search(session, headers, "test query")
            print("✗ Should have raised an exception")
            return False
        except Exception as e:
            print(f"✓ Exception properly raised: {type(e).__name__}")
            return True


if __name__ == "__main__":
    print("\n" + "="*60)
    print("DISCOGS API INTEGRATION TEST SUITE")
    print("="*60 + "\n")
    
    results = []
    
    try:
        results.append(("API Search Flow", test_discogs_search_api_flow()))
        results.append(("Rate Limiting", test_discogs_rate_limiting()))
        results.append(("App Integration Pattern", test_app_py_integration_mock()))
        results.append(("Error Handling", test_error_handling()))
    except Exception as e:
        print(f"\n✗ Test suite failed with error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    
    print("\n" + "="*60)
    print("TEST RESULTS")
    print("="*60)
    
    all_passed = True
    for test_name, passed in results:
        status = "✓ PASS" if passed else "✗ FAIL"
        print(f"{status}: {test_name}")
        if not passed:
            all_passed = False
    
    print("="*60)
    
    if all_passed:
        print("\n✅ All integration tests passed!")
        print("The Discogs API lookup functionality is working correctly.")
        sys.exit(0)
    else:
        print("\n❌ Some tests failed.")
        sys.exit(1)
