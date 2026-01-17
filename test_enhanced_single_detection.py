#!/usr/bin/env python3
"""
Test Enhanced Single Detection Algorithm
=========================================

Tests the 8-stage single detection algorithm per problem statement.
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
    normalize_title_strict,
    is_non_canonical_version_strict,
    duration_matches_strict,
    count_spotify_versions,
    should_check_track,
    calculate_z_score_strict,
    infer_from_popularity,
    determine_final_status,
    detect_single_enhanced,
    store_single_detection_result
)


def test_title_normalization():
    """Test strict title normalization per Stage 6"""
    print("\n" + "="*60)
    print("TEST: Title Normalization (Stage 6)")
    print("="*60)
    
    test_cases = [
        ("Song Name", "song name"),
        ("Song Name (Remix)", "song name"),
        ("Song Name [Live]", "song name"),
        ("Song - Acoustic Version", "song"),
        ("Song's Name!", "songs name"),
        ("SONG  NAME", "song name"),
    ]
    
    passed = 0
    failed = 0
    
    for input_title, expected in test_cases:
        result = normalize_title_strict(input_title)
        if result == expected:
            print(f"  ✅ '{input_title}' → '{result}'")
            passed += 1
        else:
            print(f"  ❌ '{input_title}' → '{result}' (expected '{expected}')")
            failed += 1
    
    print(f"\nResult: {passed} passed, {failed} failed")
    return failed == 0


def test_non_canonical_detection():
    """Test non-canonical version detection per Stage 6"""
    print("\n" + "="*60)
    print("TEST: Non-Canonical Version Detection (Stage 6)")
    print("="*60)
    
    test_cases = [
        ("Song Name", False),
        ("Song Name (Remix)", True),
        ("Song Name (Acoustic)", True),
        ("Song Name (Demo)", True),
        ("Song Name (Live)", True),
        ("Song Name (Remastered)", True),
        ("Song Name (Extended Mix)", True),
        ("Song Name - Alt Version", True),
        ("The Song", False),
    ]
    
    passed = 0
    failed = 0
    
    for title, expected in test_cases:
        result = is_non_canonical_version_strict(title)
        if result == expected:
            print(f"  ✅ '{title}' → {result}")
            passed += 1
        else:
            print(f"  ❌ '{title}' → {result} (expected {expected})")
            failed += 1
    
    print(f"\nResult: {passed} passed, {failed} failed")
    return failed == 0


def test_duration_matching():
    """Test duration matching per Stage 6"""
    print("\n" + "="*60)
    print("TEST: Duration Matching (±2 seconds)")
    print("="*60)
    
    test_cases = [
        (180.0, 180.0, True),
        (180.0, 181.5, True),
        (180.0, 182.0, True),
        (180.0, 183.0, False),
        (180.0, None, True),
        (None, 180.0, True),
        (None, None, True),
    ]
    
    passed = 0
    failed = 0
    
    for dur1, dur2, expected in test_cases:
        result = duration_matches_strict(dur1, dur2)
        if result == expected:
            print(f"  ✅ {dur1} vs {dur2} → {result}")
            passed += 1
        else:
            print(f"  ❌ {dur1} vs {dur2} → {result} (expected {expected})")
            failed += 1
    
    print(f"\nResult: {passed} passed, {failed} failed")
    return failed == 0


def test_z_score_inference():
    """Test popularity-based inference per Stage 5"""
    print("\n" + "="*60)
    print("TEST: Popularity-Based Inference (Stage 5)")
    print("="*60)
    
    # Test normal albums (z-score enabled)
    print("\n  Normal albums (z-score enabled):")
    test_cases_normal = [
        # (z_score, spotify_versions, expected_confidence, expected_is_single)
        (1.5, 2, 'high', True),   # z >= 1.0
        (0.8, 2, 'medium', True),  # z >= 0.5
        (0.3, 5, 'low', True),     # z >= 0.2 AND >= 3 versions
        (0.3, 2, 'none', False),   # z >= 0.2 but < 3 versions
        (0.1, 5, 'none', False),   # < 0.2 threshold
    ]
    
    passed = 0
    failed = 0
    
    for z_score, versions, exp_conf, exp_single in test_cases_normal:
        conf, is_single = infer_from_popularity(z_score, versions, False, False, False)
        if conf == exp_conf and is_single == exp_single:
            print(f"    ✅ z={z_score:.1f}, v={versions} → {conf}, single={is_single}")
            passed += 1
        else:
            print(f"    ❌ z={z_score:.1f}, v={versions} → {conf}, single={is_single}")
            print(f"       Expected: {exp_conf}, single={exp_single}")
            failed += 1
    
    # Test underperforming albums (z-score disabled)
    print("\n  Underperforming albums (z-score disabled):")
    test_cases_underperforming = [
        # (z_score, spotify_versions, expected_confidence, expected_is_single)
        (1.5, 2, 'none', False),   # z >= 1.0 but disabled
        (0.8, 2, 'none', False),   # z >= 0.5 but disabled
        (0.3, 5, 'none', False),   # z >= 0.2 but disabled
    ]
    
    for z_score, versions, exp_conf, exp_single in test_cases_underperforming:
        conf, is_single = infer_from_popularity(z_score, versions, False, True, False)
        if conf == exp_conf and is_single == exp_single:
            print(f"    ✅ z={z_score:.1f}, v={versions} → {conf}, single={is_single} (underperforming)")
            passed += 1
        else:
            print(f"    ❌ z={z_score:.1f}, v={versions} → {conf}, single={is_single} (underperforming)")
            print(f"       Expected: {exp_conf}, single={exp_single}")
            failed += 1
    
    # Test underperforming albums with artist-level standout (z-score re-enabled)
    print("\n  Underperforming albums with artist-level standout (z-score re-enabled):")
    test_cases_standout = [
        # (z_score, spotify_versions, expected_confidence, expected_is_single)
        (1.5, 2, 'high', True),    # z >= 1.0 and standout
        (0.8, 2, 'medium', True),  # z >= 0.5 and standout
        (0.3, 5, 'low', True),     # z >= 0.2 AND >= 3 versions and standout
    ]
    
    for z_score, versions, exp_conf, exp_single in test_cases_standout:
        conf, is_single = infer_from_popularity(z_score, versions, False, True, True)
        if conf == exp_conf and is_single == exp_single:
            print(f"    ✅ z={z_score:.1f}, v={versions} → {conf}, single={is_single} (standout)")
            passed += 1
        else:
            print(f"    ❌ z={z_score:.1f}, v={versions} → {conf}, single={is_single} (standout)")
            print(f"       Expected: {exp_conf}, single={exp_single}")
            failed += 1
    
    print(f"\nResult: {passed} passed, {failed} failed")
    return failed == 0


def test_final_status_determination():
    """Test final status determination per Stage 7"""
    print("\n" + "="*60)
    print("TEST: Final Status Determination (Stage 7)")
    print("="*60)
    
    # Test normal albums (z-score enabled)
    print("\n  Normal albums (z-score enabled):")
    test_cases_normal = [
        # (discogs, spotify, mb, z_score, versions, expected_status)
        (True, False, False, 0.0, 0, 'high'),    # Discogs confirms
        (False, False, False, 1.2, 2, 'high'),   # z >= 1.0
        (False, True, False, 0.3, 2, 'medium'),  # Spotify confirms
        (False, False, True, 0.3, 2, 'medium'),  # MusicBrainz confirms
        (False, False, False, 0.6, 2, 'medium'), # z >= 0.5
        (False, False, False, 0.3, 5, 'low'),    # z >= 0.2 AND >= 3 versions
        (False, False, False, 0.1, 5, 'none'),   # None of the above
    ]
    
    passed = 0
    failed = 0
    
    for discogs, spotify, mb, z, versions, expected in test_cases_normal:
        result = determine_final_status(discogs, spotify, mb, z, versions, False, False)
        if result == expected:
            print(f"    ✅ D={discogs}, S={spotify}, M={mb}, z={z}, v={versions} → {result}")
            passed += 1
        else:
            print(f"    ❌ D={discogs}, S={spotify}, M={mb}, z={z}, v={versions} → {result} (expected {expected})")
            failed += 1
    
    # Test underperforming albums (z-score disabled)
    print("\n  Underperforming albums (z-score disabled):")
    test_cases_underperforming = [
        # (discogs, spotify, mb, z_score, versions, expected_status)
        (True, False, False, 0.0, 0, 'high'),     # Discogs confirms (always works)
        (False, True, False, 0.3, 2, 'medium'),   # Spotify confirms (always works)
        (False, False, True, 0.3, 2, 'medium'),   # MusicBrainz confirms (always works)
        (False, False, False, 1.2, 2, 'none'),    # z >= 1.0 but disabled
        (False, False, False, 0.6, 2, 'none'),    # z >= 0.5 but disabled
        (False, False, False, 0.3, 5, 'none'),    # z >= 0.2 but disabled
    ]
    
    for discogs, spotify, mb, z, versions, expected in test_cases_underperforming:
        result = determine_final_status(discogs, spotify, mb, z, versions, True, False)
        if result == expected:
            print(f"    ✅ D={discogs}, S={spotify}, M={mb}, z={z}, v={versions} → {result} (underperforming)")
            passed += 1
        else:
            print(f"    ❌ D={discogs}, S={spotify}, M={mb}, z={z}, v={versions} → {result} (expected {expected}, underperforming)")
            failed += 1
    
    # Test underperforming albums with artist-level standout (z-score re-enabled)
    print("\n  Underperforming albums with artist-level standout (z-score re-enabled):")
    test_cases_standout = [
        # (discogs, spotify, mb, z_score, versions, expected_status)
        (False, False, False, 1.2, 2, 'high'),    # z >= 1.0 and standout
        (False, False, False, 0.6, 2, 'medium'),  # z >= 0.5 and standout
        (False, False, False, 0.3, 5, 'low'),     # z >= 0.2 AND >= 3 versions and standout
    ]
    
    for discogs, spotify, mb, z, versions, expected in test_cases_standout:
        result = determine_final_status(discogs, spotify, mb, z, versions, True, True)
        if result == expected:
            print(f"    ✅ D={discogs}, S={spotify}, M={mb}, z={z}, v={versions} → {result} (standout)")
            passed += 1
        else:
            print(f"    ❌ D={discogs}, S={spotify}, M={mb}, z={z}, v={versions} → {result} (expected {expected}, standout)")
            failed += 1
    
    print(f"\nResult: {passed} passed, {failed} failed")
    return failed == 0


def test_pre_filter():
    """Test pre-filter logic per Stage 1"""
    print("\n" + "="*60)
    print("TEST: Pre-Filter Logic (Stage 1)")
    print("="*60)
    
    # Album with 10 tracks, mean=50, stddev=20
    album_pops = [80, 70, 65, 60, 55, 50, 45, 40, 35, 30]
    album_mean = 53.0
    album_stddev = 16.43
    
    test_cases = [
        # (popularity, spotify_versions, expected_should_check, reason)
        (50, 7, True, "High Spotify version count (>= 5)"),
        (80, 2, True, "Top 3 by popularity"),
        (70, 2, True, "Top 3 by popularity"),
        (65, 2, True, "Top 3 by popularity"),
        (75, 1, True, "Above threshold (mean + stddev)"),
        (40, 1, False, "Not high priority"),
        (30, 1, False, "Not high priority"),
    ]
    
    passed = 0
    failed = 0
    
    for pop, versions, expected, reason in test_cases:
        result = should_check_track(pop, album_mean, album_stddev, album_pops, versions)
        if result == expected:
            print(f"  ✅ pop={pop}, v={versions} → {result} ({reason})")
            passed += 1
        else:
            print(f"  ❌ pop={pop}, v={versions} → {result} (expected {expected}) - {reason}")
            failed += 1
    
    print(f"\nResult: {passed} passed, {failed} failed")
    return failed == 0


def test_integration():
    """Test full integration with database"""
    print("\n" + "="*60)
    print("TEST: Integration Test")
    print("="*60)
    
    # Create temporary database
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = f.name
    
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Create tracks table with required columns
        cursor.execute("""
            CREATE TABLE tracks (
                id TEXT PRIMARY KEY,
                artist TEXT,
                album TEXT,
                title TEXT,
                popularity_score REAL,
                duration REAL,
                isrc TEXT,
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
        
        # Insert test tracks from an album
        test_tracks = [
            ('track1', 'Test Artist', 'Test Album', 'Hit Single', 90.0, 180.0, 'TEST001'),
            ('track2', 'Test Artist', 'Test Album', 'Another Song', 50.0, 200.0, 'TEST002'),
            ('track3', 'Test Artist', 'Test Album', 'Deep Cut', 30.0, 220.0, 'TEST003'),
            ('track4', 'Test Artist', 'Test Album', 'Filler Track', 25.0, 190.0, 'TEST004'),
        ]
        
        for track in test_tracks:
            cursor.execute("""
                INSERT INTO tracks (id, artist, album, title, popularity_score, duration, isrc)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, track)
        
        conn.commit()
        
        # Test detection on high-popularity track
        # Mock Spotify results
        spotify_results = [
            {
                'name': 'Hit Single',
                'duration_ms': 180000,
                'external_ids': {'isrc': 'TEST001'},
                'album': {'album_type': 'single', 'name': 'Hit Single - Single'}
            }
        ]
        
        result = detect_single_enhanced(
            conn=conn,
            track_id='track1',
            title='Hit Single',
            artist='Test Artist',
            album='Test Album',
            duration=180.0,
            isrc='TEST001',
            popularity=90.0,
            spotify_results=spotify_results,
            discogs_client=None,  # Mock would go here
            musicbrainz_client=None,  # Mock would go here
            verbose=True
        )
        
        print(f"\nDetection Result:")
        print(f"  Is Single: {result['is_single']}")
        print(f"  Status: {result['single_status']}")
        print(f"  Confidence Score: {result['single_confidence_score']}")
        print(f"  Sources: {result['single_sources']}")
        print(f"  Z-Score: {result['z_score']:.2f}")
        print(f"  Spotify Versions: {result['spotify_version_count']}")
        
        # Store result
        store_single_detection_result(conn, 'track1', result)
        
        # Verify storage
        cursor.execute("SELECT single_status, z_score FROM tracks WHERE id = ?", ('track1',))
        row = cursor.fetchone()
        
        if row:
            print(f"\n✅ Database storage verified:")
            print(f"  Status: {row[0]}")
            print(f"  Z-Score: {row[1]:.2f}")
            success = True
        else:
            print(f"\n❌ Database storage failed")
            success = False
        
        conn.close()
        return success
        
    finally:
        # Clean up
        os.unlink(db_path)


if __name__ == '__main__':
    print("\n" + "="*60)
    print("ENHANCED SINGLE DETECTION ALGORITHM TEST SUITE")
    print("="*60)
    
    all_passed = True
    
    all_passed &= test_title_normalization()
    all_passed &= test_non_canonical_detection()
    all_passed &= test_duration_matching()
    all_passed &= test_z_score_inference()
    all_passed &= test_final_status_determination()
    all_passed &= test_pre_filter()
    all_passed &= test_integration()
    
    print("\n" + "="*60)
    if all_passed:
        print("✅ ALL TESTS PASSED")
    else:
        print("❌ SOME TESTS FAILED")
    print("="*60)
    
    sys.exit(0 if all_passed else 1)
