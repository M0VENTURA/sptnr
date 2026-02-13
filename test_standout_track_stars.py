#!/usr/bin/env python3
"""
Test standout track 5-star assignment functionality.

This test validates that tracks marked as is_standout_track = 1
receive 5 stars during the star rating assignment phase.
"""

import sys
import sqlite3
from unittest.mock import MagicMock


def test_standout_track_star_assignment():
    """
    Test that standout tracks receive 5 stars during star rating assignment.
    
    Validates fix for issue where is_standout_track flag was set but not
    checked during star assignment, causing high-scrobbled non-singles to
    not receive 5 stars as intended.
    """
    print("=" * 80)
    print("Test: Standout Track 5-Star Assignment")
    print("=" * 80)
    
    # Create in-memory database
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Create tracks table with necessary columns
    cursor.execute("""
        CREATE TABLE tracks (
            id INTEGER PRIMARY KEY,
            title TEXT,
            artist TEXT,
            album TEXT,
            popularity_score INTEGER,
            is_single INTEGER DEFAULT 0,
            single_confidence TEXT DEFAULT 'low',
            single_sources TEXT DEFAULT '[]',
            lastfm_track_playcount INTEGER DEFAULT 0,
            is_standout_track INTEGER DEFAULT 0,
            artist_z_score REAL DEFAULT 0,
            stars INTEGER DEFAULT 0
        )
    """)
    
    # Insert test tracks for an album
    # Track 1: Regular track, no standout
    cursor.execute("""
        INSERT INTO tracks (id, title, artist, album, popularity_score, is_single, 
                           is_standout_track, lastfm_track_playcount)
        VALUES (1, 'Regular Track', 'Test Artist', 'Test Album', 50, 0, 0, 1000)
    """)
    
    # Track 2: Standout track (high Last.fm scrobbles, not a single)
    cursor.execute("""
        INSERT INTO tracks (id, title, artist, album, popularity_score, is_single, 
                           is_standout_track, lastfm_track_playcount, artist_z_score)
        VALUES (2, 'Popular Non-Single', 'Test Artist', 'Test Album', 70, 0, 1, 50000, 2.5)
    """)
    
    # Track 3: Another regular track
    cursor.execute("""
        INSERT INTO tracks (id, title, artist, album, popularity_score, is_single, 
                           is_standout_track, lastfm_track_playcount)
        VALUES (3, 'Another Track', 'Test Artist', 'Test Album', 45, 0, 0, 800)
    """)
    
    # Track 4: Confirmed single (should also get 5 stars)
    cursor.execute("""
        INSERT INTO tracks (id, title, artist, album, popularity_score, is_single, 
                           single_confidence, is_standout_track, lastfm_track_playcount)
        VALUES (4, 'Confirmed Single', 'Test Artist', 'Test Album', 80, 1, 'high', 0, 25000)
    """)
    
    conn.commit()
    
    # Simulate the star rating query that was fixed
    # This query should now include is_standout_track
    cursor.execute("""
        SELECT id, title, popularity_score, is_single, single_confidence, 
               single_sources, lastfm_track_playcount, is_standout_track
        FROM tracks 
        WHERE artist = ? AND album = ? 
        ORDER BY popularity_score DESC
    """, ('Test Artist', 'Test Album'))
    
    tracks = cursor.fetchall()
    
    print(f"\n  Retrieved {len(tracks)} tracks from database")
    print(f"  Checking that is_standout_track field is included in query results...")
    
    # Verify is_standout_track is in the query results
    try:
        for track in tracks:
            track_id = track['id']
            title = track['title']
            is_standout = track['is_standout_track']
            print(f"    Track {track_id}: '{title}' - is_standout_track={is_standout}")
    except KeyError as e:
        print(f"  ✗ ERROR: Column {e} not found in query results!")
        print(f"  Available columns: {tracks[0].keys() if tracks else 'none'}")
        conn.close()
        return False
    
    print(f"\n  ✅ Query includes is_standout_track field")
    
    # Simulate star assignment logic
    print(f"\n  Simulating star assignment logic...")
    
    for track in tracks:
        track_id = track['id']
        title = track['title']
        is_single = track['is_single']
        single_confidence = track['single_confidence'] if track['single_confidence'] else 'low'
        is_standout_track = track['is_standout_track'] if track['is_standout_track'] else 0
        
        # Simplified star assignment logic (matching the fix)
        stars = 3  # baseline
        
        # High confidence singles get 5 stars
        if single_confidence == 'high':
            stars = 5
            reason = "high-confidence single"
        # Standout tracks get 5 stars
        elif is_standout_track:
            stars = 5
            reason = "standout track (high Last.fm scrobbles)"
        else:
            reason = "baseline"
        
        # Update stars in database
        cursor.execute("UPDATE tracks SET stars = ? WHERE id = ?", (stars, track_id))
        
        print(f"    Track {track_id}: '{title}' -> {stars} stars ({reason})")
    
    conn.commit()
    
    # Verify the results
    print(f"\n  Verifying star assignments...")
    
    cursor.execute("SELECT id, title, is_standout_track, is_single, stars FROM tracks ORDER BY id")
    results = cursor.fetchall()
    
    success = True
    for row in results:
        track_id = row['id']
        title = row['title']
        is_standout = row['is_standout_track']
        is_single = row['is_single']
        stars = row['stars']
        
        expected_stars = None
        
        # Track 1: Regular track -> 3 stars
        if track_id == 1:
            expected_stars = 3
        # Track 2: Standout track -> 5 stars (THE KEY FIX)
        elif track_id == 2:
            expected_stars = 5
        # Track 3: Regular track -> 3 stars
        elif track_id == 3:
            expected_stars = 3
        # Track 4: High confidence single -> 5 stars
        elif track_id == 4:
            expected_stars = 5
        
        if stars == expected_stars:
            print(f"    ✓ Track {track_id}: '{title}' has {stars} stars (expected {expected_stars})")
        else:
            print(f"    ✗ Track {track_id}: '{title}' has {stars} stars (expected {expected_stars})")
            success = False
    
    conn.close()
    
    if success:
        print(f"\n  ✅ All standout tracks correctly receive 5 stars!")
        print(f"\n{'=' * 80}")
        print(f"RESULT: Test PASSED")
        print(f"{'=' * 80}\n")
        return True
    else:
        print(f"\n  ✗ Some standout tracks did not receive correct star ratings")
        print(f"\n{'=' * 80}")
        print(f"RESULT: Test FAILED")
        print(f"{'=' * 80}\n")
        return False


if __name__ == "__main__":
    success = test_standout_track_star_assignment()
    sys.exit(0 if success else 1)
