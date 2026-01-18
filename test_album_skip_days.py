#!/usr/bin/env python3
"""
Test script to verify album_skip_days time-based rescan functionality.

This test ensures that:
1. was_album_scanned() correctly checks time-based thresholds
2. Albums scanned within N days are skipped
3. Albums scanned more than N days ago are rescanned
4. The days_threshold parameter is optional (backward compatibility)
"""

import os
import sys
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta

# Create temporary test database BEFORE importing scan_history
tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
test_db_path = tmp.name
tmp.close()

# Set environment variable BEFORE importing scan_history (module caches DB_PATH)
os.environ['DB_PATH'] = test_db_path

# Add parent directory to path to import modules
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Now import scan_history - it will use our test database
from scan_history import log_album_scan, was_album_scanned


def cleanup_test_db():
    """Remove temporary test database"""
    global test_db_path
    if test_db_path and os.path.exists(test_db_path):
        try:
            os.unlink(test_db_path)
        except:
            pass
    # Also remove WAL files if they exist
    for ext in ['-wal', '-shm']:
        wal_file = test_db_path + ext
        if wal_file and os.path.exists(wal_file):
            try:
                os.unlink(wal_file)
            except:
                pass


def clear_test_db():
    """Clear all data from test database between tests"""
    global test_db_path
    try:
        conn = sqlite3.connect(test_db_path, timeout=120.0)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM scan_history")
        conn.commit()
        conn.close()
    except:
        pass


def insert_scan_with_custom_timestamp(artist, album, scan_type, days_ago):
    """Insert a scan record with a custom timestamp"""
    global test_db_path
    conn = sqlite3.connect(test_db_path, timeout=120.0)
    cursor = conn.cursor()
    
    # Create table if it doesn't exist
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS scan_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            artist TEXT NOT NULL,
            album TEXT NOT NULL,
            scan_type TEXT NOT NULL,
            scan_timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            tracks_processed INTEGER DEFAULT 0,
            status TEXT DEFAULT 'completed',
            source TEXT DEFAULT ''
        )
    """)
    
    # Calculate timestamp
    scan_timestamp = datetime.now() - timedelta(days=days_ago)
    
    cursor.execute("""
        INSERT INTO scan_history (artist, album, scan_type, scan_timestamp, tracks_processed, status)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (artist, album, scan_type, scan_timestamp.strftime('%Y-%m-%d %H:%M:%S'), 10, 'completed'))
    
    conn.commit()
    conn.close()


