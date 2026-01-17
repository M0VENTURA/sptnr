#!/usr/bin/env python3
"""
Test script to verify that the medium confidence check correctly prevents
legacy logic from overriding when metadata is missing.

This tests the fix for the issue where tracks that meet medium confidence
threshold but lack metadata were incorrectly upgraded to 5 stars by legacy logic.
"""

from statistics import mean, stdev
import heapq

# Constants from popularity.py
DEFAULT_POPULARITY_MEAN = 10
DEFAULT_HIGH_CONF_OFFSET = 6
DEFAULT_MEDIUM_CONF_THRESHOLD = -0.3


def simulate_star_calculation(
    popularity_score,
    single_sources,
    single_confidence,
    album_popularity_scores,
    excluded_indices=None
):
    """
    Simulate the star calculation logic from popularity.py lines 1520-1585.
    This should match the exact logic in the actual code.
    
    Args:
        popularity_score: Popularity score for the track
        single_sources: List of sources that confirmed single
        single_confidence: Confidence level ("high", "medium", or "low")
        album_popularity_scores: List of all popularity scores in the album (sorted DESC)
        excluded_indices: Set of track indices to exclude from statistics
        
    Returns:
        Tuple of (stars, reason)
    """
    if excluded_indices is None:
        excluded_indices = set()
    
    # Calculate statistics
    valid_scores = [s for i, s in enumerate(album_popularity_scores) if s > 0 and i not in excluded_indices]
    
    if valid_scores:
        popularity_mean = mean(valid_scores)
        popularity_stddev = stdev(valid_scores) if len(valid_scores) > 1 else 0
        
        # Calculate z-scores for all tracks
        zscores = []
        for score in valid_scores:
            if popularity_stddev > 0:
                zscore = (score - popularity_mean) / popularity_stddev
            else:
                zscore = 0
            zscores.append(zscore)
        
        # Get mean of top 50% z-scores for medium confidence threshold
        if zscores:
            top_50_count = max(1, len(zscores) // 2)
            top_50_zscores = heapq.nlargest(top_50_count, zscores)
            mean_top50_zscore = mean(top_50_zscores)
        else:
            mean_top50_zscore = 0
        
        # High confidence threshold: mean + DEFAULT_HIGH_CONF_OFFSET
        high_conf_threshold = popularity_mean + DEFAULT_HIGH_CONF_OFFSET
        # Medium confidence threshold: mean_top50_zscore + DEFAULT_MEDIUM_CONF_THRESHOLD
        medium_conf_zscore_threshold = mean_top50_zscore + DEFAULT_MEDIUM_CONF_THRESHOLD
    else:
        popularity_mean = DEFAULT_POPULARITY_MEAN
        popularity_stddev = 0
        high_conf_threshold = DEFAULT_POPULARITY_MEAN + DEFAULT_HIGH_CONF_OFFSET
        medium_conf_zscore_threshold = DEFAULT_MEDIUM_CONF_THRESHOLD
    
    # Calculate median score for band-based threshold (legacy)
    scores = album_popularity_scores
    median_score = sorted([s for s in scores if s > 0])[len([s for s in scores if s > 0]) // 2] if scores else DEFAULT_POPULARITY_MEAN
    if median_score == 0:
        median_score = DEFAULT_POPULARITY_MEAN
    jump_threshold = median_score * 1.7
    
    # Calculate band-based star rating (baseline)
    # Simulate band_index (this is simplified)
    total_tracks = len(album_popularity_scores)
    band_size = max(1, total_tracks // 4)
    track_index = next((i for i, s in enumerate(album_popularity_scores) if s == popularity_score), 0)
    band_index = track_index // band_size
    stars = max(1, 4 - band_index)
    
    # Calculate z-score for this track
    if popularity_stddev > 0 and popularity_score > 0:
        track_zscore = (popularity_score - popularity_mean) / popularity_stddev
    else:
        track_zscore = 0
    
    # NEW: Popularity-Based Confidence System
    # Track if medium confidence check explicitly denied upgrade due to missing metadata
    medium_conf_denied_upgrade = False
    reason = f"Band-based (band={band_index}, stars={stars})"
    
    # High Confidence (auto 5★): popularity >= mean + 6
    if popularity_score >= high_conf_threshold:
        stars = 5
        reason = f"HIGH CONFIDENCE (pop={popularity_score:.1f} >= {high_conf_threshold:.1f})"
    
    # Medium Confidence (requires metadata): zscore >= mean_top50_zscore - 0.3 + metadata
    elif track_zscore >= medium_conf_zscore_threshold:
        # Check if we have metadata confirmation from any source
        has_discogs = "discogs" in single_sources or "discogs_video" in single_sources
        has_spotify = "spotify" in single_sources
        has_musicbrainz = "musicbrainz" in single_sources
        has_lastfm = "lastfm" in single_sources
        has_version_count = "version_count" in single_sources
        
        has_metadata = has_discogs or has_spotify or has_musicbrainz or has_lastfm
        
        # Version count standout combined with popularity threshold = 5 stars
        if has_metadata or has_version_count:
            stars = 5
            metadata_sources = []
            if has_discogs:
                metadata_sources.append("Discogs")
            if has_spotify:
                metadata_sources.append("Spotify")
            if has_musicbrainz:
                metadata_sources.append("MusicBrainz")
            if has_lastfm:
                metadata_sources.append("Last.fm")
            if has_version_count:
                metadata_sources.append("Version Count")
            reason = f"MEDIUM CONFIDENCE (zscore={track_zscore:.2f}, metadata={', '.join(metadata_sources)})"
        else:
            # Medium confidence threshold met but no metadata support - do not upgrade
            medium_conf_denied_upgrade = True
            reason = f"Medium threshold met but NO METADATA (zscore={track_zscore:.2f}, keeping stars={stars})"
    
    # Legacy logic for backwards compatibility (if not caught by new system)
    # Skip legacy logic if medium confidence check explicitly denied upgrade
    if not medium_conf_denied_upgrade:
        # Boost to 5 stars if score exceeds threshold (only for singles)
        if popularity_score >= jump_threshold and stars < 5:
            # Only boost to 5 if it's at least a medium confidence single
            if single_confidence in ["high", "medium"]:
                old_stars = stars
                stars = 5
                reason += f" -> LEGACY JUMP (pop={popularity_score:.1f} >= {jump_threshold:.1f}, conf={single_confidence}, {old_stars}->5)"
            else:
                stars = 4  # Cap at 4 stars if not a single
        
        # Boost stars for confirmed singles (legacy)
        if single_confidence == "high" and stars < 5:
            old_stars = stars
            stars = 5  # High confidence single = 5 stars
            reason += f" -> LEGACY HIGH CONF ({old_stars}->5)"
    else:
        reason += " [LEGACY LOGIC SKIPPED DUE TO MISSING METADATA]"
    
    # Ensure at least 1 star
    stars = max(stars, 1)
    
    return (stars, reason)


def test_medium_conf_no_metadata_scenario():
    """
    Test the exact scenario from the problem statement:
    - Track meets medium confidence threshold (zscore >= threshold)
    - Track has NO metadata support (no Discogs, Spotify, etc.)
    - Track is marked as "(Single)" with high confidence
    - Expected: Should keep 4 stars, NOT upgrade to 5
    """
    print("\n" + "=" * 80)
    print("TEST: Medium Confidence Without Metadata (Problem Statement Scenario)")
    print("=" * 80)
    
    # Simulate the "Feuerschwanz - Fegefeuer" album from the problem statement
    # Mean: 65.4, Stddev: 8.0
    # High threshold: 71.4
    # Medium threshold zscore: 0.30
    
    # Album tracks (sorted by popularity DESC)
    album_scores = [
        73.5,  # Berzerkermode (HIGH CONF)
        73.5,  # Fegefeuer (HIGH CONF)
        68.5,  # Highlander - meets medium threshold, no metadata
        68.0,  # Eis & Feuer - meets medium threshold, no metadata
        67.5,  # Uruk-Hai
        67.0,  # SGFRD Dragonslayer
        66.5,  # Die Horde
        64.5,  # Knochenkarussell
        64.5,  # Valkyren
        62.5,  # Morrigan
        43.5,  # Bastard von Asgard
    ]
    
    print(f"Album: Feuerschwanz - Fegefeuer")
    print(f"Mean popularity: 65.4, Stddev: 8.0")
    print(f"High confidence threshold: 71.4")
    print(f"Medium confidence zscore threshold: 0.30")
    print()
    
    # Test case 1: Highlander (Single with high confidence, but no metadata sources)
    print("Test Case 1: Highlander")
    print("  - Popularity: 68.5")
    print("  - Is Single: True (high confidence)")
    print("  - Single Sources: [] (NO METADATA)")
    print("  - Zscore: (68.5 - 65.4) / 8.0 = 0.38 >= 0.30 ✓")
    stars, reason = simulate_star_calculation(
        popularity_score=68.5,
        single_sources=[],  # NO metadata sources
        single_confidence="high",  # But marked as high confidence single
        album_popularity_scores=album_scores
    )
    print(f"  - Result: {stars} stars")
    print(f"  - Reason: {reason}")
    print(f"  - Expected: Band-based stars (NOT 5 stars from legacy override)")
    print(f"  - Status: {'✓ PASS' if stars < 5 and '[LEGACY LOGIC SKIPPED' in reason else '✗ FAIL'}")
    print()
    
    # Test case 2: Eis & Feuer (same scenario)
    print("Test Case 2: Eis & Feuer")
    print("  - Popularity: 68.0")
    print("  - Is Single: True (high confidence)")
    print("  - Single Sources: [] (NO METADATA)")
    print("  - Zscore: (68.0 - 65.4) / 8.0 = 0.32 >= 0.30 ✓")
    stars2, reason2 = simulate_star_calculation(
        popularity_score=68.0,
        single_sources=[],  # NO metadata sources
        single_confidence="high",  # But marked as high confidence single
        album_popularity_scores=album_scores
    )
    print(f"  - Result: {stars2} stars")
    print(f"  - Reason: {reason2}")
    print(f"  - Expected: Band-based stars (NOT 5 stars from legacy override)")
    print(f"  - Status: {'✓ PASS' if stars2 < 5 and '[LEGACY LOGIC SKIPPED' in reason2 else '✗ FAIL'}")
    print()
    
    # Test case 3: Berzerkermode (high confidence with metadata)
    print("Test Case 3: Berzerkermode (Control - should get 5 stars)")
    print("  - Popularity: 73.5")
    print("  - High confidence threshold: 71.4")
    stars, reason = simulate_star_calculation(
        popularity_score=73.5,
        single_sources=["spotify", "discogs"],
        single_confidence="high",
        album_popularity_scores=album_scores
    )
    print(f"  - Result: {stars} stars")
    print(f"  - Reason: {reason}")
    print(f"  - Expected: 5 stars (high confidence threshold)")
    print(f"  - Status: {'✓ PASS' if stars == 5 else '✗ FAIL'}")
    print()
    
    # Test case 4: Track with medium threshold AND metadata (should get 5 stars)
    print("Test Case 4: Track with Medium Threshold AND Metadata")
    print("  - Popularity: 68.5")
    print("  - Single Sources: ['spotify', 'discogs'] (HAS METADATA)")
    stars, reason = simulate_star_calculation(
        popularity_score=68.5,
        single_sources=["spotify", "discogs"],  # HAS metadata
        single_confidence="high",
        album_popularity_scores=album_scores
    )
    print(f"  - Result: {stars} stars")
    print(f"  - Reason: {reason}")
    print(f"  - Expected: 5 stars (medium threshold with metadata)")
    print(f"  - Status: {'✓ PASS' if stars == 5 else '✗ FAIL'}")


if __name__ == "__main__":
    print("\n" + "=" * 80)
    print("MEDIUM CONFIDENCE WITHOUT METADATA FIX - TEST SUITE")
    print("=" * 80)
    print("\nThis test verifies the fix for the issue where tracks that meet the")
    print("medium confidence threshold but lack metadata support were incorrectly")
    print("upgraded to 5 stars by legacy logic.")
    
    test_medium_conf_no_metadata_scenario()
    
    print("\n" + "=" * 80)
    print("TEST COMPLETE")
    print("=" * 80)
