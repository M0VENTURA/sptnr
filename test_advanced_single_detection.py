#!/usr/bin/env python3
"""
Test script for advanced single detection functionality.

Tests the comprehensive single detection rules including:
- ISRC-based matching
- Title+duration matching
- Alternate version filtering
- Live/unplugged handling
- Album deduplication
- Global popularity calculation
- Z-score based determination
- Compilation handling
"""

import os
import sys
import sqlite3
import tempfile
import logging
from pathlib import Path

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from advanced_single_detection import (
    normalize_title,
    is_alternate_version,
    is_live_version,
    normalize_album_identity,
    find_matching_versions,
    calculate_global_popularity,
    is_metadata_single,
    calculate_zscore,
    is_compilation_album,
    detect_single_advanced,
    TrackVersion
)


def test_normalize_title():
    """Test title normalization"""
    print("\n" + "="*60)
    print("TEST: Title Normalization")
    print("="*60)
    
    test_cases = [
        ("Song Name", "song name"),
        ("Song Name (Remix)", "song name"),
        ("Song Name [Live]", "song name"),
        ("Song - Live Version", "song"),
        ("Song's Name!", "songs name"),
        ("SONG  NAME", "song name"),
    ]
    
    passed = 0
    failed = 0
    
    for input_title, expected in test_cases:
        result = normalize_title(input_title)
        if result == expected:
            print(f"  ✅ '{input_title}' → '{result}'")
            passed += 1
        else:
            print(f"  ❌ '{input_title}' → '{result}' (expected '{expected}')")
            failed += 1
    
    print(f"\nResult: {passed} passed, {failed} failed")
    return failed == 0


def test_alternate_version_detection():
    """Test alternate version filtering"""
    print("\n" + "="*60)
    print("TEST: Alternate Version Detection")
    print("="*60)
    
    test_cases = [
        ("Song Name", False),
        ("Song Name (Remix)", True),
        ("Song Name (Acoustic)", True),
        ("Song Name (Demo)", True),
        ("Song Name (Instrumental)", True),
        ("Song Name (Radio Edit)", True),
        ("Song Name (Extended)", True),
        ("Song Name (Club Mix)", True),
        ("Song Name (Alternate)", True),
        ("Song Name (Re-recorded)", True),
        ("Song Name (Karaoke)", True),
        ("Song Name (Cover)", True),
    ]
    
    passed = 0
    failed = 0
    
    for title, expected in test_cases:
        result = is_alternate_version(title)
        if result == expected:
            print(f"  ✅ '{title}' → {result}")
            passed += 1
        else:
            print(f"  ❌ '{title}' → {result} (expected {expected})")
            failed += 1
    
    print(f"\nResult: {passed} passed, {failed} failed")
    return failed == 0


def test_live_version_detection():
    """Test live/unplugged detection"""
    print("\n" + "="*60)
    print("TEST: Live/Unplugged Detection")
    print("="*60)
    
    test_cases = [
        ("Song Name", "Studio Album", False),
        ("Song Name (Live)", "Studio Album", True),
        ("Song Name", "Live at MSG", True),
        ("Song Name (Unplugged)", "Studio Album", True),
        ("Song Name", "MTV Unplugged", True),
    ]
    
    passed = 0
    failed = 0
    
    for title, album, expected in test_cases:
        result = is_live_version(title, album)
        if result == expected:
            print(f"  ✅ '{title}' / '{album}' → {result}")
            passed += 1
        else:
            print(f"  ❌ '{title}' / '{album}' → {result} (expected {expected})")
            failed += 1
    
    print(f"\nResult: {passed} passed, {failed} failed")
    return failed == 0


def test_zscore_calculation():
    """Test z-score calculation"""
    print("\n" + "="*60)
    print("TEST: Z-Score Calculation")
    print("="*60)
    
    test_cases = [
        (50, [10, 20, 30, 40, 50], 1.414),  # Above mean
        (30, [10, 20, 30, 40, 50], 0.0),    # At mean
        (10, [10, 20, 30, 40, 50], -1.414), # Below mean
        (100, [10, 10, 10, 10, 10], 0.0),   # Zero stddev
    ]
    
    passed = 0
    failed = 0
    
    for popularity, album_pops, expected_zscore in test_cases:
        result = calculate_zscore(popularity, album_pops)
        # Allow small floating point differences
        if abs(result - expected_zscore) < 0.01:
            print(f"  ✅ pop={popularity}, album={album_pops} → z={result:.3f}")
            passed += 1
        else:
            print(f"  ❌ pop={popularity}, album={album_pops} → z={result:.3f} (expected {expected_zscore:.3f})")
            failed += 1
    
    print(f"\nResult: {passed} passed, {failed} failed")
    return failed == 0