def test_legacy_behavior():
    """Test that was_album_scanned works without days_threshold (backward compatibility)"""
    print("Testing legacy behavior (no days_threshold)...")
    
    clear_test_db()  # Clear database before test
    
    try:
        artist = "Test Artist"
        album = "Test Album"
        scan_type = "popularity"
        
        # Album should not be scanned yet
        result = was_album_scanned(artist, album, scan_type)
        assert result == False, "Album should not be scanned initially"
        print("✅ Album correctly reported as not scanned")
        
        # Log a scan
        log_album_scan(artist, album, scan_type, tracks_processed=10)
        
        # Album should now be scanned (without days_threshold, it checks if ever scanned)
        result = was_album_scanned(artist, album, scan_type)
        assert result == True, "Album should be scanned after logging"
        print("✅ Album correctly reported as scanned (legacy behavior)")
        
        return True
    except Exception as e:
        print(f"❌ Legacy behavior test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_time_based_threshold():
    """Test that was_album_scanned respects days_threshold parameter"""
    print("\nTesting time-based threshold...")
    
    clear_test_db()  # Clear database before test
    
    try:
        artist = "Test Artist 2"
        album = "Test Album 2"
        scan_type = "popularity"
        
        # Insert a scan from 5 days ago
        insert_scan_with_custom_timestamp(artist, album, scan_type, days_ago=5)
        
        # With threshold of 7 days, album should be considered scanned
        result = was_album_scanned(artist, album, scan_type, days_threshold=7)
        assert result == True, "Album scanned 5 days ago should be within 7-day threshold"
        print("✅ Album scanned 5 days ago correctly skipped with 7-day threshold")
        
        # With threshold of 3 days, album should NOT be considered scanned
        result = was_album_scanned(artist, album, scan_type, days_threshold=3)
        assert result == False, "Album scanned 5 days ago should be outside 3-day threshold"
        print("✅ Album scanned 5 days ago correctly NOT skipped with 3-day threshold")
        
        return True
    except Exception as e:
        print(f"❌ Time-based threshold test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_old_scan_gets_rescanned():
    """Test that albums scanned more than N days ago get rescanned"""
    print("\nTesting old scan gets rescanned...")
    
    clear_test_db()  # Clear database before test
    
    try:
        artist = "Test Artist 3"
        album = "Test Album 3"
        scan_type = "popularity"
        
        # Insert a scan from 10 days ago
        insert_scan_with_custom_timestamp(artist, album, scan_type, days_ago=10)
        
        # With threshold of 7 days, album should NOT be considered scanned (needs rescan)
        result = was_album_scanned(artist, album, scan_type, days_threshold=7)
        assert result == False, "Album scanned 10 days ago should be outside 7-day threshold"
        print("✅ Album scanned 10 days ago correctly marked for rescan with 7-day threshold")
        
        return True
    except Exception as e:
        print(f"❌ Old scan rescan test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_recent_scan_gets_skipped():
    """Test that albums scanned recently get skipped"""
    print("\nTesting recent scan gets skipped...")
    
    clear_test_db()  # Clear database before test
    
    try:
        artist = "Test Artist 4"
        album = "Test Album 4"
        scan_type = "popularity"
        
        # Insert a scan from 1 day ago
        insert_scan_with_custom_timestamp(artist, album, scan_type, days_ago=1)
        
        # With threshold of 7 days, album should be considered scanned
        result = was_album_scanned(artist, album, scan_type, days_threshold=7)
        assert result == True, "Album scanned 1 day ago should be within 7-day threshold"
        print("✅ Album scanned 1 day ago correctly skipped with 7-day threshold")
        
        return True
    except Exception as e:
        print(f"❌ Recent scan skip test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_zero_days_threshold():
    """Test that zero days threshold means rescan every time"""
    print("\nTesting zero days threshold...")
    
    clear_test_db()  # Clear database before test
    
    try:
        artist = "Test Artist 5"
        album = "Test Album 5"
        scan_type = "popularity"
        
        # Insert a scan from today
        insert_scan_with_custom_timestamp(artist, album, scan_type, days_ago=0)
        
        # With threshold of 0 days, even today's scan should be rescanned
        result = was_album_scanned(artist, album, scan_type, days_threshold=0)
        assert result == False, "Album scanned today should be outside 0-day threshold"
        print("✅ Album scanned today correctly marked for rescan with 0-day threshold")
        
        return True
    except Exception as e:
        print(f"❌ Zero days threshold test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    print("=" * 60)
    print("Album Skip Days Time-Based Rescan Test Suite")
    print("=" * 60)
    
    tests_passed = 0
    tests_total = 5
    
    try:
        # Test 1: Legacy behavior
        if test_legacy_behavior():
            tests_passed += 1
        
        # Test 2: Time-based threshold
        if test_time_based_threshold():
            tests_passed += 1
        
        # Test 3: Old scan gets rescanned
        if test_old_scan_gets_rescanned():
            tests_passed += 1
        
        # Test 4: Recent scan gets skipped
        if test_recent_scan_gets_skipped():
            tests_passed += 1
        
        # Test 5: Zero days threshold
        if test_zero_days_threshold():
            tests_passed += 1
    finally:
        # Cleanup test database
        cleanup_test_db()
    
    print("\n" + "=" * 60)
    print(f"Tests passed: {tests_passed}/{tests_total}")
    print("=" * 60)
    
    sys.exit(0 if tests_passed == tests_total else 1)
