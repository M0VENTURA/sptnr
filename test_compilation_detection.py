#!/usr/bin/env python3
"""
Test Compilation Album Detection
=================================

Tests the new compilation detection logic in single_detection_enhanced.py
"""

import os
import sys
import sqlite3
import tempfile
import logging

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Configure logging
logging.basicConfig(level=logging.DEBUG, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

from single_detection_enhanced import (
    is_compilation_album,
    should_check_track,
    detect_single_enhanced
)


def test_compilation_detection():
    """Test compilation album detection"""
    print("\n" + "="*60)
    print("TEST: Compilation Album Detection")
    print("="*60)
    
    test_cases = [
        # (album_type, album_title, track_count, expected)
        ("compilation", "Regular Album", 10, True, "album_type=compilation"),
        ("album", "Greatest Hits", 10, True, "title contains 'Greatest Hits'"),
        ("album", "Best of ABBA", 8, True, "title contains 'Best Of'"),
        ("album", "The Very Best of Queen", 12, True, "title contains 'The Very Best'"),
        ("album", "Anthology", 10, True, "title contains 'Anthology'"),
        ("album", "Singles Collection", 10, True, "title contains 'Singles'"),
        ("album", "Ultimate Collection", 10, True, "title contains 'Collection'"),
        ("album", "Gold", 10, True, "title contains 'Gold'"),
        ("album", "Platinum Hits", 10, True, "title contains 'Platinum'"),
        ("album", "Regular Album", 15, True, ">12 tracks"),
        ("album", "Regular Album", 10, False, "normal album"),
        ("album", "Regular Album", 12, False, "exactly 12 tracks"),
        (None, "Greatest Hits", 8, True, "no album_type but title matches"),
    ]
    
    passed = 0
    failed = 0
    
    for album_type, album_title, track_count, expected, reason in test_cases:
        result = is_compilation_album(album_type, album_title, track_count)
        status = "✓" if result == expected else "✗"
        
        if result == expected:
            passed += 1
            print(f"{status} PASS: {reason}")
            print(f"   Album: '{album_title}' (type={album_type}, tracks={track_count})")
            print(f"   Expected: {expected}, Got: {result}")
        else:
            failed += 1
            print(f"{status} FAIL: {reason}")
            print(f"   Album: '{album_title}' (type={album_type}, tracks={track_count})")
            print(f"   Expected: {expected}, Got: {result}")
        print()
    
    print(f"Results: {passed} passed, {failed} failed")
    return failed == 0


def test_pre_filter_with_compilation():
    """Test pre-filter behavior with compilation albums"""
    print("\n" + "="*60)
    print("TEST: Pre-Filter with Compilation Override")
    print("="*60)
    
    # Test data
    album_popularities = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    album_mean = 55.0
    album_stddev = 30.0
    
    test_cases = [
        # (popularity, spotify_versions, is_compilation, expected, reason)
        (10, 0, False, False, "Low popularity, not compilation, should skip"),
        (10, 0, True, True, "Low popularity, IS compilation, should check"),
        (100, 0, False, True, "Top 3 popularity, should check"),
        (100, 0, True, True, "Top 3 popularity, compilation, should check"),
        (50, 5, False, True, "5+ Spotify versions, should check"),
        (50, 5, True, True, "5+ Spotify versions, compilation, should check"),
        (86, 0, False, True, "Above mean+stddev threshold, should check"),
        (20, 2, False, False, "Below threshold, <5 versions, not top 3, should skip"),
        (20, 2, True, True, "Below threshold, but IS compilation, should check"),
    ]
    
    passed = 0
    failed = 0
    
    for popularity, spotify_versions, is_compilation, expected, reason in test_cases:
        result = should_check_track(
            popularity=popularity,
            album_mean=album_mean,
            album_stddev=album_stddev,
            album_popularities=album_popularities,
            spotify_version_count=spotify_versions,
            is_compilation=is_compilation
        )
        status = "✓" if result == expected else "✗"
        
        if result == expected:
            passed += 1
            print(f"{status} PASS: {reason}")
        else:
            failed += 1
            print(f"{status} FAIL: {reason}")
        
        print(f"   Popularity: {popularity}, Versions: {spotify_versions}, Compilation: {is_compilation}")
        print(f"   Expected: {expected}, Got: {result}")
        print()
    
    print(f"Results: {passed} passed, {failed} failed")
    return failed == 0


def test_full_detection_with_compilation():
    """Test full single detection with compilation album"""
    print("\n" + "="*60)
    print("TEST: Full Detection with Compilation Album")
    print("="*60)
    
    # Create temporary database
    with tempfile.NamedTemporaryFile(mode='w', suffix='.db', delete=False) as f:
        db_path = f.name
    
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Create tracks table with necessary columns
        cursor.execute("""
            CREATE TABLE tracks (
                id TEXT PRIMARY KEY,
                title TEXT,
                artist TEXT,
                album TEXT,
                popularity_score REAL,
                single_status TEXT,
                single_confidence_score REAL,
                single_sources_used TEXT,
                z_score REAL,
                spotify_version_count INTEGER,
                discogs_release_ids TEXT,
                musicbrainz_release_group_ids TEXT,
                single_detection_last_updated TEXT,
                is_single INTEGER,
                single_confidence TEXT,
                single_sources TEXT
            )
        """)
        
        # Insert test data for a compilation album
        test_tracks = [
            ("track1", "Dancing Queen", "ABBA", "ABBA Gold", 100),
            ("track2", "Waterloo", "ABBA", "ABBA Gold", 95),
            ("track3", "Mamma Mia", "ABBA", "ABBA Gold", 90),
            ("track4", "Take A Chance On Me", "ABBA", "ABBA Gold", 85),
            ("track5", "The Winner Takes It All", "ABBA", "ABBA Gold", 80),
            ("track6", "Money Money Money", "ABBA", "ABBA Gold", 75),
            ("track7", "SOS", "ABBA", "ABBA Gold", 70),
            ("track8", "Knowing Me Knowing You", "ABBA", "ABBA Gold", 65),
            ("track9", "Fernando", "ABBA", "ABBA Gold", 60),
            ("track10", "Voulez-Vous", "ABBA", "ABBA Gold", 55),
            ("track11", "Gimme! Gimme! Gimme!", "ABBA", "ABBA Gold", 50),
            ("track12", "Super Trouper", "ABBA", "ABBA Gold", 45),
            ("track13", "I Have A Dream", "ABBA", "ABBA Gold", 40),
            ("track14", "The Name Of The Game", "ABBA", "ABBA Gold", 35),
        ]
        
        for track_id, title, artist, album, pop in test_tracks:
            cursor.execute("""
                INSERT INTO tracks (id, title, artist, album, popularity_score)
                VALUES (?, ?, ?, ?, ?)
            """, (track_id, title, artist, album, pop))
        
        conn.commit()
        
        # Test detection on each track
        print("Testing single detection on 'ABBA Gold' compilation album...")
        print()
        
        checked_count = 0
        for track_id, title, artist, album, pop in test_tracks:
            result = detect_single_enhanced(
                conn=conn,
                track_id=track_id,
                title=title,
                artist=artist,
                album=album,
                popularity=pop,
                spotify_results=None,
                discogs_client=None,
                musicbrainz_client=None,
                verbose=True,
                album_type="compilation"  # Mark as compilation
            )
            
            # Check if track was evaluated (not filtered out)
            if result['single_status'] != 'none' or result['z_score'] != 0.0:
                checked_count += 1
                print(f"✓ Track evaluated: {title}")
                print(f"  Status: {result['single_status']}, Z-score: {result['z_score']:.2f}")
            else:
                # Track passed pre-filter but got 'none' status
                print(f"• Track checked: {title}")
                print(f"  Status: {result['single_status']}")
            print()
        
        print(f"\nTotal tracks in compilation: {len(test_tracks)}")
        print(f"Tracks that were evaluated: {checked_count}")
        
        # For compilation albums, ALL tracks should be checked (pass pre-filter)
        # Even if they don't get high confidence, they should be evaluated
        success = checked_count == len(test_tracks)
        
        if success:
            print("✓ PASS: All tracks in compilation were evaluated")
        else:
            print(f"✗ FAIL: Expected all {len(test_tracks)} tracks to be evaluated, got {checked_count}")
        
        conn.close()
        os.unlink(db_path)
        
        return success
        
    except Exception as e:
        print(f"✗ FAIL: Exception during test: {e}")
        import traceback
        traceback.print_exc()
        if os.path.exists(db_path):
            os.unlink(db_path)
        return False


def run_all_tests():
    """Run all compilation detection tests"""
    print("\n" + "="*80)
    print("COMPILATION ALBUM DETECTION TEST SUITE")
    print("="*80)
    
    tests = [
        test_compilation_detection,
        test_pre_filter_with_compilation,
        test_full_detection_with_compilation
    ]
    
    results = []
    for test_func in tests:
        try:
            result = test_func()
            results.append((test_func.__name__, result))
        except Exception as e:
            print(f"\n✗ EXCEPTION in {test_func.__name__}: {e}")
            import traceback
            traceback.print_exc()
            results.append((test_func.__name__, False))
    
    print("\n" + "="*80)
    print("TEST SUMMARY")
    print("="*80)
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for test_name, result in results:
        status = "✓ PASS" if result else "✗ FAIL"
        print(f"{status}: {test_name}")
    
    print()
    print(f"Total: {passed}/{total} tests passed")
    print("="*80)
    
    return passed == total


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