def test_compilation_detection():
    """Test compilation album detection"""
    print("\n" + "="*60)
    print("TEST: Compilation Album Detection")
    print("="*60)
    
    test_cases = [
        ("compilation", "Any Album", True),
        (None, "Greatest Hits", True),
        (None, "Best of Artist", True),
        (None, "The Collection", True),
        (None, "Anthology", True),
        ("album", "Studio Album", False),
    ]
    
    passed = 0
    failed = 0
    
    for album_type, album_name, expected in test_cases:
        result = is_compilation_album(album_type, album_name)
        if result == expected:
            print(f"  ✅ type='{album_type}', name='{album_name}' → {result}")
            passed += 1
        else:
            print(f"  ❌ type='{album_type}', name='{album_name}' → {result} (expected {expected})")
            failed += 1
    
    print(f"\nResult: {passed} passed, {failed} failed")
    return failed == 0


def test_global_popularity_calculation():
    """Test global popularity calculation across versions"""
    print("\n" + "="*60)
    print("TEST: Global Popularity Calculation")
    print("="*60)
    
    # Create test versions
    versions = [
        TrackVersion(
            track_id="1",
            title="Song Name",
            artist="Artist",
            album="Album 1",
            isrc="USXXX1234567",
            duration=180.0,
            popularity=50.0,
            is_live=False,
            is_alternate=False,
            album_type="album",
            spotify_single=True,
            musicbrainz_single=False
        ),
        TrackVersion(
            track_id="2",
            title="Song Name (Remix)",
            artist="Artist",
            album="Album 2",
            isrc="USXXX1234567",
            duration=200.0,
            popularity=80.0,  # Higher but is alternate
            is_live=False,
            is_alternate=True,
            album_type="single",
            spotify_single=True,
            musicbrainz_single=False
        ),
        TrackVersion(
            track_id="3",
            title="Song Name",
            artist="Artist",
            album="Album 3",
            isrc="USXXX1234567",
            duration=180.0,
            popularity=70.0,  # Highest canonical version
            is_live=False,
            is_alternate=False,
            album_type="album",
            spotify_single=False,
            musicbrainz_single=True
        ),
    ]
    
    result = calculate_global_popularity(versions)
    expected = 70.0  # Should ignore remix (80.0) and take max of canonical (70.0)
    
    if result == expected:
        print(f"  ✅ Global popularity: {result} (ignoring alternate version)")
        return True
    else:
        print(f"  ❌ Global popularity: {result} (expected {expected})")
        return False


def test_metadata_single_detection():
    """Test metadata single detection"""
    print("\n" + "="*60)
    print("TEST: Metadata Single Detection")
    print("="*60)
    
    # Test case 1: Spotify single
    versions1 = [
        TrackVersion(
            track_id="1",
            title="Song",
            artist="Artist",
            album="Album",
            isrc=None,
            duration=180.0,
            popularity=50.0,
            is_live=False,
            is_alternate=False,
            album_type="single",
            spotify_single=True,
            musicbrainz_single=False
        )
    ]
    
    # Test case 2: MusicBrainz single
    versions2 = [
        TrackVersion(
            track_id="2",
            title="Song",
            artist="Artist",
            album="Album",
            isrc=None,
            duration=180.0,
            popularity=50.0,
            is_live=False,
            is_alternate=False,
            album_type="album",
            spotify_single=False,
            musicbrainz_single=True
        )
    ]
    
    # Test case 3: No single metadata
    versions3 = [
        TrackVersion(
            track_id="3",
            title="Song",
            artist="Artist",
            album="Album",
            isrc=None,
            duration=180.0,
            popularity=50.0,
            is_live=False,
            is_alternate=False,
            album_type="album",
            spotify_single=False,
            musicbrainz_single=False
        )
    ]
    
    result1 = is_metadata_single(versions1)
    result2 = is_metadata_single(versions2)
    result3 = is_metadata_single(versions3)
    
    passed = 0
    failed = 0
    
    if result1:
        print(f"  ✅ Spotify single detected")
        passed += 1
    else:
        print(f"  ❌ Spotify single not detected")
        failed += 1
    
    if result2:
        print(f"  ✅ MusicBrainz single detected")
        passed += 1
    else:
        print(f"  ❌ MusicBrainz single not detected")
        failed += 1
    
    if not result3:
        print(f"  ✅ No single metadata correctly identified")
        passed += 1
    else:
        print(f"  ❌ False positive single detection")
        failed += 1
    
    print(f"\nResult: {passed} passed, {failed} failed")
    return failed == 0


