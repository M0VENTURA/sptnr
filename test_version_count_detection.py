#!/usr/bin/env python3
"""
Test version count-based single detection logic.

This test verifies:
1. Mean version count calculation for albums
2. Version count standout detection (version_count >= mean + 1)
3. Medium confidence without marking as single
4. 5-star rating when combined with popularity threshold
"""

import sqlite3
import tempfile
import os
from single_detection_enhanced import (
    calculate_mean_version_count,
    is_version_count_standout,
    infer_from_popularity,
    determine_final_status
)


def test_version_count_calculation():
    """Test mean version count calculation."""
    print("\n=== Test 1: Mean Version Count Calculation ===")
    
    # Create temporary database
    with tempfile.NamedTemporaryFile(mode='w', suffix='.db', delete=False) as f:
        db_path = f.name
    
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Create tracks table with minimal schema
        cursor.execute("""
            CREATE TABLE tracks (
                id TEXT PRIMARY KEY,
                artist TEXT,
                album TEXT,
                title TEXT,
                spotify_version_count INTEGER
            )
        """)
        
        # Insert test data: Album with 5 tracks
        # Track 1: 10 versions
        # Track 2: 8 versions
        # Track 3: 6 versions
        # Track 4: 4 versions
        # Track 5: 2 versions
        # Mean = (10 + 8 + 6 + 4 + 2) / 5 = 6.0
        test_tracks = [
            ('t1', 'Artist A', 'Album X', 'Track 1', 10),
            ('t2', 'Artist A', 'Album X', 'Track 2', 8),
            ('t3', 'Artist A', 'Album X', 'Track 3', 6),
            ('t4', 'Artist A', 'Album X', 'Track 4', 4),
            ('t5', 'Artist A', 'Album X', 'Track 5', 2),
        ]
        
        for track in test_tracks:
            cursor.execute("""
                INSERT INTO tracks (id, artist, album, title, spotify_version_count)
                VALUES (?, ?, ?, ?, ?)
            """, track)
        
        conn.commit()
        
        # Test mean calculation
        mean_count = calculate_mean_version_count(conn, 'Artist A', 'Album X')
        print(f"Mean version count: {mean_count}")
        assert mean_count == 6.0, f"Expected 6.0, got {mean_count}"
        print("✓ Mean version count calculation correct")
        
        # Test standout detection
        # Track 1 (10 versions) should be standout: 10 >= 6 + 1 = True
        # Track 2 (8 versions) should be standout: 8 >= 7 = True
        # Track 3 (6 versions) should NOT be standout: 6 >= 7 = False
        
        assert is_version_count_standout(10, 6.0) == True, "Track 1 should be standout"
        assert is_version_count_standout(8, 6.0) == True, "Track 2 should be standout"
        assert is_version_count_standout(6, 6.0) == False, "Track 3 should NOT be standout"
        assert is_version_count_standout(4, 6.0) == False, "Track 4 should NOT be standout"
        
        print("✓ Version count standout detection correct")
        
        conn.close()
        
    finally:
        # Clean up
        if os.path.exists(db_path):
            os.unlink(db_path)


def test_medium_confidence_without_single_marking():
    """Test that version count standout gives medium confidence without marking as single."""
    print("\n=== Test 2: Medium Confidence Without Single Marking ===")
    
    # Test with version count standout
    confidence, is_single = infer_from_popularity(
        z_score=0.3,  # Below medium threshold (0.5)
        spotify_version_count=8,
        version_count_standout=True
    )
    
    print(f"Confidence: {confidence}, Is Single: {is_single}")
    assert confidence == 'medium', f"Expected 'medium', got '{confidence}'"
    assert is_single == False, f"Expected False (not marking as single), got {is_single}"
    print("✓ Version count standout gives medium confidence without single marking")
    
    # Test without version count standout
    confidence2, is_single2 = infer_from_popularity(
        z_score=0.1,  # Below low threshold
        spotify_version_count=2,  # Below 3 versions
        version_count_standout=False
    )
    
    print(f"Without standout - Confidence: {confidence2}, Is Single: {is_single2}")
    assert confidence2 == 'none', f"Expected 'none', got '{confidence2}'"
    assert is_single2 == False, f"Expected False, got {is_single2}"
    print("✓ Without standout, low z_score and version count gives no confidence")


def test_version_count_with_metadata():
    """Test that version count standout works correctly with metadata sources."""
    print("\n=== Test 3: Version Count with Metadata Sources ===")
    
    # Case 1: Version count standout + Spotify confirmation = medium confidence
    status = determine_final_status(
        discogs_confirmed=False,
        spotify_confirmed=True,
        musicbrainz_confirmed=False,
        z_score=0.3,
        spotify_version_count=8,
        version_count_standout=True
    )
    
    print(f"Spotify + Version Count Standout: {status}")
    assert status == 'medium', f"Expected 'medium', got '{status}'"
    print("✓ Version count with Spotify gives medium confidence")
    
    # Case 2: Version count standout alone (no metadata, low z-score, low version count) = none
    status2 = determine_final_status(
        discogs_confirmed=False,
        spotify_confirmed=False,
        musicbrainz_confirmed=False,
        z_score=0.1,  # Below low threshold
        spotify_version_count=2,  # Below 3 versions
        version_count_standout=True
    )
    
    print(f"Version Count Standout alone (low z-score): {status2}")
    assert status2 == 'none', f"Expected 'none', got '{status2}'"
    print("✓ Version count alone with low z-score doesn't give confidence in final status")


def test_high_z_score_overrides():
    """Test that high z-scores still work as expected."""
    print("\n=== Test 4: High Z-Score Overrides ===")
    
    # High z-score should give high confidence
    confidence, is_single = infer_from_popularity(
        z_score=1.2,
        spotify_version_count=2,
        version_count_standout=False
    )
    
    print(f"High z-score: Confidence={confidence}, Is Single={is_single}")
    assert confidence == 'high', f"Expected 'high', got '{confidence}'"
    assert is_single == True, f"Expected True, got {is_single}"
    print("✓ High z-score still gives high confidence and marks as single")
    
    # Medium z-score should give medium confidence
    confidence2, is_single2 = infer_from_popularity(
        z_score=0.6,
        spotify_version_count=2,
        version_count_standout=False
    )
    
    print(f"Medium z-score: Confidence={confidence2}, Is Single={is_single2}")
    assert confidence2 == 'medium', f"Expected 'medium', got '{confidence2}'"
    assert is_single2 == True, f"Expected True, got {is_single2}"
    print("✓ Medium z-score still gives medium confidence and marks as single")


if __name__ == '__main__':
    print("Testing Version Count-Based Single Detection")
    print("=" * 60)
    
    try:
        test_version_count_calculation()
        test_medium_confidence_without_single_marking()
        test_version_count_with_metadata()
        test_high_z_score_overrides()
        
        print("\n" + "=" * 60)
        print("✅ All tests passed!")
        print("=" * 60)
        
    except AssertionError as e:
        print(f"\n❌ Test failed: {e}")
        exit(1)
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        exit(1)
