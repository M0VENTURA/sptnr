#!/usr/bin/env python3
"""
Test for Discogs single detection fix - video entries should not be treated as singles.

This test verifies that tracks with video entries but no single releases
are not incorrectly identified as singles:

1. Lost+ - Has a video entry, but not a single release
2. Life in Technicolor (instrumental) - Only "Life in Technicolor ii" was a single
3. Lovers in Japan (Osaka Sun Mix) - Album track, not a single release
"""

import sys
from unittest.mock import Mock, patch
from api_clients.discogs import DiscogsClient


def test_video_not_treated_as_single():
    """Test that videos alone don't make a track a single."""
    print("\n" + "="*80)
    print("DISCOGS VIDEO SINGLE FIX TEST")
    print("="*80)
    
    client = DiscogsClient(token="test_token", enabled=True)
    
    # Test case 1: Lost+ - has video but is not a single
    print("\n1. Testing 'Lost+' - video entry, not a single...")
    
    mock_search_response = {
        "results": [
            {"id": 12345, "title": "Lost+ - Coldplay"}
        ]
    }
    
    # Mock release with video but NOT marked as single (it's on an album)
    mock_release_with_video = {
        "id": 12345,
        "title": "Viva La Vida",  # Album
        "formats": [
            {
                "name": "CD",
                "descriptions": ["Album"]  # NOT a single
            }
        ],
        "tracklist": [
            {"position": "1", "title": "Life in Technicolor", "duration": "2:29"},
            {"position": "2", "title": "Cemeteries of London", "duration": "3:21"},
            {"position": "3", "title": "Lost+", "duration": "3:55"},
            {"position": "4", "title": "42", "duration": "3:57"}
        ],
        "videos": [
            {
                "title": "Coldplay - Lost+ (Official Video)",
                "description": "Official music video for Lost+"
            }
        ]
    }
    
    with patch.object(client.session, 'get') as mock_get:
        def side_effect(url, *args, **kwargs):
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.raise_for_status = Mock()
            
            if '/database/search' in url:
                mock_response.json.return_value = mock_search_response
            elif '/releases/' in url:
                mock_response.json.return_value = mock_release_with_video
            
            return mock_response
        
        mock_get.side_effect = side_effect
        
        result = client.is_single("Lost+", "Coldplay")
        
        # Lost+ should NOT be detected as single (it's a video on an album)
        assert result == False, "Lost+ should NOT be detected as single (has video but not a single release)"
        print("   ✓ Lost+ correctly NOT detected as single")
    
    
    # Test case 2: Life in Technicolor (instrumental) - no single release
    print("\n2. Testing 'Life in Technicolor' (instrumental) - no single release...")
    
    mock_search_response_2 = {
        "results": [
            {"id": 23456, "title": "Life in Technicolor - Coldplay"}
        ]
    }
    
    # Mock release - instrumental on album, not a single
    mock_album_release = {
        "id": 23456,
        "title": "Viva La Vida",  # Album
        "formats": [
            {
                "name": "CD",
                "descriptions": ["Album"]  # NOT a single
            }
        ],
        "tracklist": [
            {"position": "1", "title": "Life in Technicolor", "duration": "2:29"},  # Instrumental
            {"position": "2", "title": "Cemeteries of London", "duration": "3:21"}
        ]
    }
    
    with patch.object(client.session, 'get') as mock_get:
        def side_effect(url, *args, **kwargs):
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.raise_for_status = Mock()
            
            if '/database/search' in url:
                mock_response.json.return_value = mock_search_response_2
            elif '/releases/' in url:
                mock_response.json.return_value = mock_album_release
            
            return mock_response
        
        mock_get.side_effect = side_effect
        
        result = client.is_single("Life in Technicolor", "Coldplay")
        
        # Life in Technicolor (instrumental) should NOT be detected as single
        assert result == False, "Life in Technicolor (instrumental) should NOT be detected as single"
        print("   ✓ Life in Technicolor correctly NOT detected as single")
    
    
    # Test case 3: Lovers in Japan (Osaka Sun Mix) - album track, not single
    print("\n3. Testing 'Lovers in Japan (Osaka Sun Mix)' - album track, not single...")
    
    mock_search_response_3 = {
        "results": [
            {"id": 34567, "title": "Lovers in Japan - Coldplay"}
        ]
    }
    
    # Mock release - Osaka Sun Mix on album
    mock_album_release_3 = {
        "id": 34567,
        "title": "Viva La Vida",  # Album
        "formats": [
            {
                "name": "CD",
                "descriptions": ["Album"]  # NOT a single
            }
        ],
        "tracklist": [
            {"position": "1", "title": "Life in Technicolor", "duration": "2:29"},
            {"position": "2", "title": "Cemeteries of London", "duration": "3:21"},
            {"position": "3", "title": "Lost+", "duration": "3:55"},
            {"position": "4", "title": "42", "duration": "3:57"},
            {"position": "5", "title": "Lovers in Japan / Reign of Love", "duration": "6:51"},
            {"position": "6", "title": "Lovers in Japan (Osaka Sun Mix)", "duration": "3:57"}
        ]
    }
    
    with patch.object(client.session, 'get') as mock_get:
        def side_effect(url, *args, **kwargs):
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.raise_for_status = Mock()
            
            if '/database/search' in url:
                mock_response.json.return_value = mock_search_response_3
            elif '/releases/' in url:
                mock_response.json.return_value = mock_album_release_3
            
            return mock_response
        
        mock_get.side_effect = side_effect
        
        result = client.is_single("Lovers in Japan (Osaka Sun Mix)", "Coldplay")
        
        # Lovers in Japan (Osaka Sun Mix) should NOT be detected as single
        assert result == False, "Lovers in Japan (Osaka Sun Mix) should NOT be detected as single (album track)"
        print("   ✓ Lovers in Japan (Osaka Sun Mix) correctly NOT detected as single")
    
    
    # Test case 4: Verify that actual singles still work
    print("\n4. Testing that actual singles are still detected...")
    
    mock_search_response_4 = {
        "results": [
            {"id": 45678, "title": "Viva La Vida - Coldplay"}
        ]
    }
    
    # Mock actual single release
    mock_single_release = {
        "id": 45678,
        "title": "Viva La Vida",
        "formats": [
            {
                "name": "CD",
                "descriptions": ["Single"]  # Actual single
            }
        ],
        "tracklist": [
            {"position": "1", "title": "Viva La Vida", "duration": "4:01"},
            {"position": "2", "title": "Life in Technicolor ii", "duration": "4:05"}
        ]
    }
    
    with patch.object(client.session, 'get') as mock_get:
        def side_effect(url, *args, **kwargs):
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.raise_for_status = Mock()
            
            if '/database/search' in url:
                mock_response.json.return_value = mock_search_response_4
            elif '/releases/' in url:
                mock_response.json.return_value = mock_single_release
            
            return mock_response
        
        mock_get.side_effect = side_effect
        
        result = client.is_single("Viva La Vida", "Coldplay")
        
        # Viva La Vida should be detected as single (marked as single in format)
        assert result == True, "Viva La Vida should be detected as single (has 'Single' format)"
        print("   ✓ Viva La Vida correctly detected as single")
    
    
    print("\n" + "="*80)
    print("✅ ALL TESTS PASSED")
    print("Video entries are no longer incorrectly treated as singles!")
    print("="*80)
    
    return True


if __name__ == "__main__":
    try:
        result = test_video_not_treated_as_single()
        sys.exit(0 if result else 1)
    except Exception as e:
        print(f"\n❌ TEST FAILED: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
