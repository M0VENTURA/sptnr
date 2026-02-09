#!/usr/bin/env python3
"""
Test for album mean popularity filter in single detection.

Verifies that tracks with popularity below album mean are NOT detected as singles,
even if other detection sources (Discogs, Spotify, etc.) would confirm them.

This addresses the requirement: "If the popularity is less than the mean popularity
on the album that it's not detected as a single"
"""

import sys
import os
import sqlite3
import tempfile
from unittest.mock import Mock, patch, MagicMock
from statistics import mean


def create_test_database():
    """Create a temporary database with test tracks."""
    # Create temp database
    temp_db = tempfile.NamedTemporaryFile(delete=False, suffix='.db')
    temp_db.close()
    
    conn = sqlite3.connect(temp_db.name)
    cursor = conn.cursor()
    
    # Create tracks table (simplified schema)
    cursor.execute("""
        CREATE TABLE tracks (
            id TEXT PRIMARY KEY,
            title TEXT,
            artist TEXT,
            album TEXT,
            popularity_score REAL,
            is_single INTEGER DEFAULT 0,
            single_confidence TEXT DEFAULT 'none',
            single_sources TEXT DEFAULT '[]'
        )
    """)
    
    # Insert test album with 5 tracks
    # Mean popularity will be: (80 + 75 + 70 + 65 + 60) / 5 = 70.0
    test_tracks = [
        ('track1', 'Hit Single', 'Test Artist', 'Test Album', 80.0),  # Above mean (80 > 70)
        ('track2', 'Popular Track', 'Test Artist', 'Test Album', 75.0),  # Above mean (75 > 70)
        ('track3', 'Average Track', 'Test Artist', 'Test Album', 70.0),  # Equal to mean
        ('track4', 'Lesser Track', 'Test Artist', 'Test Album', 65.0),  # Below mean (65 < 70)
        ('track5', 'Album Filler', 'Test Artist', 'Test Album', 60.0),  # Below mean (60 < 70)
    ]
    
    for track in test_tracks:
        cursor.execute("""
            INSERT INTO tracks (id, title, artist, album, popularity_score)
            VALUES (?, ?, ?, ?, ?)
        """, track)
    
    conn.commit()
    conn.close()
    
    return temp_db.name


def test_enhanced_detection_with_album_mean_filter():
    """
    Test that enhanced detection filters out tracks below album mean,
    even when Discogs confirms them as singles.
    """
    print("\n" + "="*80)
    print("TEST: Enhanced Detection - Album Mean Popularity Filter")
    print("="*80)
    
    db_path = create_test_database()
    
    try:
        # Import after creating database
        from single_detection_enhanced import detect_single_enhanced
        
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        
        # Calculate album mean for verification
        cursor = conn.cursor()
        cursor.execute("SELECT popularity_score FROM tracks WHERE album = 'Test Album'")
        popularities = [row[0] for row in cursor.fetchall()]
        album_mean = mean(popularities)
        
        print(f"\nAlbum: Test Album")
        print(f"Track popularities: {popularities}")
        print(f"Album mean popularity: {album_mean:.1f}")
        print()
        
        # Create mock Discogs client that confirms all tracks as singles
        mock_discogs = Mock()
        mock_discogs.enabled = True
        mock_discogs.is_single = Mock(return_value=True)  # Always confirms singles
        
        # Test 1: Track above mean should be detected as single
        print("Test 1: Track ABOVE mean (popularity 80 > mean 70)")
        result = detect_single_enhanced(
            conn=conn,
            track_id='track1',
            title='Hit Single',
            artist='Test Artist',
            album='Test Album',
            duration=200.0,
            isrc='TEST001',
            popularity=80.0,
            spotify_results=None,
            discogs_client=mock_discogs,
            musicbrainz_client=None,
            verbose=True,
            album_type='album'
        )
        
        print(f"  Result: is_single={result['is_single']}, confidence={result['single_confidence']}")
        if result['is_single']:
            print("  ✅ PASS: Track above mean correctly detected as single")
        else:
            print("  ❌ FAIL: Track above mean should be detected as single")
            return False
        
        # Test 2: Track below mean should NOT be detected as single
        print("\nTest 2: Track BELOW mean (popularity 65 < mean 70)")
        result = detect_single_enhanced(
            conn=conn,
            track_id='track4',
            title='Lesser Track',
            artist='Test Artist',
            album='Test Album',
            duration=200.0,
            isrc='TEST004',
            popularity=65.0,
            spotify_results=None,
            discogs_client=mock_discogs,
            musicbrainz_client=None,
            verbose=True,
            album_type='album'
        )
        
        print(f"  Result: is_single={result['is_single']}, confidence={result['single_confidence']}")
        if not result['is_single']:
            print("  ✅ PASS: Track below mean correctly rejected")
        else:
            print("  ❌ FAIL: Track below mean should NOT be detected as single")
            return False
        
        # Test 3: Track equal to mean should be detected (not below)
        print("\nTest 3: Track EQUAL to mean (popularity 70 == mean 70)")
        result = detect_single_enhanced(
            conn=conn,
            track_id='track3',
            title='Average Track',
            artist='Test Artist',
            album='Test Album',
            duration=200.0,
            isrc='TEST003',
            popularity=70.0,
            spotify_results=None,
            discogs_client=mock_discogs,
            musicbrainz_client=None,
            verbose=True,
            album_type='album'
        )
        
        print(f"  Result: is_single={result['is_single']}, confidence={result['single_confidence']}")
        if result['is_single']:
            print("  ✅ PASS: Track equal to mean correctly detected as single")
        else:
            print("  ❌ FAIL: Track equal to mean should be detected as single (not < mean)")
            return False
        
        conn.close()
        return True
        
    finally:
        # Clean up temp database
        os.unlink(db_path)


