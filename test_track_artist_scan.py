#!/usr/bin/env python3
"""
Test script to verify artist scanning works for track artists (from Various Artists albums).
This tests the fix for PR #202 where track artists couldn't be scanned because they don't have a Navidrome artist_id.
"""

import os
import sys
import sqlite3
import tempfile
import logging
from unittest.mock import Mock, patch, MagicMock

# Set up test environment
test_db_path = tempfile.mktemp(suffix=".db")
os.environ["DB_PATH"] = test_db_path
os.environ["CONFIG_PATH"] = "/tmp/test_config.yaml"
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["LOG_PATH"] = "/tmp/test_logs"
os.environ["APP_DIR"] = "/tmp/test_app"
# Create log directory
os.makedirs("/tmp/test_logs", exist_ok=True)

# Configure logging to capture messages
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)

print(f"Using test database: {test_db_path}")

# Import modules to test
from check_db import update_schema
from db_utils import get_db_connection


def setup_test_db():
    """Create test database with schema."""
    print("\n1. Setting up test database schema...")
    update_schema(test_db_path)
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Add artist_stats for "Various Artists" (album artist)
    cursor.execute("""
        INSERT INTO artist_stats (artist_id, artist_name, album_count, track_count)
        VALUES ('navidrome-id-123', 'Various Artists', 1, 3)
    """)
    
    # Add tracks from a Various Artists album with different track artists
    # Track 1: At the Drive-In
    cursor.execute("""
        INSERT INTO tracks (id, artist, album_artist, album, title, score)
        VALUES ('track-1', 'At the Drive-In', 'Various Artists', 'Compilation Album', 'One Armed Scissor', 0)
    """)
    
    # Track 2: The Beatles
    cursor.execute("""
        INSERT INTO tracks (id, artist, album_artist, album, title, score)
        VALUES ('track-2', 'The Beatles', 'Various Artists', 'Compilation Album', 'Hey Jude', 0)
    """)
    
    # Track 3: Queen
    cursor.execute("""
        INSERT INTO tracks (id, artist, album_artist, album, title, score)
        VALUES ('track-3', 'Queen', 'Various Artists', 'Compilation Album', 'Bohemian Rhapsody', 0)
    """)
    
    # Add a normal album artist with tracks
    cursor.execute("""
        INSERT INTO artist_stats (artist_id, artist_name, album_count, track_count)
        VALUES ('navidrome-id-456', 'Radiohead', 1, 2)
    """)
    
    cursor.execute("""
        INSERT INTO tracks (id, artist, album_artist, album, title, score)
        VALUES ('track-4', 'Radiohead', 'Radiohead', 'OK Computer', 'Paranoid Android', 0)
    """)
    
    cursor.execute("""
        INSERT INTO tracks (id, artist, album_artist, album, title, score)
        VALUES ('track-5', 'Radiohead', 'Radiohead', 'OK Computer', 'Karma Police', 0)
    """)
    
    conn.commit()
    conn.close()
    print("✓ Test database created with sample tracks")
    print("  - Various Artists album with tracks by: At the Drive-In, The Beatles, Queen")
    print("  - Normal album by: Radiohead")


def test_track_artist_scan():
    """Test that track artists (not album artists) can be scanned."""
    print("\n2. Testing track artist scan (At the Drive-In)...")
    
    # Import the app module after DB is set up
    with patch('app.get_db') as mock_get_db, \
         patch('app.build_artist_index') as mock_build_index, \
         patch('app.popularity_scan') as mock_pop_scan, \
         patch('app.log_unified') as mock_log:
        
        # Mock database connection
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_get_db.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_conn.__enter__ = Mock(return_value=mock_conn)
        mock_conn.__exit__ = Mock(return_value=False)
        
        # First call: check artist_stats (returns None for "At the Drive-In")
        # Second call: check tracks table (returns 1 track found)
        mock_cursor.fetchone.side_effect = [None, (1,)]
        
        # Mock build_artist_index to return empty (artist not found)
        mock_build_index.return_value = {}
        
        # Import and run the function
        from app import _run_artist_scan_pipeline
        _run_artist_scan_pipeline("At the Drive-In")
        
        # Verify that popularity_scan was called (even without artist_id)
        assert mock_pop_scan.called, "popularity_scan should have been called for track artist"
        call_args = mock_pop_scan.call_args
        assert call_args[1]['artist_filter'] == 'At the Drive-In', "Should filter by track artist name"
        
        # Check logging messages
        log_calls = [str(call) for call in mock_log.call_args_list]
        log_messages = ' '.join(log_calls)
        
        assert "track artist" in log_messages.lower() or "Track artist" in log_messages, \
            "Should log that artist is a track artist"
        assert "Skipping Navidrome import" in log_messages, \
            "Should skip Navidrome import for track artists"
        
        print("✓ Track artist scan succeeded")
        print("✓ Popularity scan was called for 'At the Drive-In'")
        print("✓ Navidrome import was skipped (track artists already imported via album artist)")
        return True