def test_integrated_detection():
    """Test integrated advanced detection with database"""
    print("\n" + "="*60)
    print("TEST: Integrated Advanced Detection")
    print("="*60)
    
    # Create temporary database
    with tempfile.NamedTemporaryFile(delete=False, suffix='.db') as tmp:
        db_path = tmp.name
    
    try:
        # Create test database
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Create tracks table
        cursor.execute("""
            CREATE TABLE tracks (
                id TEXT PRIMARY KEY,
                title TEXT,
                artist TEXT,
                album TEXT,
                isrc TEXT,
                duration REAL,
                popularity_score REAL,
                spotify_album_type TEXT,
                is_spotify_single INTEGER,
                source_musicbrainz_single INTEGER
            )
        """)
        
        # Insert test data
        test_tracks = [
            # Single with high popularity and metadata
            ("1", "Hit Single", "Artist", "Album A", "USXXX1111111", 180.0, 80.0, "single", 1, 1),
            # Same song on another album (lower popularity)
            ("2", "Hit Single", "Artist", "Album B", "USXXX1111111", 180.0, 60.0, "album", 0, 1),
            # Album track with lower popularity
            ("3", "Deep Cut", "Artist", "Album A", "USXXX2222222", 200.0, 30.0, "album", 0, 0),
            # Alternate version (should be excluded)
            ("4", "Hit Single (Remix)", "Artist", "Remixes", "USXXX1111111", 220.0, 90.0, "single", 1, 0),
        ]
        
        cursor.executemany("""
            INSERT INTO tracks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, test_tracks)
        
        conn.commit()
        
        # Test detection for the single
        result = detect_single_advanced(
            conn=conn,
            track_id="1",
            title="Hit Single",
            artist="Artist",
            album="Album A",
            isrc="USXXX1111111",
            duration=180.0,
            popularity=80.0,
            album_type="single",
            zscore_threshold=0.20,
            verbose=True
        )
        
        print(f"\nDetection Result:")
        print(f"  is_single: {result['is_single']}")
        print(f"  confidence: {result['confidence']}")
        print(f"  sources: {result['sources']}")
        print(f"  global_popularity: {result['global_popularity']}")
        print(f"  zscore: {result['zscore']:.3f}")
        print(f"  metadata_single: {result['metadata_single']}")
        
        # Verify results
        passed = 0
        failed = 0
        
        # Should detect as single (metadata + high z-score)
        if result['is_single']:
            print(f"  ✅ Correctly identified as single")
            passed += 1
        else:
            print(f"  ❌ Failed to identify as single")
            failed += 1
        
        # Should have high confidence
        if result['confidence'] == 'high':
            print(f"  ✅ Correct confidence level")
            passed += 1
        else:
            print(f"  ❌ Incorrect confidence: {result['confidence']}")
            failed += 1
        
        # Global popularity should be 80 (max of canonical versions, ignoring remix)
        if result['global_popularity'] == 80.0:
            print(f"  ✅ Correct global popularity")
            passed += 1
        else:
            print(f"  ❌ Incorrect global popularity: {result['global_popularity']}")
            failed += 1
        
        conn.close()
        
        print(f"\nResult: {passed} passed, {failed} failed")
        return failed == 0
        
    finally:
        # Clean up temporary database
        if os.path.exists(db_path):
            os.unlink(db_path)


def run_all_tests():
    """Run all tests"""
    print("\n" + "="*60)
    print("ADVANCED SINGLE DETECTION TEST SUITE")
    print("="*60)
    
    results = []
    
    results.append(("Title Normalization", test_normalize_title()))
    results.append(("Alternate Version Detection", test_alternate_version_detection()))
    results.append(("Live Version Detection", test_live_version_detection()))
    results.append(("Z-Score Calculation", test_zscore_calculation()))
    results.append(("Compilation Detection", test_compilation_detection()))
    results.append(("Global Popularity", test_global_popularity_calculation()))
    results.append(("Metadata Single", test_metadata_single_detection()))
    results.append(("Integrated Detection", test_integrated_detection()))
    
    print("\n" + "="*60)
    print("FINAL RESULTS")
    print("="*60)
    
    passed = sum(1 for _, result in results if result)
    failed = len(results) - passed
    
    for test_name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"  {status}: {test_name}")
    
    print(f"\nTotal: {passed}/{len(results)} tests passed")
    
    return failed == 0


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
