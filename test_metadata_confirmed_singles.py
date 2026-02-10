#!/usr/bin/env python3
"""
Test case for verifying that Spotify/MusicBrainz/Discogs video confirmed singles
are marked as medium confidence (and therefore is_single=True), not low confidence.

This test addresses the issue where tracks confirmed as singles by external sources
were incorrectly being marked as is_single=False due to having "low" confidence status.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from single_detection_enhanced import determine_final_status


def test_metadata_confirmed_singles():
    """
    Test that metadata-confirmed singles (Spotify, MusicBrainz, Discogs video)
    return 'medium' status and is_single=True.
    """
    print("\n" + "="*60)
    print("TEST: Metadata-Confirmed Singles Status")
    print("="*60)
    
    test_cases = [
        {
            "name": "Spotify confirmed single",
            "discogs": False,
            "spotify": True,
            "musicbrainz": False,
            "discogs_video": False,
            "expected_status": "medium",
            "description": "Single confirmed by Spotify should be medium confidence"
        },
        {
            "name": "MusicBrainz confirmed single",
            "discogs": False,
            "spotify": False,
            "musicbrainz": True,
            "discogs_video": False,
            "expected_status": "medium",
            "description": "Single confirmed by MusicBrainz should be medium confidence"
        },
        {
            "name": "Discogs video confirmed",
            "discogs": False,
            "spotify": False,
            "musicbrainz": False,
            "discogs_video": True,
            "expected_status": "medium",
            "description": "Track with Discogs music video should be medium confidence"
        },
        {
            "name": "Multiple medium sources",
            "discogs": False,
            "spotify": True,
            "musicbrainz": True,
            "discogs_video": False,
            "expected_status": "medium",
            "description": "Multiple medium-confidence sources should still be medium"
        },
        {
            "name": "Discogs confirmed (high confidence)",
            "discogs": True,
            "spotify": False,
            "musicbrainz": False,
            "discogs_video": False,
            "expected_status": "high",
            "description": "Discogs single confirmation should be high confidence"
        },
    ]
    
    passed = 0
    failed = 0
    
    for test_case in test_cases:
        status = determine_final_status(
            discogs_confirmed=test_case["discogs"],
            spotify_confirmed=test_case["spotify"],
            musicbrainz_confirmed=test_case["musicbrainz"],
            album_z=0.0,
            artist_z=0.0,
            spotify_version_count=0,
            album_is_underperforming=False,
            is_artist_level_standout=False,
            discogs_video_confirmed=test_case["discogs_video"],
            popularity=15.0,
            album_mean=15.0,
            has_metadata=True
        )
        
        is_single = status in ('high', 'medium')
        expected_is_single = test_case["expected_status"] in ('high', 'medium')
        
        if status == test_case["expected_status"] and is_single == expected_is_single:
            print(f"  ✅ {test_case['name']}: status={status}, is_single={is_single}")
            passed += 1
        else:
            print(f"  ❌ {test_case['name']}: status={status} (expected {test_case['expected_status']})")
            print(f"     {test_case['description']}")
            failed += 1
    
    print(f"\nResult: {passed} passed, {failed} failed")
    print("="*60)
    
    return failed == 0


if __name__ == "__main__":
    success = test_metadata_confirmed_singles()
    sys.exit(0 if success else 1)
