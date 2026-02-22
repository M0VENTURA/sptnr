#!/usr/bin/env python3
"""
Test to verify that sqlite3.Row objects are accessed correctly using row_get() helper.
This test validates the fix for the AttributeError: 'sqlite3.Row' object has no attribute 'get'
"""

import sqlite3
import tempfile
import os

def row_get(row, key, default=None):
    """
    Get a value from a sqlite3.Row object with a default fallback.
    
    sqlite3.Row objects don't have a .get() method like dictionaries,
    so this helper provides similar functionality.
    
    Args:
        row: sqlite3.Row object
        key: Column name to retrieve
        default: Default value if key doesn't exist or value is None
        
    Returns:
        Value from row or default
    """
    try:
        value = row[key]
        # Return default if value is None (NULL in database)
        return value if value is not None else default
    except (KeyError, IndexError):
        return default

def test_sqlite_row_access():
    """Test that we can access sqlite3.Row columns correctly using row_get()"""
    
    print("\n=== Testing sqlite3.Row Access with row_get() helper ===")
    
    # Create a temporary database
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.db') as f:
        db_path = f.name
    
    try:
        # Set up test database with same schema as actual database
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row  # This is what the actual code uses
        cursor = conn.cursor()
        
        # Create test table
        cursor.execute("""
            CREATE TABLE tracks (
                id TEXT PRIMARY KEY,
                artist TEXT,
                title TEXT,
                album TEXT,
                isrc TEXT,
                duration INTEGER,
                spotify_album_type TEXT,
                is_single INTEGER,
                single_sources TEXT,
                track_number INTEGER
            )
        """)
        
        # Insert test data with some NULL values
        cursor.execute("""
            INSERT INTO tracks (id, artist, title, album, isrc, duration, spotify_album_type, is_single, single_sources, track_number)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ("test_id_1", "Test Artist", "Test Track", "Test Album", "USRC12345", 180, "album", 1, '["spotify", "discogs"]', 1))
        
        cursor.execute("""
            INSERT INTO tracks (id, artist, title, album, isrc, duration, spotify_album_type, is_single, single_sources, track_number)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ("test_id_2", "Test Artist 2", "Test Track 2", "Test Album 2", None, None, None, 0, None, None))
        
        conn.commit()
        
        # Fetch rows as sqlite3.Row objects
        cursor.execute("""
            SELECT id, artist, title, album, isrc, duration, spotify_album_type, is_single, single_sources, track_number
            FROM tracks
            ORDER BY artist
        """)
        
        tracks = cursor.fetchall()
        
        print(f"✓ Fetched {len(tracks)} tracks from database")
        
        # Test 1: Verify we can access columns with bracket notation
        for track in tracks:
            track_id = track["id"]
            title = track["title"]
            print(f"✓ Can access track ID and title: {track_id}, {title}")
            
            # Test 2: Verify the fix - using row_get() helper function
            # This simulates the fixed code in popularity.py
            track_isrc = row_get(track, "isrc", None)
            track_duration = row_get(track, "duration", None)
            track_album_type = row_get(track, "spotify_album_type", None)
            is_single = row_get(track, "is_single", 0)
            single_sources_json = row_get(track, "single_sources", "[]")
            track_number = row_get(track, "track_number", 999)
            
            print(f"  - isrc: {track_isrc}")
            print(f"  - duration: {track_duration}")
            print(f"  - spotify_album_type: {track_album_type}")
            print(f"  - is_single: {is_single}")
            print(f"  - single_sources: {single_sources_json}")
            print(f"  - track_number: {track_number}")
        
        # Test 3: Verify that .get() does NOT work on sqlite3.Row
        print("\n=== Testing that .get() method doesn't exist ===")
        track = tracks[0]
        try:
            _ = track.get("isrc")
            print("✗ FAIL: .get() should not work on sqlite3.Row objects")
            return False
        except AttributeError as e:
            print(f"✓ Confirmed: sqlite3.Row doesn't have .get() method")
            print(f"  Error message: {e}")
        
        # Test 4: Verify row_get() handles missing keys
        print("\n=== Testing row_get() with missing keys ===")
        missing_value = row_get(track, "nonexistent_column", "default_value")
        if missing_value == "default_value":
            print("✓ row_get() correctly returns default for missing key")
        else:
            print("✗ FAIL: row_get() should return default for missing key")
            return False
        
        # Test 5: Verify row_get() handles NULL values
        print("\n=== Testing row_get() with NULL values ===")
        track_with_nulls = tracks[1]
        null_value = row_get(track_with_nulls, "isrc", "default_value")
        if null_value == "default_value":
            print("✓ row_get() correctly returns default for NULL value")
        else:
            print("✗ FAIL: row_get() should return default for NULL value")
            return False
        
        print("\n✓ All tests passed!")
        conn.close()
        return True
        
    finally:
        # Clean up
        if os.path.exists(db_path):
            os.unlink(db_path)

if __name__ == "__main__":
    success = test_sqlite_row_access()
    exit(0 if success else 1)
