#!/usr/bin/env python3
"""
Test script to verify that track_number column is properly selected and accessible.
This tests the fix for the "No item with that key" error.
"""

import os
import sys
import sqlite3
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

# Set minimal required environment variables
os.environ.setdefault("CONFIG_PATH", "/tmp/test_config.yaml")
os.environ.setdefault("DB_PATH", ":memory:")
os.environ.setdefault("LOG_PATH", "/tmp/test_sptnr.log")
os.environ.setdefault("UNIFIED_SCAN_LOG_PATH", "/tmp/test_unified_scan.log")

def test_track_number_access():
    """Test that track_number is accessible from fetched track rows"""
    
    print("\n" + "="*60)
    print("TRACK NUMBER ACCESS TEST")
    print("="*60)
    
    # Import the function
    from popularity import detect_alternate_takes
    
    # Create a mock database with tracks
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Create tracks table with required columns
    cursor.execute("""
        CREATE TABLE tracks (
            id TEXT PRIMARY KEY,
            artist TEXT,
            title TEXT,
            album TEXT,
            isrc TEXT,
            duration REAL,
            spotify_album_type TEXT,
            track_number INTEGER
        )
    """)
    
    # Insert test tracks
    test_tracks = [
        ("track1", "Test Artist", "Song One", "Test Album", None, 180.0, "album", 1),
        ("track2", "Test Artist", "Song Two (Live)", "Test Album", None, 185.0, "album", 2),
        ("track3", "Test Artist", "Song Two", "Test Album", None, 180.0, "album", 3),
    ]
    
    cursor.executemany(
        "INSERT INTO tracks (id, artist, title, album, isrc, duration, spotify_album_type, track_number) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        test_tracks
    )
    conn.commit()
    
    # Fetch tracks using the same query pattern as popularity.py
    cursor.execute("""
        SELECT id, artist, title, album, isrc, duration, spotify_album_type, track_number
        FROM tracks
        ORDER BY artist, album, title
    """)
    
    tracks = cursor.fetchall()
    
    print(f"\nFetched {len(tracks)} tracks from database")
    
    # Test 1: Verify track_number is accessible
    print("\nTest 1: Verify track_number column is accessible")
    try:
        for i, track in enumerate(tracks, 1):
            track_id = track["id"]
            title = track["title"]
            track_number = track["track_number"]
            print(f"  Track {i}: id={track_id}, title={title}, track_number={track_number}")
        print("  ✅ PASS: All track_number values accessible")
    except KeyError as e:
        print(f"  ❌ FAIL: KeyError when accessing track field: {e}")
        conn.close()
        return False
    except Exception as e:
        print(f"  ❌ FAIL: Unexpected error: {e}")
        conn.close()
        return False
    
    # Test 2: Call detect_alternate_takes with the fetched tracks
    print("\nTest 2: Call detect_alternate_takes with fetched tracks")
    try:
        alternate_takes = detect_alternate_takes(list(tracks))
        print(f"  Detected {len(alternate_takes)} alternate take(s)")
        for alt_id, base_id in alternate_takes.items():
            print(f"    {alt_id} -> {base_id}")
        print("  ✅ PASS: detect_alternate_takes executed successfully")
    except KeyError as e:
        print(f"  ❌ FAIL: KeyError in detect_alternate_takes: {e}")
        conn.close()
        return False
    except Exception as e:
        print(f"  ❌ FAIL: Unexpected error in detect_alternate_takes: {e}")
        import traceback
        traceback.print_exc()
        conn.close()
        return False
    
    conn.close()
    
    print("\n" + "="*60)
    print("ALL TESTS PASSED")
    print("="*60)
    return True

if __name__ == "__main__":
    # Mock the start module if needed
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    
    # Run test
    success = test_track_number_access()
    
    # Exit with appropriate code
    sys.exit(0 if success else 1)
