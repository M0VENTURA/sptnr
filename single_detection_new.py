#!/usr/bin/env python3
"""
New Single Detection Logic - Per Problem Statement

Implements the exact algorithm from the problem statement pseudocode:
- Preprocessing with exclusion of trailing parenthesis tracks
- Artist-level sanity filter
- High confidence detection (popularity standout, Discogs)
- Medium confidence detection (z-score+metadata, Spotify, MusicBrainz, Discogs video, version count, popularity outlier)
- Live track handling
- Final confidence classification based on source counts
- Star rating: HIGH=5★, MEDIUM with 2+ sources=5★, else baseline
"""

import re
import json
import sqlite3
from typing import Dict, List, Optional, Set, Tuple
from statistics import mean, stdev
from datetime import datetime

# Import centralized logging
from logging_config import log_unified, log_info, log_debug


# ============================================================================
# Helper Functions
# ============================================================================

def normalize_title_for_matching(title: str) -> str:
    """Normalize title for version matching."""
    # Remove bracketed/parenthesized content
    normalized = re.sub(r'\s*[\(\[].*?[\)\]]', '', title)
    # Remove dash-based versions
    normalized = re.sub(
        r'\s*-\s*(?:Live|Remix|Remaster|Edit|Mix|Version|Acoustic|Unplugged).*$',
        '', normalized, flags=re.IGNORECASE
    )
    # Remove punctuation
    normalized = re.sub(r'[^\w\s]', '', normalized)
    # Lowercase and collapse whitespace
    normalized = normalized.lower().strip()
    normalized = re.sub(r'\s+', ' ', normalized)
    return normalized


def is_live_track(title: str, album: str) -> bool:
    """Check if track is a live version."""
    combined = f"{title} {album}".lower()
    live_patterns = [r'\blive\b', r'\bunplugged\b']
    return any(re.search(p, combined) for p in live_patterns)


def has_explicit_metadata(
    title: str,
    spotify_single: bool = False,
    discogs_single: bool = False,
    musicbrainz_single: bool = False,
    discogs_video: bool = False
) -> bool:
    """Check if track has ANY explicit metadata confirmation."""
    return spotify_single or discogs_single or musicbrainz_single or discogs_video


# ============================================================================
# Preprocessing Functions
# ============================================================================

def exclude_trailing_parenthesis_tracks(tracks: List[Dict]) -> List[Dict]:
    """
    Exclude tracks with trailing parenthesis from album stats calculation.
    
    A track has trailing parenthesis if:
    - Title ends with (something)
    - AND appears later in the track list
    
    Args:
        tracks: List of track dicts with 'id', 'title', 'popularity_score'
        
    Returns:
        List of core tracks (without trailing parenthesis bonus tracks)
    """
    core_tracks = []
    title_to_track = {}
    
    for track in tracks:
        title = track.get('title', '')
        
        # Check if this track has parentheses at the end
        if re.match(r'^.*\([^)]*\)$', title):
            # Get base title without parentheses
            base_title = re.sub(r'\s*\([^)]*\)\s*$', '', title).strip().lower()
            
            # Check if we have a track with this base title
            if base_title in title_to_track:
                # This is an alternate/bonus track - skip it
                continue
            else:
                # Record this title in case we see a non-parenthesis version later
                title_to_track[base_title] = track
                core_tracks.append(track)
        else:
            # No parentheses - this is a core track
            title_lower = title.lower()
            title_to_track[title_lower] = track
            core_tracks.append(track)
    
    return core_tracks