def test_album_artist_scan():
    """Test that normal album artists still work correctly."""
    print("\n3. Testing normal album artist scan (Radiohead)...")
    
    with patch('app.get_db') as mock_get_db, \
         patch('app.build_artist_index') as mock_build_index, \
         patch('app.scan_artist_to_db') as mock_scan_artist, \
         patch('app.popularity_scan') as mock_pop_scan, \
         patch('app.log_unified') as mock_log:
        
        # Mock database connection
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_get_db.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_conn.__enter__ = Mock(return_value=mock_conn)
        mock_conn.__exit__ = Mock(return_value=False)
        
        # First call: check artist_stats (returns artist_id for Radiohead)
        mock_cursor.fetchone.return_value = ('navidrome-id-456',)
        
        # Import and run the function
        from app import _run_artist_scan_pipeline
        _run_artist_scan_pipeline("Radiohead")
        
        # Verify that both scan_artist_to_db and popularity_scan were called
        assert mock_scan_artist.called, "scan_artist_to_db should have been called for album artist"
        assert mock_pop_scan.called, "popularity_scan should have been called for album artist"
        
        # Verify correct parameters
        scan_args = mock_scan_artist.call_args
        assert scan_args[0][0] == 'Radiohead', "Should scan Radiohead"
        assert scan_args[0][1] == 'navidrome-id-456', "Should use Navidrome artist_id"
        
        print("✓ Normal album artist scan succeeded")
        print("✓ Both Navidrome import and popularity scan were called")
        return True


def test_nonexistent_artist():
    """Test that scanning a non-existent artist fails gracefully."""
    print("\n4. Testing non-existent artist scan...")
    
    with patch('app.get_db') as mock_get_db, \
         patch('app.build_artist_index') as mock_build_index, \
         patch('app.log_unified') as mock_log:
        
        # Mock database connection
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_get_db.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        mock_conn.__enter__ = Mock(return_value=mock_conn)
        mock_conn.__exit__ = Mock(return_value=False)
        
        # First call: check artist_stats (returns None)
        # Second call: check tracks table (returns 0 tracks)
        mock_cursor.fetchone.side_effect = [None, (0,)]
        
        # Mock build_artist_index to return empty
        mock_build_index.return_value = {}
        
        # Import and run the function
        from app import _run_artist_scan_pipeline
        _run_artist_scan_pipeline("Non-Existent Artist")
        
        # Check logging messages
        log_calls = [str(call) for call in mock_log.call_args_list]
        log_messages = ' '.join(log_calls)
        
        assert "Scan aborted" in log_messages or "no tracks found" in log_messages, \
            "Should abort scan for non-existent artist"
        
        print("✓ Non-existent artist scan aborted correctly")
        return True


def cleanup():
    """Remove test database."""
    print("\n5. Cleaning up...")
    if os.path.exists(test_db_path):
        os.remove(test_db_path)
        print("✓ Test database removed")


def main():
    """Run all tests."""
    print("=" * 70)
    print("Track Artist Scan Test Suite")
    print("=" * 70)
    
    try:
        setup_test_db()
        
        success = True
        success = test_track_artist_scan() and success
        success = test_album_artist_scan() and success
        success = test_nonexistent_artist() and success
        
        print("\n" + "=" * 70)
        if success:
            print("✅ All tests passed!")
            print("")
            print("Summary of fix:")
            print("- Track artists (from Various Artists albums) can now be scanned")
            print("- Artist scan checks tracks table when artist_id not found in artist_stats")
            print("- Navidrome import is skipped for track artists (already imported via album artist)")
            print("- Popularity scan still runs for track artists to update their scores")
            print("=" * 70)
            return 0
        else:
            print("❌ Some tests failed!")
            print("=" * 70)
            return 1
    except Exception as e:
        print(f"\n❌ Test suite error: {e}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        cleanup()


if __name__ == "__main__":
    sys.exit(main())
