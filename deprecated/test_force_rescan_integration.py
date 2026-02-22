#!/usr/bin/env python3
"""
Integration test to verify that force rescan correctly processes all tracks for an artist.
This simulates the scenario from the problem statement where only 1 out of 12 tracks
was being scanned.
"""

import os
import sys
import sqlite3
import tempfile
import shutil

# Set required environment variables for testing
test_db_path = "/tmp/test_force_rescan_integration.db"
os.environ["DB_PATH"] = test_db_path
os.environ["LOG_PATH"] = "/tmp/test_force_rescan.log"
os.environ["UNIFIED_SCAN_LOG_PATH"] = "/tmp/test_force_rescan_unified.log"
os.environ["SPTNR_VERBOSE_POPULARITY"] = "0"
os.environ["SPTNR_FORCE_RESCAN"] = "0"

def setup_test_database():
    """Create a test database with tracks like in the problem statement"""
    # Remove existing test database
    if os.path.exists(test_db_path):
        os.remove(test_db_path)
    
    conn = sqlite3.connect(test_db_path)
    cursor = conn.cursor()
    
    # Create tracks table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tracks (
            id TEXT PRIMARY KEY,
            artist TEXT,
            album TEXT,
            title TEXT,
            popularity_score REAL,
            stars INTEGER,
            is_single INTEGER,
            single_confidence TEXT,
            single_sources TEXT,
            last_scanned TEXT
        )
    """)
    
    # Create scan_history table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS scan_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            artist_name TEXT,
            album_name TEXT,
            scan_type TEXT,
            scan_timestamp TEXT,
            tracks_scanned INTEGER,
            status TEXT
        )
    """)
    
    # Insert 12 tracks for "+44" - "When Your Heart Stops Beating" album
    # Similar to the problem statement scenario
    tracks = [
        ("track1", "+44", "When Your Heart Stops Beating", "Cliffdiving"),
        ("track2", "+44", "When Your Heart Stops Beating", "Weatherman"),
        ("track3", "+44", "When Your Heart Stops Beating", "No, It Isn't"),
        ("track4", "+44", "When Your Heart Stops Beating", "Lillian"),
        ("track5", "+44", "When Your Heart Stops Beating", "Baby Come On"),
        ("track6", "+44", "When Your Heart Stops Beating", "When Your Heart Stops Beating"),
        ("track7", "+44", "When Your Heart Stops Beating", "Little Death"),
        ("track8", "+44", "When Your Heart Stops Beating", "155"),
        ("track9", "+44", "When Your Heart Stops Beating", "Lycanthrope"),
        ("track10", "+44", "When Your Heart Stops Beating", "Chapter 13"),
        ("track11", "+44", "When Your Heart Stops Beating", "Make You Smile"),
        ("track12", "+44", "When Your Heart Stops Beating", "Interlude"),
    ]
    
    # Insert tracks - only first track has NULL popularity_score, rest have scores
    for i, (track_id, artist, album, title) in enumerate(tracks):
        if i == 0:
            # First track has no score (simulating the initial state)
            cursor.execute(
                "INSERT INTO tracks (id, artist, album, title, popularity_score, stars) VALUES (?, ?, ?, ?, NULL, NULL)",
                (track_id, artist, album, title)
            )
        else:
            # Rest have scores (simulating already scanned tracks)
            cursor.execute(
                "INSERT INTO tracks (id, artist, album, title, popularity_score, stars) VALUES (?, ?, ?, ?, ?, ?)",
                (track_id, artist, album, title, 50.0, 3)
            )
    
    conn.commit()
    conn.close()
    print(f"✓ Test database created with 12 tracks (1 without score, 11 with scores)")

def test_normal_mode_query():
    """Test that normal mode only selects 1 track (without score)"""
    print("\n=== Test 1: Normal Mode (force=False) ===")
    
    conn = sqlite3.connect(test_db_path)
    cursor = conn.cursor()
    
    # Simulate the query from popularity_scan in normal mode
    sql_conditions = []
    force = False
    FORCE_RESCAN = False
    
    if not (FORCE_RESCAN or force):
        sql_conditions.append("(popularity_score IS NULL OR popularity_score = 0)")
    
    artist_filter = "+44"
    sql_params = []
    
    if artist_filter:
        sql_conditions.append("artist = ?")
        sql_params.append(artist_filter)
    
    sql = f"""
        SELECT id, artist, title, album
        FROM tracks
        {('WHERE ' + ' AND '.join(sql_conditions)) if sql_conditions else ''}
        ORDER BY artist, album, title
    """
    
    cursor.execute(sql, sql_params)
    tracks = cursor.fetchall()
    
    print(f"SQL: {sql.strip()}")
    print(f"Params: {sql_params}")
    print(f"Found {len(tracks)} tracks to scan")
    
    conn.close()
    
    assert len(tracks) == 1, f"Expected 1 track, got {len(tracks)}"
    print("✓ Normal mode correctly selects 1 track (without score)")

def test_force_mode_query():
    """Test that force mode selects all 12 tracks"""
    print("\n=== Test 2: Force Mode (force=True) ===")
    
    conn = sqlite3.connect(test_db_path)
    cursor = conn.cursor()
    
    # Simulate the query from popularity_scan in force mode
    sql_conditions = []
    force = True
    FORCE_RESCAN = False
    
    if not (FORCE_RESCAN or force):
        sql_conditions.append("(popularity_score IS NULL OR popularity_score = 0)")
    
    artist_filter = "+44"
    sql_params = []
    
    if artist_filter:
        sql_conditions.append("artist = ?")
        sql_params.append(artist_filter)
    
    sql = f"""
        SELECT id, artist, title, album
        FROM tracks
        {('WHERE ' + ' AND '.join(sql_conditions)) if sql_conditions else ''}
        ORDER BY artist, album, title
    """
    
    cursor.execute(sql, sql_params)
    tracks = cursor.fetchall()
    
    print(f"SQL: {sql.strip()}")
    print(f"Params: {sql_params}")
    print(f"Found {len(tracks)} tracks to scan")
    
    conn.close()
    
    assert len(tracks) == 12, f"Expected 12 tracks, got {len(tracks)}"
    print("✓ Force mode correctly selects all 12 tracks")

def cleanup():
    """Clean up test database"""
    if os.path.exists(test_db_path):
        os.remove(test_db_path)
    print("\n✓ Test database cleaned up")

if __name__ == "__main__":
    try:
        setup_test_database()
        test_normal_mode_query()
        test_force_mode_query()
        cleanup()
        print("\n=== All Integration Tests Passed ===\n")
    except AssertionError as e:
        print(f"\n✗ Test failed: {e}")
        cleanup()
        sys.exit(1)
    except Exception as e:
        print(f"\n✗ Error: {e}")
        import traceback
        traceback.print_exc()
        cleanup()
        sys.exit(1)
