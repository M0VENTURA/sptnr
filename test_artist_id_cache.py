#!/usr/bin/env python3
"""
Test script to verify Spotify artist ID caching functionality.
"""
import os
import sqlite3
import sys
from unittest.mock import Mock, patch
from datetime import datetime

# Set up test database path
os.environ['DB_PATH'] = './test_sptnr.db'

# Import after setting environment
from popularity_helpers import get_spotify_artist_id


def setup_test_db():
    """Create a test database with artist_stats table"""
    conn = sqlite3.connect('./test_sptnr.db')
    cursor = conn.cursor()
    
    # Create artist_stats table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS artist_stats (
            artist_id TEXT PRIMARY KEY,
            artist_name TEXT,
            album_count INTEGER,
            track_count INTEGER,
            last_updated TEXT,
            spotify_artist_id TEXT,
            spotify_id_cached_at TEXT
        );
    """)
    
    # Clear any existing test data
    cursor.execute("DELETE FROM artist_stats WHERE artist_name = 'Test Artist'")
    conn.commit()
    conn.close()
    print("✅ Test database setup complete")


def test_cache_miss_then_hit():
    """Test that artist ID is cached after first lookup"""
    
    print("\n=== Test 1: Cache Miss Then Hit ===")
    
    # Mock the Spotify client to simulate API response
    mock_spotify_client = Mock()
    mock_spotify_client.get_artist_id = Mock(return_value="test_spotify_id_123")
    
    # Clear the cache
    conn = sqlite3.connect('./test_sptnr.db')
    cursor = conn.cursor()
    cursor.execute("DELETE FROM artist_stats WHERE artist_name = 'Test Artist'")
    conn.commit()
    conn.close()
    
    with patch('popularity_helpers._spotify_client', mock_spotify_client), \
         patch('popularity_helpers._spotify_enabled', True), \
         patch('popularity_helpers._clients_configured', True):
        
        # First call - should query API and cache
        print("First call (should hit API)...")
        artist_id = get_spotify_artist_id("Test Artist")
        print(f"  Result: {artist_id}")
        assert artist_id == "test_spotify_id_123", f"Expected 'test_spotify_id_123', got {artist_id}"
        assert mock_spotify_client.get_artist_id.call_count == 1, "Should call API once"
        
        # Verify it was cached
        conn = sqlite3.connect('./test_sptnr.db')
        cursor = conn.cursor()
        cursor.execute("SELECT spotify_artist_id FROM artist_stats WHERE artist_name = 'Test Artist'")
        row = cursor.fetchone()
        conn.close()
        assert row is not None, "Should have cached entry"
        assert row[0] == "test_spotify_id_123", f"Cached ID should be 'test_spotify_id_123', got {row[0]}"
        print("  ✅ ID cached successfully")
        
        # Second call - should use cache, not call API
        print("Second call (should use cache)...")
        artist_id = get_spotify_artist_id("Test Artist")
        print(f"  Result: {artist_id}")
        assert artist_id == "test_spotify_id_123", f"Expected 'test_spotify_id_123', got {artist_id}"
        assert mock_spotify_client.get_artist_id.call_count == 1, "Should NOT call API again (still 1 call)"
        print("  ✅ Used cached ID, no API call made")
        
    print("✅ Test 1 passed!")


def test_cache_persistence():
    """Test that cache persists across multiple lookups"""
    
    print("\n=== Test 2: Cache Persistence ===")
    
    # Pre-populate cache
    conn = sqlite3.connect('./test_sptnr.db')
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO artist_stats 
        (artist_name, spotify_artist_id, spotify_id_cached_at) 
        VALUES (?, ?, ?)
    """, ("Cached Artist", "cached_id_456", datetime.now().isoformat()))
    conn.commit()
    conn.close()
    print("Pre-populated cache with 'Cached Artist'")
    
    # Mock the Spotify client - it should NOT be called
    mock_spotify_client = Mock()
    mock_spotify_client.get_artist_id = Mock(return_value="should_not_be_called")
    
    with patch('popularity_helpers._spotify_client', mock_spotify_client), \
         patch('popularity_helpers._spotify_enabled', True), \
         patch('popularity_helpers._clients_configured', True):
        
        # Lookup should use cache
        print("Looking up cached artist...")
        artist_id = get_spotify_artist_id("Cached Artist")
        print(f"  Result: {artist_id}")
        assert artist_id == "cached_id_456", f"Expected 'cached_id_456', got {artist_id}"
        assert mock_spotify_client.get_artist_id.call_count == 0, "Should NOT call API (cache hit)"
        print("  ✅ Used cached ID successfully")
    
    print("✅ Test 2 passed!")


def test_api_fallback():
    """Test that API is called when cache is empty"""
    
    print("\n=== Test 3: API Fallback ===")
    
    # Clear cache
    conn = sqlite3.connect('./test_sptnr.db')
    cursor = conn.cursor()
    cursor.execute("DELETE FROM artist_stats WHERE artist_name = 'New Artist'")
    conn.commit()
    conn.close()
    
    # Mock the Spotify client
    mock_spotify_client = Mock()
    mock_spotify_client.get_artist_id = Mock(return_value="new_artist_id_789")
    
    with patch('popularity_helpers._spotify_client', mock_spotify_client), \
         patch('popularity_helpers._spotify_enabled', True), \
         patch('popularity_helpers._clients_configured', True):
        
        print("Looking up uncached artist...")
        artist_id = get_spotify_artist_id("New Artist")
        print(f"  Result: {artist_id}")
        assert artist_id == "new_artist_id_789", f"Expected 'new_artist_id_789', got {artist_id}"
        assert mock_spotify_client.get_artist_id.call_count == 1, "Should call API once"
        print("  ✅ API fallback worked")
        
        # Verify it was cached
        conn = sqlite3.connect('./test_sptnr.db')
        cursor = conn.cursor()
        cursor.execute("SELECT spotify_artist_id FROM artist_stats WHERE artist_name = 'New Artist'")
        row = cursor.fetchone()
        conn.close()
        assert row is not None and row[0] == "new_artist_id_789", "Should cache the new ID"
        print("  ✅ New ID was cached")
    
    print("✅ Test 3 passed!")


def cleanup():
    """Clean up test database"""
    try:
        os.remove('./test_sptnr.db')
        print("\n✅ Test database cleaned up")
    except:
        pass


if __name__ == "__main__":
    try:
        setup_test_db()
        test_cache_miss_then_hit()
        test_cache_persistence()
        test_api_fallback()
        
        print("\n" + "="*50)
        print("✅ ALL TESTS PASSED!")
        print("="*50)
        
        cleanup()
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ TEST FAILED: {e}")
        import traceback
        traceback.print_exc()
        cleanup()
        sys.exit(1)
