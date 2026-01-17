#!/usr/bin/env python3
"""
Test script for the parenthesis-based track filtering logic.
"""

import sys
import os
import re

# Copy the function here to test without importing the full module
def should_exclude_from_stats(tracks_with_scores):
    """
    Identify tracks that should be excluded from popularity statistics calculation.
    
    Excludes tracks at the end of an album when there are multiple consecutive tracks
    with parenthetical content (e.g., "Live in Wacken 2022"), as these bonus/alternate
    versions can skew the popularity mean and z-scores.
    
    Args:
        tracks_with_scores: List of track dictionaries ordered by popularity (descending)
        
    Returns:
        Set of track indices to exclude from statistics
    """
    
    if not tracks_with_scores or len(tracks_with_scores) < 3:
        # Don't filter albums with too few tracks
        return set()
    
    # Check for parentheses in track titles
    # Tracks are ordered by popularity DESC, so the end of album (low popularity) is at the end of the list
    tracks_with_parens = []
    for i, track in enumerate(tracks_with_scores):
        title = track.get("title", "")
        # Check if title contains parenthetical content
        if re.search(r'\([^)]+\)', title):
            tracks_with_parens.append(i)
    
    # Only exclude if we have multiple tracks with parentheses
    if len(tracks_with_parens) < 2:
        return set()
    
    # Find consecutive tracks with parentheses at the END of the track list
    # Since tracks are sorted by popularity DESC, the last indices are the end of the album
    tracks_with_parens_set = set(tracks_with_parens)  # O(1) membership testing
    
    # Build a list of consecutive tracks starting from the last track index
    consecutive_at_end = []
    last_track_idx = len(tracks_with_scores) - 1
    
    # Start from the last track and work backwards
    for i in range(last_track_idx, -1, -1):
        if i in tracks_with_parens_set:
            # This track has parentheses
            if not consecutive_at_end:
                # First track in the sequence (must be the last track)
                consecutive_at_end.insert(0, i)
            elif i == consecutive_at_end[0] - 1:
                # Consecutive with previous track
                consecutive_at_end.insert(0, i)
            else:
                # Gap found, stop looking
                break
        elif consecutive_at_end:
            # We've started building a sequence but hit a track without parentheses
            # This means the sequence is not at the end
            break
    
    # Only exclude if we have at least 2 consecutive tracks with parentheses at the end
    if len(consecutive_at_end) >= 2:
        return set(consecutive_at_end)
    
    return set()


def test_basic_exclusion():
    """Test that multiple tracks with parentheses at the end are excluded."""
    tracks = [
        {"title": "Uruk-Hai", "popularity_score": 67.5},
        {"title": "SGFRD Dragonslayer", "popularity_score": 67.0},
        {"title": "Die Horde", "popularity_score": 66.5},
        {"title": "Knochenkarussell", "popularity_score": 64.5},
        {"title": "Valkyren", "popularity_score": 64.5},
        {"title": "Morrigan", "popularity_score": 62.5},
        {"title": "Bastard von Asgard", "popularity_score": 43.5},
        {"title": "Untot im Drachenboot (Live in Wacken 2022)", "popularity_score": 12.0},
        {"title": "Memento Mori (Live in Wacken 2022)", "popularity_score": 10.0},
        {"title": "Intro (Das elfte Gebot) (Live in Wacken 2022)", "popularity_score": 9.0},
        {"title": "Ultima Nocte (Live in Wacken 2022)", "popularity_score": 9.0},
        {"title": "Dragostea din tei (Live in Wacken 2022)", "popularity_score": 9.0},
        {"title": "Das elfte Gebot (Live in Wacken 2022)", "popularity_score": 9.0},
        {"title": "Metfest (Live in Wacken 2022)", "popularity_score": 8.5},
        {"title": "Schubsetanz (Live in Wacken 2022)", "popularity_score": 8.5},
        {"title": "Rohirrim (Live in Wacken 2022)", "popularity_score": 8.5},
        {"title": "Methämmer (Live in Wacken 2022)", "popularity_score": 8.0},
        {"title": "Warriors of the World United (Live in Wacken 2022)", "popularity_score": 7.5},
        {"title": "Die Hörner Hoch (Live in Wacken 2022)", "popularity_score": 7.0},
        {"title": "Extro (Live in Wacken 2022)", "popularity_score": 6.0},
    ]
    
    excluded = should_exclude_from_stats(tracks)
    print(f"Test 1: Basic exclusion")
    print(f"  Excluded indices: {sorted(excluded)}")
    print(f"  Expected: tracks 7-19 (the ones with 'Live in Wacken 2022')")
    
    # Check that we excluded the right tracks
    excluded_titles = [tracks[i]["title"] for i in excluded]
    print(f"  Excluded titles: {excluded_titles[:3]}... ({len(excluded_titles)} total)")
    
    # All excluded tracks should have parentheses
    assert all("(" in tracks[i]["title"] for i in excluded), "All excluded tracks should have parentheses"
    # Should exclude multiple tracks
    assert len(excluded) >= 2, "Should exclude at least 2 tracks"
    print("  ✓ Test passed!\n")


