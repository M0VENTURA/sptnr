#!/usr/bin/env python3
"""
Integration test for Discogs metadata storage in database.

This test verifies that:
1. Discogs metadata can be fetched from the API (mocked)
2. Metadata can be stored in the database
3. Metadata can be retrieved from the database
4. All fields are correctly serialized/deserialized
"""

import sys
import json
import sqlite3
import tempfile
import os
from unittest.mock import Mock, patch

def test_discogs_metadata_integration():
    """Test end-to-end Discogs metadata storage."""
    print("\n" + "="*80)
    print("DISCOGS METADATA INTEGRATION TEST")
    print("="*80)
    
    # Create test database
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp:
        test_db_path = tmp.name
    
    try:
        # Step 1: Initialize database schema
        print("\n1. Initializing database schema...")
        import check_db
        check_db.update_schema(test_db_path)
        print("   ✓ Schema initialized")
        
        # Step 2: Test Discogs client with mocked API
        print("\n2. Testing Discogs metadata fetch (mocked)...")
        from api_clients.discogs import DiscogsClient
        
        # Mock API responses
        mock_search_response = {
            "results": [
                {
                    "id": 123456,
                    "title": "Test Song - Artist"
                }
            ]
        }
        
        mock_release_data = {
            "id": 123456,
            "title": "Test Song",
            "formats": [
                {
                    "name": "Vinyl",
                    "descriptions": ["7\"", "Single", "45 RPM"]
                }
            ],
            "tracklist": [
                {"position": "A", "title": "Test Song", "duration": "3:45"},
                {"position": "B", "title": "B-Side", "duration": "3:30"}
            ],
            "artists": [{"name": "Test Artist"}],
            "master_id": 789,
            "year": 2023,
            "labels": [{"name": "Test Records"}],
            "country": "US"
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
            
            # Fetch metadata
            metadata = client.get_comprehensive_metadata(
                title="Test Song",
                artist="Test Artist"
            )
            
            assert metadata is not None, "Failed to fetch metadata"
            assert metadata['discogs_release_id'] == '123456'
            assert metadata['discogs_master_id'] == '789'
            assert 'Vinyl' in metadata['discogs_formats']
            assert 'Single' in metadata['discogs_format_descriptions']
            assert metadata['discogs_is_single'] == True
            assert 'Test Song' in metadata['discogs_track_titles']
            assert metadata['discogs_release_year'] == 2023
            assert metadata['discogs_label'] == 'Test Records'
            assert metadata['discogs_country'] == 'US'
            
            print("   ✓ Metadata fetched successfully")
            print(f"   ✓ Release ID: {metadata['discogs_release_id']}")
            print(f"   ✓ Is single: {metadata['discogs_is_single']}")
        
        # Step 3: Store metadata in database
        print("\n3. Storing metadata in database...")
        
        conn = sqlite3.connect(test_db_path)
        cursor = conn.cursor()
        
        # Insert track with Discogs metadata
        cursor.execute("""
            INSERT INTO tracks (
                id, title, artist, album,
                discogs_release_id, discogs_master_id,
                discogs_formats, discogs_format_descriptions,
                discogs_is_single, discogs_track_titles,
                discogs_release_year, discogs_label, discogs_country
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            'track_001',
            'Test Song',
            'Test Artist',
            'Test Album',
            metadata['discogs_release_id'],
            metadata['discogs_master_id'],
            json.dumps(metadata['discogs_formats']),
            json.dumps(metadata['discogs_format_descriptions']),
            1 if metadata['discogs_is_single'] else 0,
            json.dumps(metadata['discogs_track_titles']),
            metadata['discogs_release_year'],
            metadata['discogs_label'],
            metadata['discogs_country']
        ))
        
        conn.commit()
        print("   ✓ Metadata stored in database")
        
        # Step 4: Retrieve and verify
        print("\n4. Retrieving and verifying metadata...")
        
        cursor.execute("""
            SELECT discogs_release_id, discogs_master_id, discogs_formats,
                   discogs_format_descriptions, discogs_is_single,
                   discogs_track_titles, discogs_release_year,
                   discogs_label, discogs_country
            FROM tracks WHERE id = ?
        """, ('track_001',))
        
        row = cursor.fetchone()
        assert row is not None, "Failed to retrieve track"
        
        # Verify all fields
        assert row[0] == '123456', "discogs_release_id mismatch"
        assert row[1] == '789', "discogs_master_id mismatch"
        
        formats = json.loads(row[2])
        assert 'Vinyl' in formats, "discogs_formats not deserialized correctly"
        
        descriptions = json.loads(row[3])
        assert 'Single' in descriptions, "discogs_format_descriptions not deserialized correctly"
        
        assert row[4] == 1, "discogs_is_single mismatch"
        
        track_titles = json.loads(row[5])
        assert 'Test Song' in track_titles, "discogs_track_titles not deserialized correctly"
        
        assert row[6] == 2023, "discogs_release_year mismatch"
        assert row[7] == 'Test Records', "discogs_label mismatch"
        assert row[8] == 'US', "discogs_country mismatch"
        
        print("   ✓ All fields verified successfully")
        print("   ✓ Formats:", formats)
        print("   ✓ Descriptions:", descriptions)
        print("   ✓ Track titles:", track_titles)
        
        conn.close()
        
        print("\n" + "="*80)
        print("✅ INTEGRATION TEST PASSED")
        print("Discogs metadata can be successfully stored and retrieved!")
        print("="*80)
        
        return True
        
    finally:
        # Clean up
        if os.path.exists(test_db_path):
            os.remove(test_db_path)


if __name__ == "__main__":
    try:
        result = test_discogs_metadata_integration()
        sys.exit(0 if result else 1)
    except Exception as e:
        print(f"\n❌ INTEGRATION TEST FAILED: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
