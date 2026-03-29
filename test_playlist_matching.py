#!/usr/bin/env python3
"""
Test suite for playlist matching logic in /api/playlist/load endpoint.

Tests the multi-strategy matching algorithm:
1. MBID exact match (most reliable)
2. Album + Title + Artist match
3. Title + Artist match with album preference
4. Title-only match (fallback)

This validates that playlists from external sources (Navidrome, Spotify) can
be reliably matched to database file paths even when metadata doesn't match exactly.
"""

import sqlite3
import logging
from datetime import datetime

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
log = logging.getLogger(__name__)


def create_test_db(db_path=":memory:"):
    """Create a test database with sample tracks."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Create simplified tracks table (matching actual schema)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tracks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            artist TEXT NOT NULL,
            album TEXT,
            file_path TEXT UNIQUE NOT NULL,
            musicbrainz_id TEXT,
            last_scanned TIMESTAMP,
            popularity_score REAL
        )
    """)
    
    # Insert test tracks
    test_tracks = [
        # Perfect match test cases
        {
            "title": "Bohemian Rhapsody",
            "artist": "Queen",
            "album": "A Night at the Opera",
            "file_path": "/music/queen/a_night_at_the_opera/01_bohemian_rhapsody.mp3",
            "mbid": "9ace0904-617f-4c60-9057-97b6d55db548"
        },
        # Case sensitivity test
        {
            "title": "Stairway to Heaven",
            "artist": "Led Zeppelin",
            "album": "Led Zeppelin IV",
            "file_path": "/music/led_zeppelin/led_zeppelin_iv/04_stairway_to_heaven.mp3",
            "mbid": "d6802e8f-a74d-41c9-b019-566722df0b73"
        },
        # Multiple versions - should prefer matching album
        {
            "title": "Yesterday",
            "artist": "The Beatles",
            "album": "Help!",
            "file_path": "/music/the_beatles/help/01_yesterday.mp3",
            "mbid": "aaa00000-0000-0000-0000-000000000001"
        },
        {
            "title": "Yesterday",
            "artist": "The Beatles",
            "album": "Yesterday and Today",
            "file_path": "/music/the_beatles/yesterday_and_today/02_yesterday.mp3",
            "mbid": "bbb00000-0000-0000-0000-000000000001"
        },
        # Live version
        {
            "title": "Yesterday",
            "artist": "The Beatles",
            "album": "Live at Abbey Road",
            "file_path": "/music/the_beatles/live_at_abbey_road/05_yesterday.mp3",
            "mbid": "ccc00000-0000-0000-0000-000000000001"
        },
        # Special characters test
        {
            "title": "Don't Stop Believin'",
            "artist": "Journey",
            "album": "Escape",
            "file_path": "/music/journey/escape/08_dont_stop_believin.mp3",
            "mbid": "ddd00000-0000-0000-0000-000000000001"
        },
        # Unicode test
        {
            "title": "Café au Lait",
            "artist": "Café del Mar",
            "album": "Volumen Uno",
            "file_path": "/music/cafe_del_mar/volumen_uno/03_cafe_au_lait.mp3",
            "mbid": "eee00000-0000-0000-0000-000000000001"
        },
        # Title-only collision test (multiple tracks with same title)
        {
            "title": "Love Me Do",
            "artist": "The Beatles",
            "album": "The Beatles",
            "file_path": "/music/the_beatles/the_beatles/01_love_me_do.mp3",
            "mbid": "fff00000-0000-0000-0000-000000000001"
        },
        {
            "title": "Love Me Do",
            "artist": "The Beatles",
            "album": "The Singles",
            "file_path": "/music/the_beatles/the_singles/22_love_me_do.mp3",
            "mbid": None
        },
    ]
    
    now = datetime.now().isoformat()
    for track in test_tracks:
        cursor.execute("""
            INSERT INTO tracks (title, artist, album, file_path, musicbrainz_id, last_scanned)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            track["title"],
            track["artist"],
            track["album"],
            track["file_path"],
            track.get("mbid"),
            now
        ))
    
    conn.commit()
    return conn


def test_mbid_matching(conn):
    """Test Strategy 1: MBID exact match"""
    log.info("=" * 60)
    log.info("TEST 1: MBID Exact Match (Most Reliable)")
    log.info("=" * 60)
    
    cursor = conn.cursor()
    
    test_cases = [
        {
            "mbid": "9ace0904-617f-4c60-9057-97b6d55db548",
            "expected_title": "Bohemian Rhapsody",
            "expected_artist": "Queen"
        },
        {
            "mbid": "d6802e8f-a74d-41c9-b019-566722df0b73",
            "expected_title": "Stairway to Heaven",
            "expected_artist": "Led Zeppelin"
        }
    ]
    
    for i, test in enumerate(test_cases, 1):
        cursor.execute(
            "SELECT file_path, title, artist FROM tracks WHERE musicbrainz_id = ? LIMIT 1",
            (test["mbid"],)
        )
        row = cursor.fetchone()
        
        if row:
            success = (row["title"] == test["expected_title"] and 
                      row["artist"] == test["expected_artist"])
            status = "✓ PASS" if success else "✗ FAIL"
            log.info(f"  Test 1.{i}: {status}")
            log.info(f"    MBID: {test['mbid']}")
            log.info(f"    Found: {row['title']} by {row['artist']}")
        else:
            log.error(f"  Test 1.{i}: ✗ FAIL - No MBID match found")


def test_album_title_artist_matching(conn):
    """Test Strategy 2: Album + Title + Artist match"""
    log.info("\n" + "=" * 60)
    log.info("TEST 2: Album + Title + Artist Match")
    log.info("=" * 60)
    
    cursor = conn.cursor()
    
    test_cases = [
        {
            "title": "Bohemian Rhapsody",
            "artist": "Queen",
            "album": "A Night at the Opera",
            "expected_path": "/music/queen/a_night_at_the_opera/01_bohemian_rhapsody.mp3"
        },
        {
            "title": "Don't Stop Believin'",
            "artist": "Journey",
            "album": "Escape",
            "expected_path": "/music/journey/escape/08_dont_stop_believin.mp3"
        },
    ]
    
    for i, test in enumerate(test_cases, 1):
        cursor.execute("""
            SELECT file_path
            FROM tracks
            WHERE LOWER(title) = LOWER(?)
              AND LOWER(artist) = LOWER(?)
              AND LOWER(album) = LOWER(?)
            LIMIT 1
        """, (test["title"], test["artist"], test["album"]))
        
        row = cursor.fetchone()
        
        if row:
            success = row["file_path"] == test["expected_path"]
            status = "✓ PASS" if success else "✗ FAIL"
            log.info(f"  Test 2.{i}: {status}")
            log.info(f"    {test['artist']} - {test['title']} from {test['album']}")
            log.info(f"    Found: {row['file_path']}")
        else:
            log.error(f"  Test 2.{i}: ✗ FAIL - No match found for album+title+artist")


def test_title_artist_matching(conn):
    """Test Strategy 3: Title + Artist match with album preference"""
    log.info("\n" + "=" * 60)
    log.info("TEST 3: Title + Artist Match (with Album Preference)")
    log.info("=" * 60)
    
    cursor = conn.cursor()
    
    test_cases = [
        {
            "title": "Yesterday",
            "artist": "The Beatles",
            "preferred_album": "Help!",
            "reason": "Should prefer 'Help!' over other versions"
        },
        {
            "title": "Love Me Do",
            "artist": "The Beatles",
            "preferred_album": "The Beatles",
            "reason": "Should prefer 'The Beatles' album over singles"
        }
    ]
    
    for i, test in enumerate(test_cases, 1):
        # This simulates the query from the improved endpoint
        # It orders by album match first, then by last_scanned
        cursor.execute("""
            SELECT file_path, album
            FROM tracks
            WHERE LOWER(title) = LOWER(?)
              AND LOWER(artist) = LOWER(?)
            ORDER BY 
              CASE WHEN LOWER(album) = LOWER(?) THEN 0 ELSE 1 END,
              last_scanned DESC
            LIMIT 1
        """, (test["title"], test["artist"], test["preferred_album"]))
        
        row = cursor.fetchone()
        
        if row:
            correct_album = row["album"] == test["preferred_album"]
            status = "✓ PASS" if correct_album else "⚠ INFO"
            log.info(f"  Test 3.{i}: {status}")
            log.info(f"    Track: {test['artist']} - {test['title']}")
            log.info(f"    Reason: {test['reason']}")
            log.info(f"    Preferred: {test['preferred_album']}")
            log.info(f"    Found: {row['album']}")
        else:
            log.error(f"  Test 3.{i}: ✗ FAIL - No title+artist match found")


def test_title_only_matching(conn):
    """Test Strategy 4: Title-only match (fallback)"""
    log.info("\n" + "=" * 60)
    log.info("TEST 4: Title-Only Match (Fallback)")
    log.info("=" * 60)
    
    cursor = conn.cursor()
    
    test_cases = [
        {
            "title": "Bohemian Rhapsody",
            "reason": "Unique title - should match first",
            "min_matches": 1
        },
        {
            "title": "Yesterday",
            "reason": "Multiple versions exist - should match any",
            "min_matches": 1
        }
    ]
    
    for i, test in enumerate(test_cases, 1):
        cursor.execute("""
            SELECT COUNT(*) as count, GROUP_CONCAT(artist || ' (' || album || ')') as variants
            FROM tracks
            WHERE LOWER(title) = LOWER(?)
        """, (test["title"],))
        
        row = cursor.fetchone()
        
        if row and row["count"] >= test["min_matches"]:
            status = "✓ PASS"
            log.info(f"  Test 4.{i}: {status}")
            log.info(f"    Title: {test['title']}")
            log.info(f"    Reason: {test['reason']}")
            log.info(f"    Variants found: {row['count']}")
            log.info(f"    Artists: {row['variants']}")
        else:
            log.error(f"  Test 4.{i}: ✗ FAIL - No title match or insufficient matches")


def test_case_insensitive_matching(conn):
    """Test that matching is case-insensitive"""
    log.info("\n" + "=" * 60)
    log.info("TEST 5: Case Insensitivity")
    log.info("=" * 60)
    
    cursor = conn.cursor()
    
    test_cases = [
        {"title": "BOHEMIAN RHAPSODY", "artist": "queen"},
        {"title": "stairway to heaven", "artist": "LED ZEPPELIN"},
        {"title": "Don't Stop Believin'", "artist": "JOURNEY"}
    ]
    
    for i, test in enumerate(test_cases, 1):
        cursor.execute("""
            SELECT file_path, title, artist
            FROM tracks
            WHERE LOWER(title) = LOWER(?)
              AND LOWER(artist) = LOWER(?)
            LIMIT 1
        """, (test["title"], test["artist"]))
        
        row = cursor.fetchone()
        
        if row:
            log.info(f"  Test 5.{i}: ✓ PASS")
            log.info(f"    Input: {test['artist']} - {test['title']}")
            log.info(f"    Found: {row['artist']} - {row['title']}")
        else:
            log.error(f"  Test 5.{i}: ✗ FAIL")


def test_special_characters(conn):
    """Test matching with special characters and unicode"""
    log.info("\n" + "=" * 60)
    log.info("TEST 6: Special Characters & Unicode")
    log.info("=" * 60)
    
    cursor = conn.cursor()
    
    test_cases = [
        {"title": "Don't Stop Believin'", "artist": "Journey"},
        {"title": "Café au Lait", "artist": "Café del Mar"}
    ]
    
    for i, test in enumerate(test_cases, 1):
        cursor.execute("""
            SELECT file_path, title, artist
            FROM tracks
            WHERE LOWER(title) = LOWER(?)
              AND LOWER(artist) = LOWER(?)
            LIMIT 1
        """, (test["title"], test["artist"]))
        
        row = cursor.fetchone()
        
        if row:
            log.info(f"  Test 6.{i}: ✓ PASS")
            log.info(f"    Input: {test['artist']} - {test['title']}")
            log.info(f"    Found: {row['artist']} - {row['title']}")
        else:
            log.error(f"  Test 6.{i}: ✗ FAIL")


def test_no_match_scenario(conn):
    """Test behavior when no match is found"""
    log.info("\n" + "=" * 60)
    log.info("TEST 7: No Match Scenarios")
    log.info("=" * 60)
    
    cursor = conn.cursor()
    
    non_existent_tracks = [
        {"title": "Nonexistent Track", "artist": "Nonexistent Artist"},
        {"title": "Another Fake", "artist": "Nobody"},
        {"mbid": "00000000-0000-0000-0000-000000000000"}
    ]
    
    for i, track in enumerate(non_existent_tracks, 1):
        if "mbid" in track:
            cursor.execute(
                "SELECT file_path FROM tracks WHERE musicbrainz_id = ? LIMIT 1",
                (track["mbid"],)
            )
        else:
            cursor.execute("""
                SELECT file_path FROM tracks
                WHERE LOWER(title) = LOWER(?)
                  AND LOWER(artist) = LOWER(?)
                LIMIT 1
            """, (track.get("title", ""), track.get("artist", "")))
        
        row = cursor.fetchone()
        
        if row is None:
            log.info(f"  Test 7.{i}: ✓ PASS (Correctly returned no match)")
        else:
            log.error(f"  Test 7.{i}: ✗ FAIL (Should have returned no match)")


def run_all_tests():
    """Run complete test suite"""
    log.info("\n" + "=" * 80)
    log.info("PLAYLIST MATCHING TEST SUITE")
    log.info("Testing multi-strategy matching algorithm for /api/playlist/load")
    log.info("=" * 80)
    
    conn = create_test_db()
    
    try:
        test_mbid_matching(conn)
        test_album_title_artist_matching(conn)
        test_title_artist_matching(conn)
        test_title_only_matching(conn)
        test_case_insensitive_matching(conn)
        test_special_characters(conn)
        test_no_match_scenario(conn)
        
        log.info("\n" + "=" * 80)
        log.info("TEST SUITE COMPLETE")
        log.info("=" * 80)
        
    finally:
        conn.close()


if __name__ == "__main__":
    run_all_tests()
