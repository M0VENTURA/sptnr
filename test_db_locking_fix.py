#!/usr/bin/env python3
"""
Test script to verify database locking fixes.

This test ensures that:
1. update_schema() uses timeout and WAL mode
2. Concurrent access doesn't cause database locked errors
"""

import os
import sys
import sqlite3
import tempfile
import threading
import time

# Add parent directory to path to import modules
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from check_db import update_schema


def test_update_schema_with_wal_mode():
    """Test that update_schema enables WAL mode"""
    print("Testing update_schema with WAL mode...")
    
    # Create a temporary database
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp:
        test_db_path = tmp.name
    
    try:
        # Run update_schema
        update_schema(test_db_path)
        
        # Verify WAL mode is enabled
        conn = sqlite3.connect(test_db_path, timeout=120.0)
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode")
        mode = cursor.fetchone()[0]
        conn.close()
        
        assert mode.lower() == 'wal', f"Expected WAL mode, got {mode}"
        print(f"✅ WAL mode is correctly set: {mode}")
        
        return True
    finally:
        # Cleanup
        if os.path.exists(test_db_path):
            os.unlink(test_db_path)
        # Also remove WAL files if they exist
        for ext in ['-wal', '-shm']:
            wal_file = test_db_path + ext
            if os.path.exists(wal_file):
                os.unlink(wal_file)


def test_concurrent_access():
    """Test that concurrent access doesn't cause locking errors"""
    print("\nTesting concurrent database access...")
    
    # Create a temporary database
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp:
        test_db_path = tmp.name
    
    try:
        # Initialize schema
        update_schema(test_db_path)
        
        errors = []
        
        def writer_thread():
            """Thread that writes to the database"""
            try:
                conn = sqlite3.connect(test_db_path, timeout=120.0)
                conn.execute("PRAGMA journal_mode=WAL")
                cursor = conn.cursor()
                
                for i in range(5):
                    cursor.execute("INSERT OR IGNORE INTO tracks (id, artist, album) VALUES (?, ?, ?)",
                                   (f"test_{i}", "Test Artist", "Test Album"))
                    conn.commit()
                    time.sleep(0.05)  # Small delay to allow concurrent access
                
                conn.close()
            except Exception as e:
                errors.append(f"Writer error: {e}")
        
        def reader_thread():
            """Thread that reads from the database"""
            try:
                conn = sqlite3.connect(test_db_path, timeout=120.0)
                conn.execute("PRAGMA journal_mode=WAL")
                cursor = conn.cursor()
                
                for i in range(5):
                    cursor.execute("SELECT COUNT(*) FROM tracks")
                    cursor.fetchone()
                    time.sleep(0.05)  # Small delay
                
                conn.close()
            except Exception as e:
                errors.append(f"Reader error: {e}")
        
        # Start multiple threads
        threads = []
        for _ in range(2):
            threads.append(threading.Thread(target=writer_thread))
            threads.append(threading.Thread(target=reader_thread))
        
        for t in threads:
            t.start()
        
        for t in threads:
            t.join()
        
        if errors:
            print(f"❌ Errors occurred during concurrent access:")
            for error in errors:
                print(f"   {error}")
            return False
        else:
            print("✅ No locking errors during concurrent access")
            return True
            
    finally:
        # Cleanup
        if os.path.exists(test_db_path):
            os.unlink(test_db_path)
        for ext in ['-wal', '-shm']:
            wal_file = test_db_path + ext
            if os.path.exists(wal_file):
                os.unlink(wal_file)


if __name__ == "__main__":
    print("=" * 60)
    print("Database Locking Fix Test Suite")
    print("=" * 60)
    
    tests_passed = 0
    tests_total = 2
    
    # Test 1: WAL mode
    if test_update_schema_with_wal_mode():
        tests_passed += 1
    
    # Test 2: Concurrent access
    if test_concurrent_access():
        tests_passed += 1
    
    print("\n" + "=" * 60)
    print(f"Tests passed: {tests_passed}/{tests_total}")
    print("=" * 60)
    
    sys.exit(0 if tests_passed == tests_total else 1)
