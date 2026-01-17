#!/usr/bin/env python3
"""
Test script to demonstrate the popularity-based confidence system.
This shows how the new rating logic works with sample data.
"""

import sys
import os
from statistics import mean, stdev
import heapq

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Constants from popularity.py
DEFAULT_POPULARITY_MEAN = 10
DEFAULT_HIGH_CONF_OFFSET = 6
DEFAULT_MEDIUM_CONF_THRESHOLD = -0.3


def calculate_confidence_thresholds(popularity_scores):
    """
    Calculate high and medium confidence thresholds for an album.
    
    Args:
        popularity_scores: List of popularity scores for tracks in the album
        
    Returns:
        Tuple of (high_conf_threshold, medium_conf_zscore_threshold, mean, stddev)
    """
    valid_scores = [s for s in popularity_scores if s > 0]
    
    if not valid_scores:
        return (
            DEFAULT_POPULARITY_MEAN + DEFAULT_HIGH_CONF_OFFSET,
            DEFAULT_MEDIUM_CONF_THRESHOLD,
            DEFAULT_POPULARITY_MEAN,
            0
        )
    
    # Calculate statistics
    popularity_mean = mean(valid_scores)
    popularity_stddev = stdev(valid_scores) if len(valid_scores) > 1 else 0
    
    # Calculate z-scores
    zscores = []
    for score in valid_scores:
        if popularity_stddev > 0:
            zscore = (score - popularity_mean) / popularity_stddev
        else:
            zscore = 0
        zscores.append(zscore)
    
    # Get mean of top 50% z-scores
    if zscores:
        top_50_count = max(1, len(zscores) // 2)
        top_50_zscores = heapq.nlargest(top_50_count, zscores)
        mean_top50_zscore = mean(top_50_zscores)
    else:
        mean_top50_zscore = 0
    
    # Calculate thresholds
    high_conf_threshold = popularity_mean + DEFAULT_HIGH_CONF_OFFSET
    medium_conf_zscore_threshold = mean_top50_zscore + DEFAULT_MEDIUM_CONF_THRESHOLD
    
    return (high_conf_threshold, medium_conf_zscore_threshold, popularity_mean, popularity_stddev)


def determine_rating(track_name, popularity, has_metadata, high_threshold, medium_threshold, mean_pop, stddev_pop):
    """
    Determine the rating for a track using the new confidence system.
    
    Args:
        track_name: Name of the track
        popularity: Popularity score
        has_metadata: Whether metadata confirms this is a single
        high_threshold: High confidence threshold (mean + 6)
        medium_threshold: Medium confidence z-score threshold
        mean_pop: Album mean popularity
        stddev_pop: Album stddev popularity
        
    Returns:
        Tuple of (stars, reason)
    """
    # Calculate z-score
    if stddev_pop > 0 and popularity > 0:
        zscore = (popularity - mean_pop) / stddev_pop
    else:
        zscore = 0
    
    # Check high confidence
    if popularity >= high_threshold:
        return (5, f"HIGH CONFIDENCE (pop={popularity:.1f} >= {high_threshold:.1f})")
    
    # Check medium confidence
    if zscore >= medium_threshold:
        if has_metadata:
            return (5, f"MEDIUM CONFIDENCE (zscore={zscore:.2f} >= {medium_threshold:.2f}, has metadata)")
        else:
            return (3, f"Medium threshold met but no metadata (zscore={zscore:.2f})")
    
    # Fallback to band-based rating
    return (3, f"Band-based rating (zscore={zscore:.2f})")


def test_flat_album():
    """Test with a flat album (similar popularity across tracks)"""
    print("\n" + "=" * 70)
    print("TEST 1: FLAT ALBUM (Similar Popularity)")
    print("=" * 70)
    
    # Album with similar popularity scores
    tracks = [
        ("Track 1", 45, False),
        ("Track 2", 48, False),
        ("Track 3", 46, True),  # Has metadata
        ("Track 4", 47, False),
        ("Track 5", 44, False),
        ("Track 6", 45, False),
    ]
    
    scores = [t[1] for t in tracks]
    high_thresh, med_thresh, mean_pop, stddev_pop = calculate_confidence_thresholds(scores)
    
    print(f"Album Stats: mean={mean_pop:.1f}, stddev={stddev_pop:.1f}")
    print(f"High confidence threshold: {high_thresh:.1f}")
    print(f"Medium confidence z-score threshold: {med_thresh:.2f}")
    print()
    
    for name, pop, has_meta in tracks:
        stars, reason = determine_rating(name, pop, has_meta, high_thresh, med_thresh, mean_pop, stddev_pop)
        print(f"{'★' * stars}{'☆' * (5 - stars)} ({stars}/5) - {name} - {reason}")


def test_spiky_album():
    """Test with a spiky album (some tracks much more popular)"""
    print("\n" + "=" * 70)
    print("TEST 2: SPIKY ALBUM (Some Very Popular Tracks)")
    print("=" * 70)
    
    # Album with some very popular tracks
    tracks = [
        ("Hit Single", 85, True),  # Very popular with metadata
        ("Popular Track", 72, False),
        ("Another Single", 65, True),  # Popular with metadata
        ("Album Track 1", 35, False),
        ("Album Track 2", 32, False),
        ("Album Track 3", 30, False),
        ("Album Track 4", 28, False),
        ("Deep Cut", 25, False),
    ]
    
    scores = [t[1] for t in tracks]
    high_thresh, med_thresh, mean_pop, stddev_pop = calculate_confidence_thresholds(scores)
    
    print(f"Album Stats: mean={mean_pop:.1f}, stddev={stddev_pop:.1f}")
    print(f"High confidence threshold: {high_thresh:.1f}")
    print(f"Medium confidence z-score threshold: {med_thresh:.2f}")
    print()
    
    for name, pop, has_meta in tracks:
        stars, reason = determine_rating(name, pop, has_meta, high_thresh, med_thresh, mean_pop, stddev_pop)
        print(f"{'★' * stars}{'☆' * (5 - stars)} ({stars}/5) - {name} - {reason}")


def test_compilation():
    """Test with a compilation album"""
    print("\n" + "=" * 70)
    print("TEST 3: COMPILATION ALBUM (Greatest Hits)")
    print("=" * 70)
    
    # Compilation with many popular singles
    tracks = [
        ("Mega Hit #1", 90, True),
        ("Mega Hit #2", 88, True),
        ("Famous Single", 82, True),
        ("Another Hit", 78, True),
        ("Classic Track", 75, True),
        ("Popular B-Side", 65, False),
        ("Album Cut", 55, False),
        ("Bonus Track", 45, False),
    ]
    
    scores = [t[1] for t in tracks]
    high_thresh, med_thresh, mean_pop, stddev_pop = calculate_confidence_thresholds(scores)
    
    print(f"Album Stats: mean={mean_pop:.1f}, stddev={stddev_pop:.1f}")
    print(f"High confidence threshold: {high_thresh:.1f}")
    print(f"Medium confidence z-score threshold: {med_thresh:.2f}")
    print()
    
    for name, pop, has_meta in tracks:
        stars, reason = determine_rating(name, pop, has_meta, high_thresh, med_thresh, mean_pop, stddev_pop)
        print(f"{'★' * stars}{'☆' * (5 - stars)} ({stars}/5) - {name} - {reason}")


def test_niche_album():
    """Test with a niche/low popularity album"""
    print("\n" + "=" * 70)
    print("TEST 4: NICHE ALBUM (Low Overall Popularity)")
    print("=" * 70)
    
    # Niche album with low overall popularity
    tracks = [
        ("Fan Favorite", 18, True),  # Relatively popular for this artist
        ("Track 2", 12, False),
        ("Track 3", 10, False),
        ("Track 4", 8, False),
        ("Track 5", 7, False),
        ("Deep Cut", 5, False),
    ]
    
    scores = [t[1] for t in tracks]
    high_thresh, med_thresh, mean_pop, stddev_pop = calculate_confidence_thresholds(scores)
    
    print(f"Album Stats: mean={mean_pop:.1f}, stddev={stddev_pop:.1f}")
    print(f"High confidence threshold: {high_thresh:.1f}")
    print(f"Medium confidence z-score threshold: {med_thresh:.2f}")
    print()
    
    for name, pop, has_meta in tracks:
        stars, reason = determine_rating(name, pop, has_meta, high_thresh, med_thresh, mean_pop, stddev_pop)
        print(f"{'★' * stars}{'☆' * (5 - stars)} ({stars}/5) - {name} - {reason}")


if __name__ == "__main__":
    print("\n" + "=" * 70)
    print("POPULARITY-BASED CONFIDENCE SYSTEM TEST")
    print("=" * 70)
    print("\nThis test demonstrates how the new rating system adapts to different")
    print("album types and popularity distributions.")
    
    test_flat_album()
    test_spiky_album()
    test_compilation()
    test_niche_album()
    
    print("\n" + "=" * 70)
    print("TEST COMPLETE")
    print("=" * 70)
    print("\nKey Observations:")
    print("1. Flat albums: Fewer high confidence tracks (requires higher pop)")
    print("2. Spiky albums: Popular tracks get high confidence automatically")
    print("3. Compilations: Many tracks can qualify with metadata")
    print("4. Niche albums: System adapts to lower overall popularity")
    print("\nThe system is adaptive and works with any popularity distribution!")
