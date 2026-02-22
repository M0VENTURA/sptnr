#!/usr/bin/env python3
"""
Test Radio Edit Detection Feature
=================================

Tests that radio edit versions found in Spotify search results
are treated as medium confidence indicators for singles.
"""

import os
import sys

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from single_detection_enhanced import determine_final_status


def test_radio_edit_medium_confidence():
    """Test that radio_edit_found is treated as medium confidence"""
    print("\n" + "="*60)
    print("TEST: Radio Edit Medium Confidence")
    print("="*60)
    
    test_cases = [
        # (discogs, spotify, mb, album_z, artist_z, versions, radio_edit, expected_status, description)
        (False, False, False, 0.0, 0.0, 0, True, 'medium', 'Radio edit alone → medium'),
        (False, False, False, 0.1, 0.1, 1, True, 'medium', 'Radio edit with low z-scores → medium'),
        (True, False, False, 0.0, 0.0, 0, True, 'high', 'Discogs + radio edit → high (Discogs takes precedence)'),
        (False, True, False, 0.0, 0.0, 0, True, 'medium', 'Spotify + radio edit → medium'),
        (False, False, False, 0.0, 0.0, 0, False, 'none', 'No radio edit, no other sources → none'),
    ]
    
    passed = 0
    failed = 0
    
    for discogs, spotify, mb, album_z, artist_z, versions, radio_edit, expected, desc in test_cases:
        result = determine_final_status(
            discogs_confirmed=discogs,
            spotify_confirmed=spotify,
            musicbrainz_confirmed=mb,
            album_z=album_z,
            artist_z=artist_z,
            spotify_version_count=versions,
            album_is_underperforming=False,
            is_artist_level_standout=False,
            discogs_video_confirmed=False,
            lastfm_single_confirmed=False,
            popularity=50.0,
            album_mean=45.0,
            has_metadata=True,
            radio_edit_found=radio_edit
        )
        
        if result == expected:
            print(f"  ✅ {desc}: {result}")
            passed += 1
        else:
            print(f"  ❌ {desc}: {result} (expected {expected})")
            failed += 1
    
    print(f"\nResult: {passed} passed, {failed} failed")
    return failed == 0


def test_radio_edit_combined_with_other_sources():
    """Test radio edit combined with other confidence sources"""
    print("\n" + "="*60)
    print("TEST: Radio Edit Combined with Other Sources")
    print("="*60)
    
    test_cases = [
        # (discogs_video, lastfm, radio_edit, expected, description)
        (True, False, True, 'medium', 'Video + radio edit → medium (2 medium sources)'),
        (False, True, True, 'medium', 'Last.fm + radio edit → medium (2 medium sources)'),
        (True, True, True, 'medium', 'Video + Last.fm + radio edit → medium (3 medium sources)'),
        (False, False, False, 'none', 'No sources → none'),
    ]
    
    passed = 0
    failed = 0
    
    for video, lastfm, radio_edit, expected, desc in test_cases:
        result = determine_final_status(
            discogs_confirmed=False,
            spotify_confirmed=False,
            musicbrainz_confirmed=False,
            album_z=0.0,
            artist_z=0.0,
            spotify_version_count=0,
            album_is_underperforming=False,
            is_artist_level_standout=False,
            discogs_video_confirmed=video,
            lastfm_single_confirmed=lastfm,
            popularity=50.0,
            album_mean=45.0,
            has_metadata=True,
            radio_edit_found=radio_edit
        )
        
        if result == expected:
            print(f"  ✅ {desc}: {result}")
            passed += 1
        else:
            print(f"  ❌ {desc}: {result} (expected {expected})")
            failed += 1
    
    print(f"\nResult: {passed} passed, {failed} failed")
    return failed == 0


if __name__ == '__main__':
    print("="*60)
    print("Radio Edit Detection Tests")
    print("="*60)
    
    all_passed = True
    
    all_passed = test_radio_edit_medium_confidence() and all_passed
    all_passed = test_radio_edit_combined_with_other_sources() and all_passed
    
    print("\n" + "="*60)
    if all_passed:
        print("✅ ALL TESTS PASSED")
    else:
        print("❌ SOME TESTS FAILED")
    print("="*60)
    
    sys.exit(0 if all_passed else 1)
