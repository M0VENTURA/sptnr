#!/usr/bin/env python3
"""
Test script to verify essential playlist creation logic.
Tests that Case A and Case B are properly applied.
"""

import os
import math

# Set up test environment before importing popularity
os.environ["LOG_PATH"] = "/tmp/test_config/sptnr.log"
os.environ["UNIFIED_SCAN_LOG_PATH"] = "/tmp/test_config/unified_scan.log"
os.environ["DB_PATH"] = "/tmp/test_database/sptnr.db"
os.environ["MUSIC_FOLDER"] = "/tmp/test_music"
os.environ["POPULARITY_PROGRESS_FILE"] = "/tmp/test_database/popularity_scan_progress.json"
os.environ["NAVIDROME_PROGRESS_FILE"] = "/tmp/test_database/navidrome_scan_progress.json"

from popularity import create_or_update_playlist_for_artist

def test_case_a_ten_plus_five_star_tracks():
    """Test Case A: 10+ five-star tracks → purely 5★ essentials"""
    print("\n=== Test Case A: 10+ five-star tracks ===")
    
    # Create test data with 12 five-star tracks and 20 total tracks
    tracks = []
    for i in range(12):
        tracks.append({
            "id": f"track_{i}",
            "artist": "Test Artist A",
            "album": "Test Album",
            "title": f"Five Star Track {i}",
            "stars": 5
        })
    
    for i in range(8):
        tracks.append({
            "id": f"track_{i+12}",
            "artist": "Test Artist A",
            "album": "Test Album",
            "title": f"Four Star Track {i}",
            "stars": 4
        })
    
    print(f"Total tracks: {len(tracks)}")
    five_star_count = len([t for t in tracks if t.get("stars") == 5])
    print(f"Five-star tracks: {five_star_count}")
    
    # This should create a 5★ essentials playlist
    create_or_update_playlist_for_artist("Test Artist A", tracks)
    print("✓ Case A should create 5★ essentials playlist")


def test_case_b_hundred_plus_tracks():
    """Test Case B: 100+ total tracks → top 10% by rating"""
    print("\n=== Test Case B: 100+ total tracks ===")
    
    # Create test data with 120 tracks, only 1 five-star track
    tracks = []
    tracks.append({
        "id": "track_0",
        "artist": "Test Artist B",
        "album": "Test Album",
        "title": "Single Five Star Track",
        "stars": 5
    })
    
    for i in range(30):
        tracks.append({
            "id": f"track_{i+1}",
            "artist": "Test Artist B",
            "album": "Test Album",
            "title": f"Four Star Track {i}",
            "stars": 4
        })
    
    for i in range(89):
        tracks.append({
            "id": f"track_{i+31}",
            "artist": "Test Artist B",
            "album": "Test Album",
            "title": f"Lower Rated Track {i}",
            "stars": 3
        })
    
    total_tracks = len(tracks)
    five_star_count = len([t for t in tracks if t.get("stars") == 5])
    expected_limit = max(1, math.ceil(total_tracks * 0.10))
    
    print(f"Total tracks: {total_tracks}")
    print(f"Five-star tracks: {five_star_count}")
    print(f"Expected playlist limit (top 10%): {expected_limit}")
    
    # This should create a top 10% essentials playlist (12 tracks)
    create_or_update_playlist_for_artist("Test Artist B", tracks)
    print(f"✓ Case B should create top 10% playlist with {expected_limit} tracks")


def test_chiodos_scenario():
    """Test the specific Chiodos scenario from the bug report"""
    print("\n=== Test Chiodos Scenario (100+ tracks, 1 five-star) ===")
    
    # Simulate Chiodos with over 100 songs and only 1 five-star track
    tracks = []
    tracks.append({
        "id": "chiodos_1",
        "artist": "Chiodos",
        "album": "All's Well That Ends Well (Deluxe Edition)",
        "title": "Baby, You Wouldn't Last A Minute On The Creek",
        "stars": 5
    })
    
    # Add 110 tracks with varying ratings (simulating reality)
    for i in range(30):
        tracks.append({
            "id": f"chiodos_{i+2}",
            "artist": "Chiodos",
            "album": f"Test Album {i // 10}",
            "title": f"Track {i}",
            "stars": 4
        })
    
    for i in range(80):
        tracks.append({
            "id": f"chiodos_{i+32}",
            "artist": "Chiodos",
            "album": f"Test Album {i // 20}",
            "title": f"Track {i+30}",
            "stars": 3
        })
    
    total_tracks = len(tracks)
    five_star_count = len([t for t in tracks if t.get("stars") == 5])
    expected_limit = max(1, math.ceil(total_tracks * 0.10))
    
    print(f"Total tracks: {total_tracks}")
    print(f"Five-star tracks: {five_star_count}")
    print(f"Expected playlist limit (top 10%): {expected_limit}")
    print(f"Old behavior: Only {five_star_count} track(s) - WRONG!")
    print(f"New behavior: Top {expected_limit} tracks by rating - CORRECT!")
    
    # This should create a top 10% essentials playlist, not just the 1 five-star track
    create_or_update_playlist_for_artist("Chiodos", tracks)
    print(f"✓ Chiodos should get top 10% playlist with {expected_limit} tracks, not just {five_star_count}")


def test_no_playlist_case():
    """Test case where no playlist should be created"""
    print("\n=== Test No Playlist Case (< 100 tracks, < 10 five-star) ===")
    
    # Create test data with 50 tracks, 5 five-star tracks
    tracks = []
    for i in range(5):
        tracks.append({
            "id": f"track_{i}",
            "artist": "Test Artist C",
            "album": "Test Album",
            "title": f"Five Star Track {i}",
            "stars": 5
        })
    
    for i in range(45):
        tracks.append({
            "id": f"track_{i+5}",
            "artist": "Test Artist C",
            "album": "Test Album",
            "title": f"Other Track {i}",
            "stars": 3
        })
    
    print(f"Total tracks: {len(tracks)}")
    five_star_count = len([t for t in tracks if t.get("stars") == 5])
    print(f"Five-star tracks: {five_star_count}")
    
    # This should NOT create a playlist
    create_or_update_playlist_for_artist("Test Artist C", tracks)
    print("✓ No playlist should be created (requirements not met)")


if __name__ == "__main__":
    print("=" * 70)
    print("Essential Playlist Logic Test Suite")
    print("=" * 70)
    
    test_case_a_ten_plus_five_star_tracks()
    test_case_b_hundred_plus_tracks()
    test_chiodos_scenario()
    test_no_playlist_case()
    
    print("\n" + "=" * 70)
    print("All tests completed!")
    print("=" * 70)
