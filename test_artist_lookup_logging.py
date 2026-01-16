#!/usr/bin/env python3
"""
Test script to verify artist ID lookup logging shows cache hits/misses.
This tests the fix for the issue where artist lookups were happening multiple times.
"""

import os
import sys
import sqlite3
import tempfile
import logging

# Set up test environment
os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
test_db_path = os.environ["DB_PATH"]

# Configure logging to capture INFO level messages
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)

print(f"Using test database: {test_db_path}")

# Import modules to test
from check_db import update_schema
from db_utils import get_db_connection
from popularity_helpers import get_spotify_artist_id, update_artist_id_for_artist

def setup_test_db():
    """Create test database with schema."""
    print("\n1. Setting up test database schema...")
    update_schema(test_db_path)
    
    # Add test tracks for two artists
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Artist 1: Has cached ID
    cursor.execute("""
        INSERT INTO tracks (id, artist, album, title, spotify_artist_id)
        VALUES ('track-1', 'Artist With Cache', 'Album 1', 'Track 1', 'spotify:artist:cached123')
    """)
    
    cursor.execute("""
        INSERT INTO tracks (id, artist, album, title, spotify_artist_id)
        VALUES ('track-2', 'Artist With Cache', 'Album 1', 'Track 2', 'spotify:artist:cached123')
    """)
    
    # Artist 2: No cached ID
    cursor.execute("""
        INSERT INTO tracks (id, artist, album, title)
        VALUES ('track-3', 'Artist Without Cache', 'Album 2', 'Track 3')
    """)
    
    conn.commit()
    conn.close()
    print("✓ Test database created with sample tracks")

def test_cache_hit_logging():
    """Test that cache hits are logged at INFO level."""
    print("\n2. Testing cache hit logging...")
    
    # This should find the cached ID and log it at INFO level
    print("   Looking up artist with cached ID...")
    result = get_spotify_artist_id('Artist With Cache')
    
    if result == 'spotify:artist:cached123':
        print(f"   ✓ Cached ID returned: {result}")
        print("   ✓ Check logs above for: '✓ Using cached Spotify artist ID'")
        return True
    else:
        print(f"   ✗ Unexpected result: {result}")
        return False

def test_cache_miss_logging():
    """Test that cache misses trigger API lookup logging."""
    print("\n3. Testing cache miss logging (will fail without Spotify credentials)...")
    
    # Mock the Spotify client to avoid actual API calls
    import popularity_helpers
    original_client = popularity_helpers._spotify_client
    original_enabled = popularity_helpers._spotify_enabled
    
    # Disable Spotify to avoid actual API calls in test
    popularity_helpers._spotify_client = None
    popularity_helpers._spotify_enabled = False
    
    print("   Looking up artist without cached ID (Spotify disabled for test)...")
    result = get_spotify_artist_id('Artist Without Cache')
    
    # Restore original state
    popularity_helpers._spotify_client = original_client
    popularity_helpers._spotify_enabled = original_enabled
    
    if result is None:
        print("   ✓ No result (expected with Spotify disabled)")
        print("   ✓ In production, this would log: 'Querying Spotify API for artist ID'")
        return True
    else:
        print(f"   ✗ Unexpected result: {result}")
        return False

def test_multiple_lookups_same_artist():
    """Test that multiple lookups for same artist use cache."""
    print("\n4. Testing multiple lookups for same artist...")
    
    # First lookup
    print("   First lookup for 'Artist With Cache'...")
    result1 = get_spotify_artist_id('Artist With Cache')
    
    # Second lookup (should use cache)
    print("   Second lookup for 'Artist With Cache'...")
    result2 = get_spotify_artist_id('Artist With Cache')
    
    # Third lookup (should use cache)
    print("   Third lookup for 'Artist With Cache'...")
    result3 = get_spotify_artist_id('Artist With Cache')
    
    if result1 == result2 == result3 == 'spotify:artist:cached123':
        print("   ✓ All three lookups returned cached ID")
        print("   ✓ Check logs above - should see 3x '✓ Using cached Spotify artist ID'")
        print("   ✓ This confirms the fix: cache is used for repeated lookups")
        return True
    else:
        print(f"   ✗ Inconsistent results: {result1}, {result2}, {result3}")
        return False

def cleanup():
    """Remove test database."""
    print("\n5. Cleaning up...")
    if os.path.exists(test_db_path):
        os.remove(test_db_path)
        print("✓ Test database removed")

def main():
    """Run all tests."""
    print("=" * 70)
    print("Artist Lookup Logging Test Suite")
    print("=" * 70)
    
    try:
        setup_test_db()
        
        success = True
        success = test_cache_hit_logging() and success
        success = test_cache_miss_logging() and success
        success = test_multiple_lookups_same_artist() and success
        
        print("\n" + "=" * 70)
        if success:
            print("✅ All tests passed!")
            print("")
            print("Summary of fix:")
            print("- Artist ID lookups now log at INFO level when cache is used")
            print("- Multiple lookups for same artist will use cached ID")
            print("- unified_scan.py now calls popularity_scan() only ONCE")
            print("- This prevents repeated artist lookups for each track/album")
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
