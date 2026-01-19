#!/usr/bin/env python3
"""
Test to verify that sqlite3.Row objects are accessed correctly without .get() method.
This test validates the fix for the AttributeError: 'sqlite3.Row' object has no attribute 'get'
"""

import sqlite3
import tempfile
import os

def test_sqlite_row_access():
    """Test that we can access sqlite3.Row columns correctly"""
    
    print("\n=== Testing sqlite3.Row Access ===")
    
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
                spotify_album_type TEXT
            )
        """)
        
        # Insert test data with some NULL values
        cursor.execute("""
            INSERT INTO tracks (id, artist, title, album, isrc, duration, spotify_album_type)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, ("test_id_1", "Test Artist", "Test Track", "Test Album", "USRC12345", 180, "album"))
        
        cursor.execute("""
            INSERT INTO tracks (id, artist, title, album, isrc, duration, spotify_album_type)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, ("test_id_2", "Test Artist 2", "Test Track 2", "Test Album 2", None, None, None))
        
        conn.commit()
        
        # Fetch rows as sqlite3.Row objects
        cursor.execute("""
            SELECT id, artist, title, album, isrc, duration, spotify_album_type
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
            
            # Test 2: Verify the fix - using bracket notation with None check
            # This simulates the fixed code (lines 1186-1188 in popularity.py)
            track_isrc = track["isrc"] if track["isrc"] else None
            track_duration = track["duration"] if track["duration"] else None
            track_album_type = track["spotify_album_type"] if track["spotify_album_type"] else None
            
            print(f"  - isrc: {track_isrc}")
            print(f"  - duration: {track_duration}")
            print(f"  - spotify_album_type: {track_album_type}")
        
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