def test_no_exclusion_single_track():
    """Test that a single track with parentheses is not excluded."""
    tracks = [
        {"title": "Track 1", "popularity_score": 67.5},
        {"title": "Track 2", "popularity_score": 66.0},
        {"title": "Track 3 (Bonus)", "popularity_score": 12.0},
    ]
    
    excluded = should_exclude_from_stats(tracks)
    print(f"Test 2: No exclusion for single track with parentheses")
    print(f"  Excluded indices: {sorted(excluded)}")
    print(f"  Expected: empty set (only 1 track with parentheses)")
    assert len(excluded) == 0, "Should not exclude when only 1 track has parentheses"
    print("  ✓ Test passed!\n")


def test_no_exclusion_non_consecutive():
    """Test that non-consecutive tracks with parentheses are not excluded."""
    tracks = [
        {"title": "Track 1 (Intro)", "popularity_score": 67.5},
        {"title": "Track 2", "popularity_score": 66.0},
        {"title": "Track 3", "popularity_score": 65.0},
        {"title": "Track 4 (Outro)", "popularity_score": 12.0},
    ]
    
    excluded = should_exclude_from_stats(tracks)
    print(f"Test 3: No exclusion for non-consecutive tracks")
    print(f"  Excluded indices: {sorted(excluded)}")
    print(f"  Expected: empty set (tracks with parentheses are not consecutive at end)")
    assert len(excluded) == 0, f"Should not exclude non-consecutive tracks, got {excluded}"
    print("  ✓ Test passed!\n")


def test_exclusion_with_gap():
    """Test that tracks with a gap in parentheses are not excluded."""
    tracks = [
        {"title": "Track 1", "popularity_score": 70.0},
        {"title": "Track 2 (Live)", "popularity_score": 65.0},
        {"title": "Track 3", "popularity_score": 60.0},
        {"title": "Track 4 (Live)", "popularity_score": 12.0},
        {"title": "Track 5 (Live)", "popularity_score": 10.0},
    ]
    
    excluded = should_exclude_from_stats(tracks)
    print(f"Test 3b: Exclusion only for consecutive tracks at end")
    print(f"  Excluded indices: {sorted(excluded)}")
    print(f"  Expected: [3, 4] (only the last 2 consecutive tracks)")
    assert len(excluded) == 2, f"Should exclude only the last 2 tracks, got {len(excluded)}"
    assert excluded == {3, 4}, f"Should exclude indices [3, 4], got {excluded}"
    print("  ✓ Test passed!\n")


def test_exclusion_two_tracks_at_end():
    """Test that exactly 2 tracks with parentheses at the end are excluded."""
    tracks = [
        {"title": "Track 1", "popularity_score": 67.5},
        {"title": "Track 2", "popularity_score": 66.0},
        {"title": "Track 3", "popularity_score": 65.0},
        {"title": "Track 4 (Live)", "popularity_score": 12.0},
        {"title": "Track 5 (Live)", "popularity_score": 10.0},
    ]
    
    excluded = should_exclude_from_stats(tracks)
    print(f"Test 4: Exclusion for 2 tracks with parentheses at end")
    print(f"  Excluded indices: {sorted(excluded)}")
    print(f"  Expected: [3, 4] (the two tracks at the end)")
    assert len(excluded) == 2, f"Should exclude exactly 2 tracks, got {len(excluded)}"
    excluded_titles = [tracks[i]["title"] for i in excluded]
    print(f"  Excluded titles: {excluded_titles}")
    print("  ✓ Test passed!\n")


def test_small_album():
    """Test that small albums (< 3 tracks) are not filtered."""
    tracks = [
        {"title": "Track 1 (Live)", "popularity_score": 67.5},
        {"title": "Track 2 (Live)", "popularity_score": 66.0},
    ]
    
    excluded = should_exclude_from_stats(tracks)
    print(f"Test 5: No exclusion for small albums")
    print(f"  Excluded indices: {sorted(excluded)}")
    print(f"  Expected: empty set (album has < 3 tracks)")
    assert len(excluded) == 0, "Should not filter albums with < 3 tracks"
    print("  ✓ Test passed!\n")


if __name__ == "__main__":
    print("=" * 60)
    print("Testing parenthesis-based track filtering")
    print("=" * 60 + "\n")
    
    test_basic_exclusion()
    test_no_exclusion_single_track()
    test_no_exclusion_non_consecutive()
    test_exclusion_with_gap()
    test_exclusion_two_tracks_at_end()
    test_small_album()
    
    print("=" * 60)
    print("All tests completed!")
    print("=" * 60)
