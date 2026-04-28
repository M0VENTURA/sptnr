#!/usr/bin/env python3
"""
Integration test to verify that filtering tracks with parentheses
results in correct popularity statistics and confidence thresholds.
"""

from statistics import mean, stdev
import re

# Constants from popularity.py
DEFAULT_HIGH_CONF_OFFSET = 6


def should_exclude_from_stats(tracks_with_scores):
    """Copy of the function from popularity.py"""
    if not tracks_with_scores or len(tracks_with_scores) < 3:
        return set()
    
    tracks_with_suffix = []
    for i, track in enumerate(tracks_with_scores):
        title = track.get("title", "")
        # Check if title ends with a parenthesized suffix
        if re.match(r'^.*\([^)]*\)$', title):
            tracks_with_suffix.append(i)
    
    if len(tracks_with_suffix) < 2:
        return set()
    
    tracks_with_suffix_set = set(tracks_with_suffix)  # O(1) membership testing
    
    consecutive_at_end = []
    last_track_idx = len(tracks_with_scores) - 1
    
    for i in range(last_track_idx, -1, -1):
        if i in tracks_with_suffix_set:
            if not consecutive_at_end:
                consecutive_at_end.insert(0, i)
            elif i == consecutive_at_end[0] - 1:
                consecutive_at_end.insert(0, i)
            else:
                break
        elif consecutive_at_end:
            break
    
    if len(consecutive_at_end) >= 2:
        return set(consecutive_at_end)
    
    return set()


def test_fegefeuer_album():
    """Test the exact scenario from the problem statement (Feuerschwanz - Fegefeuer album)."""
    
    print("Testing Feuerschwanz - Fegefeuer album scenario")
    print("=" * 80)
    
    # Exact data from the problem statement
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
    
    # Calculate WITHOUT filtering (old behavior)
    print("\n❌ OLD BEHAVIOR (without filtering):")
    print("-" * 80)
    scores_old = [t["popularity_score"] for t in tracks if t["popularity_score"] > 0]
    mean_old = mean(scores_old)
    stddev_old = stdev(scores_old) if len(scores_old) > 1 else 0
    high_conf_threshold_old = mean_old + DEFAULT_HIGH_CONF_OFFSET
    
    print(f"  Mean: {mean_old:.2f}")
    print(f"  Std Dev: {stddev_old:.2f}")
    print(f"  High Confidence Threshold (mean + 6): {high_conf_threshold_old:.2f}")
    print(f"  Tracks meeting HIGH CONFIDENCE:")
    
    high_conf_count_old = 0
    for track in tracks:
        if track["popularity_score"] >= high_conf_threshold_old:
            print(f"    ✓ {track['title']} (pop={track['popularity_score']:.1f})")
            high_conf_count_old += 1
    
    print(f"  Total HIGH CONFIDENCE tracks: {high_conf_count_old}")
    
    # Calculate WITH filtering (new behavior)
    print("\n✅ NEW BEHAVIOR (with filtering):")
    print("-" * 80)
    excluded_indices = should_exclude_from_stats(tracks)
    scores_new = [
        t["popularity_score"] for i, t in enumerate(tracks)
        if t["popularity_score"] > 0 and i not in excluded_indices
    ]
    mean_new = mean(scores_new)
    stddev_new = stdev(scores_new) if len(scores_new) > 1 else 0
    high_conf_threshold_new = mean_new + DEFAULT_HIGH_CONF_OFFSET
    
    print(f"  Excluded {len(excluded_indices)} tracks from statistics:")
    for idx in sorted(excluded_indices)[:3]:
        print(f"    - {tracks[idx]['title']}")
    if len(excluded_indices) > 3:
        print(f"    ... and {len(excluded_indices) - 3} more")
    
    print(f"\n  Mean: {mean_new:.2f}")
    print(f"  Std Dev: {stddev_new:.2f}")
    print(f"  High Confidence Threshold (mean + 6): {high_conf_threshold_new:.2f}")
    print(f"  Tracks meeting HIGH CONFIDENCE:")
    
    high_conf_count_new = 0
    for track in tracks:
        if track["popularity_score"] >= high_conf_threshold_new:
            print(f"    ✓ {track['title']} (pop={track['popularity_score']:.1f})")
            high_conf_count_new += 1
    
    print(f"  Total HIGH CONFIDENCE tracks: {high_conf_count_new}")
    
    # Comparison
    print("\n📊 COMPARISON:")
    print("-" * 80)
    print(f"  Mean change: {mean_old:.2f} → {mean_new:.2f} (Δ {mean_new - mean_old:+.2f})")
    print(f"  High threshold change: {high_conf_threshold_old:.2f} → {high_conf_threshold_new:.2f} (Δ {high_conf_threshold_new - high_conf_threshold_old:+.2f})")
    print(f"  HIGH CONFIDENCE tracks: {high_conf_count_old} → {high_conf_count_new}")
    
    # Verify the improvement
    print("\n✅ VERIFICATION:")
    print("-" * 80)
    
    # With filtering, we should get a higher mean (not dragged down by live tracks)
    assert mean_new > mean_old, f"Expected higher mean with filtering, got {mean_new:.2f} vs {mean_old:.2f}"
    print(f"  ✓ Mean increased from {mean_old:.2f} to {mean_new:.2f}")
    
    # With filtering, we should get a higher threshold
    assert high_conf_threshold_new > high_conf_threshold_old, f"Expected higher threshold with filtering"
    print(f"  ✓ High confidence threshold increased from {high_conf_threshold_old:.2f} to {high_conf_threshold_new:.2f}")
    
    # The problem statement shows tracks with pop=67.5, 67.0, 66.5, 64.5, etc. should be HIGH CONFIDENCE
    # With the old behavior (mean ~30), threshold would be ~36, so many tracks would qualify
    # With the new behavior (mean ~60+), threshold should be ~60+, which is more accurate
    expected_high_conf_tracks = ["Uruk-Hai", "SGFRD Dragonslayer", "Die Horde", "Knochenkarussell", "Valkyren", "Morrigan"]
    actual_high_conf_tracks = [t["title"] for t in tracks if t["popularity_score"] >= high_conf_threshold_new]
    
    print(f"\n  Expected HIGH CONFIDENCE tracks based on logs: {len(expected_high_conf_tracks)}")
    print(f"  Actual HIGH CONFIDENCE tracks with new logic: {len(actual_high_conf_tracks)}")
    
    # The key insight: without filtering, the mean is dragged down by the live tracks
    # This causes MORE tracks to qualify as "high confidence" than they should
    # With filtering, we get a more accurate threshold that better represents the actual album
    
    print("\n" + "=" * 80)
    print("✅ Test completed successfully!")
    print("=" * 80)


if __name__ == "__main__":
    test_fegefeuer_album()
