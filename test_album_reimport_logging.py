#!/usr/bin/env python3
"""
Test for album re-import duplicate logging fix.

This test ensures that when multiple tracks in an album have missing fields,
the log message about flagging the album for re-import is only printed once,
not once per track.
"""

import os
import sys
import sqlite3
import tempfile
import shutil
from unittest.mock import patch, MagicMock
import logging

# Set up test environment
os.environ['LOG_PATH'] = '/tmp/test_sptnr.log'
os.environ['UNIFIED_SCAN_LOG_PATH'] = '/tmp/test_unified_scan.log'
os.environ['DB_PATH'] = '/tmp/test_sptnr.db'

# Import the module under test
import navidrome_import
from db_utils import get_db_connection


def setup_test_db_with_missing_fields():
    """Create a test database with an album that has multiple tracks with missing fields."""
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Create tracks table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tracks (
            id TEXT PRIMARY KEY,
            title TEXT,
            album TEXT,
            artist TEXT,
            duration INTEGER,
            track_number INTEGER,
            year INTEGER,
            file_path TEXT
        )
    """)
    
    # Insert test data: 3 tracks from the same album, all with missing duration
    test_artist = "Test Artist"
    test_album = "Test Album"
    
    for i in range(1, 4):
        cursor.execute("""
            INSERT INTO tracks (id, title, album, artist, duration, track_number, year, file_path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            f"track_{i}",
            f"Track {i}",
            test_album,
            test_artist,
            None,  # Missing duration - should trigger re-import
            i,
            2023,
            f"/music/track_{i}.mp3"
        ))
    
    conn.commit()
    conn.close()
    
    return test_artist, test_album


def test_album_reimport_logging_once():
    """Test that album re-import message is logged only once despite multiple tracks with missing fields."""
    
    # Setup
    test_artist, test_album = setup_test_db_with_missing_fields()
    
    # Mock the external dependencies
    with patch('navidrome_import.fetch_artist_albums') as mock_fetch_albums, \
         patch('navidrome_import.fetch_album_tracks') as mock_fetch_tracks, \
         patch('navidrome_import.log_album_scan') as mock_log_scan, \
         patch('navidrome_import._fetch_artist_metadata') as mock_fetch_metadata, \
         patch('navidrome_import._scan_missing_musicbrainz_releases') as mock_scan_mb:
        
        # Mock returns
        mock_fetch_albums.return_value = []  # No albums to process from Navidrome
        
        # Capture log output
        log_messages = []
        original_info = logging.info
        
        def capture_info(msg, *args, **kwargs):
            log_messages.append(msg)
            return original_info(msg, *args, **kwargs)
        
        with patch('logging.info', side_effect=capture_info):
            # Run the scan with verbose=True to trigger the logging
            navidrome_import.scan_artist_to_db(
                artist_name=test_artist,
                artist_id="test_artist_id",
                verbose=True,
                force=False
            )
        
        # Check that the album was flagged for re-import only ONCE
        reimport_logs = [msg for msg in log_messages if "flagged for re-import due to missing fields" in msg]
        
        print(f"\n📊 Test Results:")
        print(f"   Total log messages: {len(log_messages)}")
        print(f"   Re-import flag messages: {len(reimport_logs)}")
        
        if reimport_logs:
            print(f"   Messages found:")
            for msg in reimport_logs:
                print(f"      - {msg}")
        
        # Assert: Should only have logged once for the album
        assert len(reimport_logs) == 1, f"Expected 1 re-import log message, but got {len(reimport_logs)}"
        assert test_album in reimport_logs[0], f"Expected album name '{test_album}' in log message"
        
        print(f"\n✅ Test PASSED: Album re-import message logged exactly once")
        return True


