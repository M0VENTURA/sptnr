#!/usr/bin/env python3
"""
Test artist-level popularity and alternate take detection features.
"""

import sys
import sqlite3
from datetime import datetime, timedelta
from popularity import (
    strip_parentheses,
    detect_alternate_takes,
    should_skip_spotify_lookup,
    calculate_artist_popularity_stats,
    should_exclude_from_stats
)

def test_strip_parentheses():
    """Test removing parentheses from track titles."""
    print("=" * 80)
    print("Test 1: strip_parentheses()")
    print("=" * 80)
    
    test_cases = [
        ("Track (Live)", "Track"),
        ("Track (Single)", "Track"),
        ("Track (Live in Wacken 2022)", "Track"),
        ("Track", "Track"),
        ("Track (One) Two", "Track (One) Two"),  # Middle parentheses not removed
        ("Track (One) (Two)", "Track (One)"),  # Only last parentheses removed
    ]
    
    for input_str, expected in test_cases:
        result = strip_parentheses(input_str)
        status = "✓" if result == expected else "✗"
        print(f"  {status} strip_parentheses('{input_str}') = '{result}' (expected '{expected}')")
        if result != expected:
            print(f"    ERROR: Expected '{expected}' but got '{result}'")
            return False
    
    print("  ✅ All strip_parentheses tests passed!\n")
    return True


def test_detect_alternate_takes():
    """Test detecting alternate takes."""
    print("=" * 80)
    print("Test 2: detect_alternate_takes()")
    print("=" * 80)
    
    # Test case 1: Basic alternate take detection
    tracks = [
        {"id": "1", "title": "Track One", "track_number": 1},
        {"id": "2", "title": "Track Two", "track_number": 2},
        {"id": "3", "title": "Track One (Live)", "track_number": 10},
        {"id": "4", "title": "Track Two (Single)", "track_number": 11},
    ]
    
    result = detect_alternate_takes(tracks)
    expected = {"3": "1", "4": "2"}
    
    print(f"  Input: 4 tracks (2 base, 2 alternate)")
    print(f"  Result: {result}")
    print(f"  Expected: {expected}")
    
    if result == expected:
        print("  ✅ Basic alternate take detection passed!\n")
    else:
        print("  ✗ ERROR: Unexpected result")
        return False
    
    # Test case 2: No alternate takes
    tracks2 = [
        {"id": "1", "title": "Track One", "track_number": 1},
        {"id": "2", "title": "Track Two", "track_number": 2},
    ]
    
    result2 = detect_alternate_takes(tracks2)
    expected2 = {}
    
    print(f"  Test 2: No alternate takes")
    print(f"  Result: {result2}")
    
    if result2 == expected2:
        print("  ✅ No alternate takes test passed!\n")
    else:
        print("  ✗ ERROR: Should return empty dict")
        return False
    
    return True


def test_should_skip_spotify_lookup():
    """Test 24-hour Spotify cache logic."""
    print("=" * 80)
    print("Test 3: should_skip_spotify_lookup()")
    print("=" * 80)
    
    # Create in-memory database for testing
    conn = sqlite3.connect(":memory:")
    cursor = conn.cursor()
    
    # Create tracks table
    cursor.execute("""
        CREATE TABLE tracks (
            id TEXT PRIMARY KEY,
            title TEXT,
            last_spotify_lookup TEXT,
            popularity_score REAL
        )
    """)
    
    # Test case 1: Recent lookup (< 24 hours old) with valid score
    recent_time = datetime.now() - timedelta(hours=1)
    cursor.execute("""
        INSERT INTO tracks (id, title, last_spotify_lookup, popularity_score)
        VALUES (?, ?, ?, ?)
    """, ("track1", "Test Track", recent_time.isoformat(), 50.0))
    
    result1 = should_skip_spotify_lookup("track1", conn)
    print(f"  Recent lookup (1 hour ago, score=50): {result1}")
    if not result1:
        print("  ✗ ERROR: Should skip recent lookup")
        return False
    
    # Test case 2: Old lookup (> 24 hours old)
    old_time = datetime.now() - timedelta(hours=25)
    cursor.execute("""
        INSERT INTO tracks (id, title, last_spotify_lookup, popularity_score)
        VALUES (?, ?, ?, ?)
    """, ("track2", "Test Track 2", old_time.isoformat(), 50.0))
    
    result2 = should_skip_spotify_lookup("track2", conn)
    print(f"  Old lookup (25 hours ago, score=50): {result2}")
    if result2:
        print("  ✗ ERROR: Should not skip old lookup")
        return False
    
    # Test case 3: No cached data
    cursor.execute("""
        INSERT INTO tracks (id, title)
        VALUES (?, ?)
    """, ("track3", "Test Track 3"))
    
    result3 = should_skip_spotify_lookup("track3", conn)
    print(f"  No cached lookup: {result3}")
    if result3:
        print("  ✗ ERROR: Should not skip when no cache")
        return False
    
    # Test case 4: Recent lookup but no score
    cursor.execute("""
        INSERT INTO tracks (id, title, last_spotify_lookup, popularity_score)
        VALUES (?, ?, ?, ?)
    """, ("track4", "Test Track 4", recent_time.isoformat(), 0.0))
    
    result4 = should_skip_spotify_lookup("track4", conn)
    print(f"  Recent lookup (1 hour ago, score=0): {result4}")
    if result4:
        print("  ✗ ERROR: Should not skip when score is 0")
        return False
    
    conn.close()
    print("  ✅ All should_skip_spotify_lookup tests passed!\n")
    return True