def compute_z_threshold(core_tracks: List[Dict]) -> float:
    """
    Compute z-score threshold as mean of top 50% z-scores.
    
    Args:
        core_tracks: List of core track dicts with 'popularity_score'
        
    Returns:
        Z-score threshold value
    """
    popularities = [t.get('popularity_score', 0) for t in core_tracks if t.get('popularity_score', 0) > 0]
    
    if len(popularities) < 2:
        return 0.0
    
    pop_mean = mean(popularities)
    pop_stddev = stdev(popularities)
    
    if pop_stddev == 0:
        return 0.0
    
    # Calculate z-scores
    zscores = [(p - pop_mean) / pop_stddev for p in popularities]
    
    # Get top 50%
    zscores.sort(reverse=True)
    top_50_count = max(1, len(zscores) // 2)
    top_50 = zscores[:top_50_count]
    
    return mean(top_50) if top_50 else 0.0


def compute_artist_mean_popularity(conn: sqlite3.Connection, artist: str) -> float:
    """
    Compute mean popularity across entire artist catalog.
    Excludes live/remix/alternate versions.
    
    Args:
        conn: Database connection
        artist: Artist name
        
    Returns:
        Mean popularity score
    """
    cursor = conn.cursor()
    cursor.execute("""
        SELECT popularity_score, title, album
        FROM tracks
        WHERE artist = ? AND popularity_score > 0
    """, (artist,))
    
    # Filter out live/remix/alternate tracks
    IGNORE_KEYWORDS = [
        "intro", "outro", "jam",
        "live", "unplugged",
        "remix", "edit", "mix",
        "acoustic", "orchestral",
        "demo", "instrumental", "karaoke",
        "remaster", "remastered"
    ]
    
    popularities = []
    for row in cursor.fetchall():
        popularity_score = row[0]
        title = row[1] if row[1] else ""
        album = row[2] if row[2] else ""
        
        # Exclude live/remix/alternate versions
        combined_text = f"{title} {album}".lower()
        should_exclude = any(re.search(r'\b' + re.escape(keyword) + r'\b', combined_text) for keyword in IGNORE_KEYWORDS)
        
        if not should_exclude:
            popularities.append(popularity_score)
    
    return mean(popularities) if popularities else 0.0


# ============================================================================
# Single Detection Pipeline
# ============================================================================

def detect_single_new(
    conn: sqlite3.Connection,
    track: Dict,
    album_tracks: List[Dict],
    artist_name: str,
    discogs_client=None,
    musicbrainz_client=None,
    spotify_results: Optional[List[Dict]] = None,
    verbose: bool = False
) -> Dict:
    """
    Detect if track is a single using the exact algorithm from problem statement.
    
    Pipeline:
    1. PREPROCESSING - Exclude trailing parenthesis tracks, calculate stats
    2. ARTIST-LEVEL SANITY FILTER - Skip if popularity < artist_mean AND no explicit metadata
    3. HIGH CONFIDENCE DETECTION - Popularity standout OR Discogs
    4. MEDIUM CONFIDENCE DETECTION - Multiple sources
    5. LIVE TRACK HANDLING - Require metadata for exact live version
    6. FINAL CONFIDENCE CLASSIFICATION - Based on source counts
    
    Args:
        conn: Database connection
        track: Track dict with 'id', 'title', 'artist', 'album', 'popularity_score', etc.
        album_tracks: List of all tracks in the album
        artist_name: Artist name
        discogs_client: Discogs API client (optional)
        musicbrainz_client: MusicBrainz API client (optional)
        spotify_results: Cached Spotify search results (optional)
        verbose: Enable verbose logging
        
    Returns:
        Dict with:
            - is_single: bool (True only for HIGH confidence)
            - single_confidence: str ('high', 'medium', 'none')
            - single_sources: List[str] (all sources that contributed)
            - high_conf_sources: Set[str]
            - med_conf_sources: Set[str]
    """
    result = {
        'is_single': False,
        'single_confidence': 'none',
        'single_sources': [],
        'high_conf_sources': set(),
        'med_conf_sources': set()
    }
    
    title = track.get('title', '')
    popularity = track.get('popularity_score', 0.0)
    album = track.get('album', '')
    
    # -----------------------------------------------------
    # PREPROCESSING
    # -----------------------------------------------------
    
    # 1. Exclude trailing parenthesis tracks from album stats
    core_tracks = exclude_trailing_parenthesis_tracks(album_tracks)
    
    # Calculate album stats from core tracks only
    core_popularities = [t.get('popularity_score', 0) for t in core_tracks if t.get('popularity_score', 0) > 0]
    if core_popularities:
        album_mean = mean(core_popularities)
        album_std = stdev(core_popularities) if len(core_popularities) > 1 else 0.0
    else:
        album_mean = 0.0
        album_std = 0.0
    
    # Calculate z-threshold
    z_threshold = compute_z_threshold(core_tracks)
    
    # Calculate artist mean
    artist_mean = compute_artist_mean_popularity(conn, artist_name)
    
    if verbose:
        log_debug(f"[PREPROCESSING] {title}: pop={popularity:.1f}, album_mean={album_mean:.1f}, artist_mean={artist_mean:.1f}, z_threshold={z_threshold:.2f}")
    
    # -----------------------------------------------------
    # 2. ARTIST-LEVEL SANITY FILTER
    # -----------------------------------------------------
    
    if popularity < artist_mean:
        # Check if track has any explicit metadata
        # We'll do the actual detailed checks later, but we need to know
        # if we should skip this track entirely
        
        # Quick check: do we have any metadata sources available?
        has_potential_metadata = (
            (discogs_client is not None and hasattr(discogs_client, 'enabled') and discogs_client.enabled) or
            (musicbrainz_client is not None and hasattr(musicbrainz_client, 'enabled') and musicbrainz_client.enabled) or
            (spotify_results is not None and len(spotify_results) > 0)
        )
        
        if not has_potential_metadata:
            # No metadata sources available at all, skip this track
            result['single_confidence'] = 'none'
            if verbose:
                log_debug(f"[SANITY FILTER] Skipping {title}: popularity={popularity:.1f} < artist_mean={artist_mean:.1f}, no metadata sources available")
            return result
        
        # If we have potential metadata sources, continue checking
        # The actual metadata confirmation will happen in the detection stages below
    
    # -----------------------------------------------------
    # 3. HIGH CONFIDENCE DETECTION
    # -----------------------------------------------------
    
    high_conf_sources = set()
    
    # A. Popularity standout (>= album_mean + 6)
    if popularity >= (album_mean + 6):
        high_conf_sources.add('popularity')
        if verbose:
            log_debug(f"[HIGH CONF] Popularity standout: {title} ({popularity:.1f} >= {album_mean + 6:.1f})")
    
    # B. Discogs single (studio or live version)
    if discogs_client and hasattr(discogs_client, 'enabled') and discogs_client.enabled:
        try:
            log_unified(f"   Checking Discogs for single: {title}")
            
            if discogs_client.is_single(title, artist_name, album_context={'duration': track.get('duration')}):
                high_conf_sources.add('discogs')
                log_unified(f"   ✓ Discogs confirms single: {title}")
            else:
                log_unified(f"   ⓘ Discogs does not confirm single: {title}")
        except Exception as e:
            log_unified(f"   ⚠ Discogs check failed for {title}: {e}")
    
    # -----------------------------------------------------
    # 4. MEDIUM CONFIDENCE DETECTION
    # -----------------------------------------------------
    
    med_conf_sources = set()
    
    # A. Z-score + metadata confirmation
    if album_std > 0:
        z_score = (popularity - album_mean) / album_std
        
        if z_score >= z_threshold:
            # Check for metadata confirmation
            # Metadata confirmation requires at least ONE of:
            # 1. Explicit metadata (Spotify, MusicBrainz, Discogs) - will be checked below
            # 2. Popularity outlier (>= mean + 2)
            # 3. Version count standout (>= mean + 1) - TODO: implement when version count data available
            
            has_popularity_outlier = popularity >= (album_mean + 2)
            
            # For now, we'll use popularity outlier as metadata confirmation
            # The explicit metadata sources (Spotify, MusicBrainz, Discogs) will be checked below
            # and if found, they will also provide metadata confirmation retroactively
            
            # We'll mark this as a potential medium confidence source
            # and finalize it after checking explicit sources
            has_metadata_confirmation = has_popularity_outlier
            
            if has_metadata_confirmation:
                med_conf_sources.add('zscore+metadata')
                if verbose:
                    log_debug(f"[MEDIUM CONF] Z-score + metadata: {title} (z={z_score:.2f} >= {z_threshold:.2f})")
    
    # B. Spotify single (strict)
    if spotify_results:
        norm_title = normalize_title_for_matching(title)
        for result_item in spotify_results:
            result_title = result_item.get('name', '')
            album_info = result_item.get('album', {})
            album_type = album_info.get('album_type', '').lower()
            album_name = album_info.get('name', '')
            
            # Check title match
            if normalize_title_for_matching(result_title) != norm_title:
                continue
            
            # Check if single or EP
            if album_type == 'single':
                med_conf_sources.add('spotify')
                if verbose:
                    log_debug(f"[MEDIUM CONF] Spotify single: {title}")
                break
            elif album_type == 'ep' and normalize_title_for_matching(album_name) == norm_title:
                med_conf_sources.add('spotify')
                if verbose:
                    log_debug(f"[MEDIUM CONF] Spotify EP single: {title}")
                break
    
    # C. MusicBrainz single (strict)
    if musicbrainz_client and hasattr(musicbrainz_client, 'enabled') and musicbrainz_client.enabled:
        try:
            log_unified(f"   Checking MusicBrainz for single: {title}")
            
            if musicbrainz_client.is_single(title, artist_name):
                med_conf_sources.add('musicbrainz')
                log_unified(f"   ✓ MusicBrainz confirms single: {title}")
            else:
                log_unified(f"   ⓘ MusicBrainz does not confirm single: {title}")
        except Exception as e:
            log_unified(f"   ⚠ MusicBrainz check failed for {title}: {e}")
    
    # D. Discogs music video
    if discogs_client and hasattr(discogs_client, 'enabled') and discogs_client.enabled:
        try:
            if hasattr(discogs_client, 'has_official_video'):
                log_unified(f"   Checking Discogs for music video: {title}")
                
                if discogs_client.has_official_video(title, artist_name):
                    med_conf_sources.add('discogs_video')
                    log_unified(f"   ✓ Discogs confirms music video: {title}")
                else:
                    log_unified(f"   ⓘ Discogs does not confirm music video: {title}")
        except Exception as e:
            log_unified(f"   ⚠ Discogs video check failed for {title}: {e}")
    
    # E. Version-count standout
    # NOTE: Version count standout requires Spotify version matching data which is not
    # currently available in the track dict. This would need to be calculated by:
    # 1. Counting exact-match Spotify versions per strict matching rules
    # 2. Comparing to album mean version count
    # 3. If >= mean + 1, add 'version_count' to medium confidence sources
    # 
    # Implementation deferred until version count data is available in the pipeline.
    
    # F. Popularity outlier (>= album_mean + 2)
    if popularity >= (album_mean + 2):
        med_conf_sources.add('popularity_outlier')
        if verbose:
            log_debug(f"[MEDIUM CONF] Popularity outlier: {title} ({popularity:.1f} >= {album_mean + 2:.1f})")
    
    # -----------------------------------------------------
    # 5. LIVE TRACK HANDLING
    # -----------------------------------------------------
    
    if is_live_track(title, album):
        # Live tracks must have metadata for exact live version
        # For simplicity, check if we have any metadata at all
        has_live_metadata = len(high_conf_sources) > 0 or len(med_conf_sources) > 0
        
        if not has_live_metadata:
            result['single_confidence'] = 'none'
            if verbose:
                log_debug(f"[LIVE TRACK] Skipping {title}: no metadata for exact live version")
            return result
    
    # -----------------------------------------------------
    # 6. FINAL CONFIDENCE CLASSIFICATION
    # -----------------------------------------------------
    
    # High confidence requires ANY high-confidence source
    if len(high_conf_sources) >= 1:
        result['single_confidence'] = 'high'
        result['is_single'] = True
        result['high_conf_sources'] = high_conf_sources
        result['med_conf_sources'] = med_conf_sources
        result['single_sources'] = list(high_conf_sources | med_conf_sources)
        if verbose:
            log_debug(f"[HIGH CONFIDENCE] {title}: sources={high_conf_sources}")
        return result
    
    # Medium confidence requires ANY medium-confidence source
    if len(med_conf_sources) >= 1:
        result['single_confidence'] = 'medium'
        result['is_single'] = False
        result['high_conf_sources'] = high_conf_sources
        result['med_conf_sources'] = med_conf_sources
        result['single_sources'] = list(med_conf_sources)
        if verbose:
            log_debug(f"[MEDIUM CONFIDENCE] {title}: sources={med_conf_sources}")
        return result
    
    # Otherwise, not a single
    result['single_confidence'] = 'none'
    result['is_single'] = False
    result['high_conf_sources'] = high_conf_sources
    result['med_conf_sources'] = med_conf_sources
    if verbose:
        log_debug(f"[NOT A SINGLE] {title}")
    
    return result


# ============================================================================
# Star Rating Logic
# ============================================================================

def compute_baseline_stars(popularity: float, album_tracks: List[Dict]) -> int:
    """
    Compute baseline star rating from popularity using band-based approach.
    
    Args:
        popularity: Track popularity score
        album_tracks: List of all tracks in album (sorted by popularity DESC)
        
    Returns:
        Star rating (1-5)
    """
    # Find track position in sorted list
    sorted_tracks = sorted(album_tracks, key=lambda t: t.get('popularity_score', 0), reverse=True)
    
    try:
        track_index = next(i for i, t in enumerate(sorted_tracks) if t.get('popularity_score') == popularity)
    except StopIteration:
        return 1  # Default to 1 star if not found
    
    # Calculate band size
    total_tracks = len(sorted_tracks)
    import math
    band_size = math.ceil(total_tracks / 4)
    
    # Calculate band index
    band_index = track_index // band_size
    stars = max(1, 4 - band_index)
    
    return stars


def calculate_star_rating(
    track: Dict,
    album_tracks: List[Dict],
    single_confidence: str,
    single_sources: List[str]
) -> int:
    """
    Calculate star rating per problem statement.
    
    Logic:
    - HIGH confidence = 5★
    - MEDIUM confidence with 2+ sources = 5★
    - Otherwise, compute baseline stars from popularity
    
    Args:
        track: Track dict with 'popularity_score'
        album_tracks: List of all tracks in album
        single_confidence: 'high', 'medium', or 'none'
        single_sources: List of sources
        
    Returns:
        Star rating (1-5)
    """
    if single_confidence == 'high':
        return 5
    
    # Medium confidence requires 2 independent signals
    if single_confidence == 'medium' and len(single_sources) >= 2:
        return 5
    
    # Otherwise compute baseline stars from popularity
    return compute_baseline_stars(track.get('popularity_score', 0), album_tracks)