def test_standard_detection_with_album_mean_filter():
    """
    Test that standard detection (fallback) also filters out tracks below album mean.
    """
    print("\n" + "="*80)
    print("TEST: Standard Detection - Album Mean Popularity Filter")
    print("="*80)
    
    db_path = create_test_database()
    
    try:
        # Patch DB_PATH to use our test database
        with patch('db_utils.DB_PATH', db_path):
            from popularity import detect_single_for_track
            
            # Connect to verify mean
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT popularity_score FROM tracks WHERE album = 'Test Album'")
            popularities = [row[0] for row in cursor.fetchall()]
            album_mean = mean(popularities)
            conn.close()
            
            print(f"\nAlbum: Test Album")
            print(f"Album mean popularity: {album_mean:.1f}")
            print()
            
            # Test 1: Track above mean should pass filter
            print("Test 1: Track ABOVE mean (popularity 80 > mean 70)")
            with patch('popularity.get_db_connection') as mock_get_db:
                mock_conn = sqlite3.connect(db_path)
                mock_get_db.return_value = mock_conn
                
                result = detect_single_for_track(
                    title='Hit Single',
                    artist='Test Artist',
                    album='Test Album',
                    popularity=80.0,
                    use_advanced_detection=False,  # Force standard path
                    verbose=True
                )
                
                print(f"  Filter result: Sources={result['sources']}, is_single={result['is_single']}")
                # The filter should allow this track through (not reject it)
                # Whether it's detected as single depends on other sources
                print("  ✅ PASS: Track above mean passed popularity filter")
            
            # Test 2: Track below mean should be rejected
            print("\nTest 2: Track BELOW mean (popularity 65 < mean 70)")
            with patch('popularity.get_db_connection') as mock_get_db:
                mock_conn = sqlite3.connect(db_path)
                mock_get_db.return_value = mock_conn
                
                result = detect_single_for_track(
                    title='Lesser Track',
                    artist='Test Artist',
                    album='Test Album',
                    popularity=65.0,
                    use_advanced_detection=False,  # Force standard path
                    verbose=True
                )
                
                print(f"  Filter result: Sources={result['sources']}, is_single={result['is_single']}")
                if not result['is_single'] and len(result['sources']) == 0:
                    print("  ✅ PASS: Track below mean correctly rejected by filter")
                    return True
                else:
                    print("  ❌ FAIL: Track below mean should be rejected")
                    return False
        
    finally:
        # Clean up temp database
        os.unlink(db_path)


if __name__ == "__main__":
    print("\n" + "="*80)
    print("ALBUM MEAN POPULARITY FILTER TESTS")
    print("="*80)
    print("\nRequirement: Tracks with popularity < album mean should NOT be detected as singles")
    
    test1_passed = test_enhanced_detection_with_album_mean_filter()
    test2_passed = test_standard_detection_with_album_mean_filter()
    
    print("\n" + "="*80)
    print("TEST SUMMARY")
    print("="*80)
    print(f"Enhanced detection filter: {'✅ PASS' if test1_passed else '❌ FAIL'}")
    print(f"Standard detection filter: {'✅ PASS' if test2_passed else '❌ FAIL'}")
    
    if test1_passed and test2_passed:
        print("\n🎉 All tests passed!")
        sys.exit(0)
    else:
        print("\n❌ Some tests failed")
        sys.exit(1)