def test_calculate_artist_popularity_stats():
    """Test artist-level popularity statistics calculation."""
    print("=" * 80)
    print("Test 4: calculate_artist_popularity_stats()")
    print("=" * 80)
    
    # Create in-memory database for testing
    conn = sqlite3.connect(":memory:")
    cursor = conn.cursor()
    
    # Create tracks table
    cursor.execute("""
        CREATE TABLE tracks (
            id TEXT PRIMARY KEY,
            artist TEXT,
            title TEXT,
            popularity_score REAL
        )
    """)
    
    # Add test data
    test_tracks = [
        ("1", "Artist A", "Track 1", 80.0),
        ("2", "Artist A", "Track 2", 75.0),
        ("3", "Artist A", "Track 3", 70.0),
        ("4", "Artist A", "Track 4", 65.0),
        ("5", "Artist A", "Track 5", 60.0),
    ]
    
    cursor.executemany("""
        INSERT INTO tracks (id, artist, title, popularity_score)
        VALUES (?, ?, ?, ?)
    """, test_tracks)
    
    stats = calculate_artist_popularity_stats("Artist A", conn)
    
    print(f"  Test tracks: 5 tracks with scores [80, 75, 70, 65, 60]")
    print(f"  avg_popularity: {stats['avg_popularity']:.1f} (expected 70.0)")
    print(f"  median_popularity: {stats['median_popularity']:.1f} (expected 70.0)")
    print(f"  stddev_popularity: {stats['stddev_popularity']:.2f}")
    print(f"  track_count: {stats['track_count']} (expected 5)")
    
    # Verify results
    if abs(stats['avg_popularity'] - 70.0) > 0.1:
        print("  ✗ ERROR: Average should be 70.0")
        return False
    
    if abs(stats['median_popularity'] - 70.0) > 0.1:
        print("  ✗ ERROR: Median should be 70.0")
        return False
    
    if stats['track_count'] != 5:
        print("  ✗ ERROR: Track count should be 5")
        return False
    
    conn.close()
    print("  ✅ calculate_artist_popularity_stats tests passed!\n")
    return True


def test_should_exclude_from_stats_with_alternate_takes():
    """Test excluding alternate takes from statistics."""
    print("=" * 80)
    print("Test 5: should_exclude_from_stats() with alternate_takes_map")
    print("=" * 80)
    
    # Test data
    tracks_with_scores = [
        {"id": "1", "title": "Track One", "popularity_score": 80.0},
        {"id": "2", "title": "Track Two", "popularity_score": 75.0},
        {"id": "3", "title": "Track Three", "popularity_score": 70.0},
        {"id": "4", "title": "Track One (Live)", "popularity_score": 20.0},
        {"id": "5", "title": "Track Two (Single)", "popularity_score": 15.0},
    ]
    
    # Map alternate takes to base tracks
    alternate_takes_map = {
        "4": "1",  # Track One (Live) -> Track One
        "5": "2",  # Track Two (Single) -> Track Two
    }
    
    # Run exclusion logic
    excluded = should_exclude_from_stats(tracks_with_scores, alternate_takes_map)
    
    print(f"  Input: 5 tracks (3 base, 2 alternate)")
    print(f"  Alternate takes map: {alternate_takes_map}")
    print(f"  Excluded indices: {excluded}")
    
    # Tracks 4 and 5 should be excluded (indices 3 and 4)
    # Additionally, tracks at end with parentheses should be excluded
    expected_min = {3, 4}  # At minimum, the two alternate takes
    
    if not expected_min.issubset(excluded):
        print(f"  ✗ ERROR: Expected at least {expected_min} to be excluded")
        return False
    
    print("  ✅ should_exclude_from_stats with alternate takes passed!\n")
    return True


def main():
    """Run all tests."""
    print("\n" + "=" * 80)
    print("Testing Artist-Level Popularity and Alternate Take Detection")
    print("=" * 80 + "\n")
    
    all_passed = True
    
    # Run tests
    all_passed &= test_strip_parentheses()
    all_passed &= test_detect_alternate_takes()
    all_passed &= test_should_skip_spotify_lookup()
    all_passed &= test_calculate_artist_popularity_stats()
    all_passed &= test_should_exclude_from_stats_with_alternate_takes()
    
    print("=" * 80)
    if all_passed:
        print("✅ All tests passed!")
        print("=" * 80)
        return 0
    else:
        print("❌ Some tests failed!")
        print("=" * 80)
        return 1


if __name__ == "__main__":
    sys.exit(main())