def test_album_reimport_logging_multiple_albums():
    """Test that different albums each get logged once."""
    
    # Setup database with 2 different albums, each with multiple tracks with missing fields
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Ensure tracks table exists
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tracks (
            id TEXT PRIMARY KEY,
            title TEXT,
            album TEXT,
            artist TEXT,
            duration INTEGER,
            track_number INTEGER,
            year INTEGER,
            file_path TEXT
        )
    """)
    
    test_artist = "Multi Album Artist"
    
    # Album 1: 3 tracks with missing duration
    for i in range(1, 4):
        cursor.execute("""
            INSERT INTO tracks (id, title, album, artist, duration, track_number, year, file_path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            f"album1_track_{i}",
            f"Track {i}",
            "Album One",
            test_artist,
            None,  # Missing
            i,
            2023,
            f"/music/album1_track_{i}.mp3"
        ))
    
    # Album 2: 2 tracks with missing duration
    for i in range(1, 3):
        cursor.execute("""
            INSERT INTO tracks (id, title, album, artist, duration, track_number, year, file_path)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            f"album2_track_{i}",
            f"Track {i}",
            "Album Two",
            test_artist,
            None,  # Missing
            i,
            2023,
            f"/music/album2_track_{i}.mp3"
        ))
    
    conn.commit()
    conn.close()
    
    # Mock dependencies
    with patch('navidrome_import.fetch_artist_albums') as mock_fetch_albums, \
         patch('navidrome_import.fetch_album_tracks') as mock_fetch_tracks, \
         patch('navidrome_import.log_album_scan') as mock_log_scan, \
         patch('navidrome_import._fetch_artist_metadata') as mock_fetch_metadata, \
         patch('navidrome_import._scan_missing_musicbrainz_releases') as mock_scan_mb:
        
        mock_fetch_albums.return_value = []
        
        # Capture logs
        log_messages = []
        original_info = logging.info
        
        def capture_info(msg, *args, **kwargs):
            log_messages.append(msg)
            return original_info(msg, *args, **kwargs)
        
        with patch('logging.info', side_effect=capture_info):
            navidrome_import.scan_artist_to_db(
                artist_name=test_artist,
                artist_id="test_artist_id",
                verbose=True,
                force=False
            )
        
        # Check results
        reimport_logs = [msg for msg in log_messages if "flagged for re-import due to missing fields" in msg]
        
        print(f"\n📊 Test Results (Multiple Albums):")
        print(f"   Re-import flag messages: {len(reimport_logs)}")
        
        if reimport_logs:
            print(f"   Messages found:")
            for msg in reimport_logs:
                print(f"      - {msg}")
        
        # Should have exactly 2 messages, one for each album
        assert len(reimport_logs) == 2, f"Expected 2 re-import log messages (one per album), but got {len(reimport_logs)}"
        
        # Check that both albums are mentioned
        album_names_in_logs = set()
        for msg in reimport_logs:
            if "Album One" in msg:
                album_names_in_logs.add("Album One")
            if "Album Two" in msg:
                album_names_in_logs.add("Album Two")
        
        assert len(album_names_in_logs) == 2, f"Expected both albums in logs, but only found: {album_names_in_logs}"
        
        print(f"\n✅ Test PASSED: Each album logged exactly once")
        return True


if __name__ == "__main__":
    # Clean up any existing test database
    test_db = '/tmp/test_sptnr.db'
    if os.path.exists(test_db):
        os.remove(test_db)
    
    try:
        print("=" * 60)
        print("Test 1: Single album with multiple tracks with missing fields")
        print("=" * 60)
        test_album_reimport_logging_once()
        
        # Clean database for next test
        if os.path.exists(test_db):
            os.remove(test_db)
        
        print("\n" + "=" * 60)
        print("Test 2: Multiple albums with missing fields")
        print("=" * 60)
        test_album_reimport_logging_multiple_albums()
        
        print("\n" + "=" * 60)
        print("🎉 All tests PASSED!")
        print("=" * 60)
        
    finally:
        # Clean up
        if os.path.exists(test_db):
            os.remove(test_db)
        for log_file in ['/tmp/test_sptnr.log', '/tmp/test_unified_scan.log']:
            if os.path.exists(log_file):
                os.remove(log_file)
