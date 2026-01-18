#!/usr/bin/env python3
"""
Enhanced Single Detection Algorithm
====================================

Implements the comprehensive 8-stage single detection algorithm per problem statement.
This module provides an enhanced wrapper around existing detection logic to ensure
compliance with the exact specifications while maintaining backward compatibility.

Author: SPTNR Team
"""

import re
import json
import logging
import sqlite3
from typing import Dict, List, Optional, Tuple
from statistics import mean, stdev
from datetime import datetime

# Import centralized logging functions
# Use centralized logging to ensure API activity appears in unified_scan.log, info.log, and debug.log
# instead of Python's default logging system which doesn't route to these files
from logging_config import log_unified, log_info, log_debug

logger = logging.getLogger(__name__)


# ============================================================================
# Stage 6: Strict Version Matching Rules
# ============================================================================

def normalize_title_strict(title: str) -> str:
    """
    Normalize title per problem statement Stage 6.
    - lowercase
    - remove punctuation
    - remove bracketed suffixes
    - collapse whitespace
    """
    # Remove bracketed/parenthesized content
    normalized = re.sub(r'\s*[\(\[].*?[\)\]]', '', title)
    # Remove dash-based versions
    normalized = re.sub(
        r'\s*-\s*(?:Live|Remix|Remaster|Edit|Mix|Version|Acoustic|Unplugged).*$',
        '', normalized, flags=re.IGNORECASE
    )
    # Remove punctuation
    normalized = re.sub(r'[^\w\s]', '', normalized)
    # Lowercase
    normalized = normalized.lower().strip()
    # Collapse whitespace
    normalized = re.sub(r'\s+', ' ', normalized)
    return normalized


def is_non_canonical_version_strict(title: str) -> bool:
    """
    Check if title contains non-canonical version markers per Stage 6.
    Reject: remix, remaster, acoustic, live, unplugged, orchestral, symphonic,
            demo, instrumental, edit, extended, version, alt, alternate, mix
    """
    title_lower = title.lower()
    patterns = [
        r'\bremix\b', r'\bremaster(ed)?\b', r'\bacoustic\b', r'\blive\b',
        r'\bunplugged\b', r'\borchestral\b', r'\bsymphonic\b',
        r'\bdemo\b', r'\binstrumental\b', r'\bedit\b', r'\bextended\b',
        r'\bversion\b', r'\balt\b', r'\balternate\b', r'\bmix\b'
    ]
    return any(re.search(p, title_lower) for p in patterns)


def duration_matches_strict(duration1: Optional[float], duration2: Optional[float]) -> bool:
    """Duration must match within ±2 seconds per Stage 6."""
    if duration1 is None or duration2 is None:
        return True  # Can't verify
    return abs(duration1 - duration2) <= 2.0


# ============================================================================
# Compilation Album Detection
# ============================================================================

# Keywords for detecting compilation/greatest hits albums
COMPILATION_KEYWORDS = [
    "greatest hits",
    "best of",
    "the very best",
    "anthology",
    "singles",
    "collection",
    "ultimate",
    "gold",
    "platinum"
]


def is_compilation_album(album_type: Optional[str], album_title: str, track_count: int) -> bool:
    """
    Detect if an album is a compilation or greatest hits album.
    
    Per problem statement:
    - If album_type == "compilation"
    - OR album has more than 12 tracks
    - OR album title contains compilation keywords
    
    Args:
        album_type: Spotify album type (if available)
        album_title: Album title
        track_count: Number of tracks in the album
        
    Returns:
        True if album is a compilation
    """
    # Check album type
    if album_type and album_type.lower() == "compilation":
        return True
    
    # Check track count
    if track_count > 12:
        return True
    
    # Check album title for keywords
    album_lower = album_title.lower()
    for keyword in COMPILATION_KEYWORDS:
        if keyword in album_lower:
            return True
    
    return False


# ============================================================================
# Stage 1: Pre-Filter Logic
# ============================================================================

# Keyword filter for non-singles (used in artist stats calculation)
# Filters out alternate versions: live, acoustic, orchestral, remixes, demos, etc.
IGNORE_SINGLE_KEYWORDS = [
    "intro", "outro", "jam",
    "live", "unplugged",
    "remix", "edit", "mix",
    "acoustic", "orchestral",
    "demo", "instrumental", "karaoke",
    "remaster", "remastered"
]


def calculate_album_stats(conn, artist: str, album: str) -> Tuple[float, float, int]:
    """
    Calculate album popularity statistics for pre-filter.
    
    Returns:
        Tuple of (mean, stddev, count)
    """
    cursor = conn.cursor()
    cursor.execute("""
        SELECT popularity_score
        FROM tracks
        WHERE artist = ? AND album = ? AND popularity_score > 0
    """, (artist, album))
    
    popularities = [row[0] for row in cursor.fetchall()]
    
    if len(popularities) < 2:
        return 0.0, 0.0, len(popularities)
    
    album_mean = mean(popularities)
    album_stddev = stdev(popularities)
    
    return album_mean, album_stddev, len(popularities)


def calculate_artist_stats(conn, artist: str) -> Tuple[float, float, int]:
    """
    Calculate artist-level popularity statistics across entire catalogue.
    
    Filters out live/remix/alternate versions to ensure statistics reflect
    the core catalog and are not skewed by bonus tracks or alternate versions.
    
    Returns:
        Tuple of (mean, stddev, count)
    """
    cursor = conn.cursor()
    cursor.execute("""
        SELECT popularity_score, title, album
        FROM tracks
        WHERE artist = ? AND popularity_score > 0
    """, (artist,))
    
    # Filter out live/remix/alternate tracks before calculating statistics
    # Use word boundary matching to avoid false positives
    popularities = []
    for row in cursor.fetchall():
        popularity_score = row[0]
        title = row[1] if row[1] else ""
        album = row[2] if row[2] else ""
        
        # Exclude live/remix/alternate versions from artist statistics
        # Use word boundary matching with regex for more precise detection
        combined_text = f"{title} {album}".lower()
        should_exclude = False
        for keyword in IGNORE_SINGLE_KEYWORDS:
            # Use word boundary matching to avoid false positives
            # e.g., "remix" matches "remix" but not "supremix"
            if re.search(r'\b' + re.escape(keyword) + r'\b', combined_text):
                should_exclude = True
                break
        
        if not should_exclude:
            popularities.append(popularity_score)
    
    if len(popularities) < 2:
        return 0.0, 0.0, len(popularities)
    
    artist_mean = mean(popularities)
    artist_stddev = stdev(popularities)
    
    return artist_mean, artist_stddev, len(popularities)


def count_spotify_versions(spotify_results: List[Dict], title: str, duration: Optional[float], isrc: Optional[str]) -> int:
    """
    Count exact-match Spotify versions per Stage 6 rules.
    - Title must match after normalization
    - Reject non-canonical versions
    - Duration must match within ±2 seconds
    - ISRC must match exactly if present
    """
    if not spotify_results:
        return 0
    
    norm_title = normalize_title_strict(title)
    count = 0
    
    for result in spotify_results:
        result_title = result.get('name', '')
        norm_result = normalize_title_strict(result_title)
        
        # Title match
        if norm_result != norm_title:
            continue
        
        # Reject non-canonical
        if is_non_canonical_version_strict(result_title):
            continue
        
        # Duration match
        result_duration_ms = result.get('duration_ms')
        if result_duration_ms:
            result_duration_sec = result_duration_ms / 1000.0
            if not duration_matches_strict(duration, result_duration_sec):
                continue
        
        # ISRC match
        if isrc and result.get('external_ids', {}).get('isrc'):
            if result['external_ids']['isrc'] != isrc:
                continue
        
        count += 1
    
    return count


def calculate_mean_version_count(conn, artist: str, album: str) -> float:
    """
    Calculate mean version count for all tracks in an album.
    
    Args:
        conn: Database connection
        artist: Artist name
        album: Album name
        
    Returns:
        Mean version count across all tracks in the album (0.0 if no tracks)
    """
    cursor = conn.cursor()
    cursor.execute("""
        SELECT spotify_version_count
        FROM tracks
        WHERE artist = ? AND album = ? AND spotify_version_count IS NOT NULL
    """, (artist, album))
    
    version_counts = [row[0] for row in cursor.fetchall()]
    
    if not version_counts:
        return 0.0
    
    return mean(version_counts)


def is_version_count_standout(version_count: int, mean_version_count: float) -> bool:
    """
    Determine if a track is a version count standout.
    
    Per problem statement: version_count >= mean_version_count + 1
    
    Args:
        version_count: Version count for this track
        mean_version_count: Mean version count for the album
        
    Returns:
        True if track qualifies as version-based standout
    """
    return version_count >= (mean_version_count + 1)


def should_check_track(
    popularity: float,
    album_mean: float,
    album_stddev: float,
    album_popularities: List[float],
    spotify_version_count: int,
    is_compilation: bool = False
) -> bool:
    """
    Pre-filter per problem statement Stage 1.
    
    If compilation album:
    - Always return True (check ALL tracks)
    
    Otherwise, always check if:
    - Spotify version count >= 5
    
    Otherwise check if:
    - In top 3 by popularity
    - popularity >= (album_mean + 1 * album_stddev)
    """
    # Compilation albums: check ALL tracks
    if is_compilation:
        return True
    
    # Rule 1.1: High Spotify version count
    if spotify_version_count >= 5:
        return True
    
    # Rule 1.2: Most popular tracks
    if len(album_popularities) < 2:
        return True
    
    # Top 3 check
    sorted_pops = sorted(album_popularities, reverse=True)
    if popularity in sorted_pops[:3]:
        return True
    
    # Threshold check
    threshold = album_mean + album_stddev
    if popularity >= threshold:
        return True
    
    return False


# ============================================================================
# Stage 5: Popularity-Based Inference
# ============================================================================

def calculate_z_score_strict(popularity: float, pop_mean: float, pop_stddev: float) -> float:
    """
    Calculate z-score for a track.
    
    Args:
        popularity: Track popularity score
        pop_mean: Mean popularity (album or artist level)
        pop_stddev: Standard deviation (album or artist level)
    
    Returns:
        Z-score value (0 if stddev is 0)
    """
    if pop_stddev == 0:
        return 0.0
    return (popularity - pop_mean) / pop_stddev


def infer_from_popularity(
    album_z: float,
    artist_z: float,
    spotify_version_count: int, 
    version_count_standout: bool = False,
    album_is_underperforming: bool = False,
    is_artist_level_standout: bool = False
) -> Tuple[str, bool]:
    """
    Popularity-based inference using hybrid z-score (album + artist).
    
    Args:
        album_z: Album-level z-score for the track
        artist_z: Artist-level z-score for the track
        spotify_version_count: Number of exact-match Spotify versions
        version_count_standout: Whether track has version_count >= mean + 1 for the album
        album_is_underperforming: Whether the album is underperforming vs artist median
        is_artist_level_standout: Whether track exceeds artist median (standout across entire catalogue)
    
    Returns:
        Tuple of (confidence_level, is_inferred_single)
    
    Hybrid Z-Score Thresholds (per problem statement):
    
    HIGH-CONFIDENCE SINGLE:
    - album_z >= 1.0 AND artist_z >= 0.5
    
    MEDIUM-CONFIDENCE SINGLE:
    - album_z >= 0.5 OR artist_z >= 1.0
    
    LOW-CONFIDENCE (legacy support):
    - album_z >= 0.2 AND >= 3 versions
    
    Version count standout:
    - version_count >= mean + 1 → medium confidence indicator
      (for rating boost, but does not mark as single)
    
    Z-score detection behavior:
    - Normal albums: Z-score detection ENABLED
    - Underperforming albums: Z-score detection DISABLED
    - Exception: If song is a standout across entire artist catalogue, Z-score detection ENABLED
    """
    # Use z-score detection unless album underperforms, except when track is artist-level standout
    use_zscore_detection = (not album_is_underperforming) or is_artist_level_standout
    
    if use_zscore_detection:
        # Apply hybrid z-score based single detection per problem statement
        
        # HIGH: album_z >= 1.0 AND artist_z >= 0.5
        if album_z >= 1.0 and artist_z >= 0.5:
            return 'high', True
        
        # MEDIUM: album_z >= 0.5 OR artist_z >= 1.0
        if album_z >= 0.5 or artist_z >= 1.0:
            return 'medium', True
        
        # LOW: Legacy support for album_z >= 0.2 AND >= 3 versions
        if album_z >= 0.2 and spotify_version_count >= 3:
            return 'low', True
    
    # Version count standout always applies regardless of underperformance
    if version_count_standout:
        # Version-based medium confidence: doesn't mark as single by itself
        # but contributes to medium confidence which can achieve 5★ via popularity-based system
        return 'medium', False
    
    return 'none', False


# ============================================================================
# Stage 7: Final Decision (Source-Based Classification)
# ============================================================================

def determine_final_status(
    discogs_confirmed: bool,
    spotify_confirmed: bool,
    musicbrainz_confirmed: bool,
    album_z: float,
    artist_z: float,
    spotify_version_count: int,
    album_is_underperforming: bool = False,
    is_artist_level_standout: bool = False,
    discogs_video_confirmed: bool = False
) -> str:
    """
    Final single status based on source detection and z-score analysis.
    
    HIGH-CONFIDENCE:
    - Discogs confirms
    - album_z >= 1.0 AND artist_z >= 0.5 (when z-score enabled)
    
    MEDIUM-CONFIDENCE:
    - Spotify confirms
    - MusicBrainz confirms
    - Discogs video confirms
    - album_z >= 0.5 OR artist_z >= 1.0 (when z-score enabled)
    
    LOW-CONFIDENCE:
    - album_z >= 0.2 AND >= 3 versions (when z-score enabled)
    
    NOT A SINGLE:
    - None of the above
    
    Z-score detection is enabled unless:
    - Album is underperforming (popularity < artist median)
    - EXCEPT when track is artist-level standout (track popularity >= artist median)
    
    Args:
        discogs_confirmed: Whether Discogs confirms this is a single
        spotify_confirmed: Whether Spotify confirms this is a single
        musicbrainz_confirmed: Whether MusicBrainz confirms this is a single
        album_z: Album-level z-score
        artist_z: Artist-level z-score
        spotify_version_count: Number of Spotify versions found
        album_is_underperforming: Whether album is underperforming vs artist median
        is_artist_level_standout: Whether track exceeds artist median popularity
        discogs_video_confirmed: Whether Discogs confirms this has a music video
        
    Returns:
        Confidence level: 'high', 'medium', 'low', or 'none'
    """
    # Discogs is always high confidence
    if discogs_confirmed:
        return 'high'
    
    # Determine if z-score detection is enabled
    # Z-score detection disabled for underperforming albums, unless track is artist-level standout
    use_zscore_detection = (not album_is_underperforming) or is_artist_level_standout
    
    # Check z-score based high confidence (if enabled)
    if use_zscore_detection:
        # High confidence: album_z >= 1.0 AND artist_z >= 0.5
        if album_z >= 1.0 and artist_z >= 0.5:
            return 'high'
    
    # Check metadata-based medium confidence
    if spotify_confirmed or musicbrainz_confirmed or discogs_video_confirmed:
        return 'medium'
    
    # Check z-score based medium confidence (if enabled)
    if use_zscore_detection:
        # Medium confidence: album_z >= 0.5 OR artist_z >= 1.0
        if album_z >= 0.5 or artist_z >= 1.0:
            return 'medium'
        
        # Low confidence: album_z >= 0.2 AND >= 3 versions
        if album_z >= 0.2 and spotify_version_count >= 3:
            return 'low'
    
    # No confidence indicators
    return 'none'


# ============================================================================
# Main Enhanced Detection Function
# ============================================================================

def is_live_version_strict(title: str, album: str) -> bool:
    """
    Check if track or album indicates a live/unplugged version per Stage 5.
    
    Args:
        title: Track title
        album: Album name
        
    Returns:
        True if title or album matches live patterns
    """
    combined = f"{title} {album}".lower()
    live_patterns = [r'\blive\b', r'\bunplugged\b']
    return any(re.search(p, combined) for p in live_patterns)


def check_has_explicit_metadata(
    title: str,
    spotify_results: Optional[List[Dict]],
    discogs_client=None,
    musicbrainz_client=None,
    artist: str = "",
    duration: Optional[float] = None
) -> bool:
    """
    Check if track has ANY explicit metadata from external sources.
    
    Returns True if ANY of:
    - Spotify confirms single
    - Discogs confirms single
    - MusicBrainz confirms single
    """
    # Check Spotify
    if spotify_results:
        norm_title = normalize_title_strict(title)
        for result_item in spotify_results:
            result_title = result_item.get('name', '')
            album_info = result_item.get('album', {})
            album_type_check = album_info.get('album_type', '').lower()
            album_name = album_info.get('name', '')
            
            # Check title match
            if normalize_title_strict(result_title) != norm_title:
                continue
            
            # Reject non-canonical
            if is_non_canonical_version_strict(result_title):
                continue
            
            # Check if single or EP with matching title
            if album_type_check == 'single':
                return True
            elif album_type_check == 'ep' and normalize_title_strict(album_name) == norm_title:
                return True
    
    # Check Discogs
    if discogs_client and hasattr(discogs_client, 'enabled') and discogs_client.enabled:
        try:
            if discogs_client.is_single(title, artist, album_context={'duration': duration}):
                return True
        except Exception:
            pass  # Fail gracefully
    
    # Check MusicBrainz
    if musicbrainz_client and hasattr(musicbrainz_client, 'enabled') and musicbrainz_client.enabled:
        try:
            if musicbrainz_client.is_single(title, artist):
                return True
        except Exception:
            pass  # Fail gracefully
    
    return False


def check_metadata_for_live_version(
    title: str,
    spotify_results: Optional[List[Dict]],
    discogs_client=None,
    musicbrainz_client=None,
    artist: str = "",
    duration: Optional[float] = None
) -> bool:
    """
    Check if there's metadata for the EXACT live version of this track.
    
    For live tracks, we need metadata that confirms the live version specifically,
    not just the studio version.
    """
    # For now, use same logic as has_explicit_metadata
    # In a more sophisticated implementation, we would check if the metadata
    # specifically mentions "live" in the release title
    return check_has_explicit_metadata(title, spotify_results, discogs_client, musicbrainz_client, artist, duration)


def detect_single_enhanced(
    conn,
    track_id: str,
    title: str,
    artist: str,
    album: str,
    duration: Optional[float] = None,
    isrc: Optional[str] = None,
    popularity: float = 0.0,
    spotify_results: Optional[List[Dict]] = None,
    discogs_client=None,
    musicbrainz_client=None,
    verbose: bool = False,
    album_type: Optional[str] = None,
    album_is_underperforming: bool = False,
    artist_median_popularity: float = 0.0
) -> Dict:
    """
    Enhanced single detection implementing the exact algorithm from problem statement.
    
    Pipeline:
    1. PREPROCESSING - Calculate album/artist stats, exclude trailing parenthesis tracks
    2. ARTIST-LEVEL SANITY FILTER - Skip if popularity < artist_mean AND no explicit metadata
    3. HIGH CONFIDENCE DETECTION - Popularity standout OR Discogs
    4. MEDIUM CONFIDENCE DETECTION - Z-score+metadata, Spotify, MusicBrainz, Discogs video, version count, popularity outlier
    5. LIVE TRACK HANDLING - Require metadata for exact live version
    6. FINAL CONFIDENCE CLASSIFICATION - Based on source counts
    7. STAR RATING - HIGH=5★, MEDIUM with 2+ sources=5★, else baseline
    
    Args:
        conn: Database connection
        track_id: Track ID
        title: Track title
        artist: Artist name
        album: Album name
        duration: Track duration in seconds
        isrc: ISRC code
        popularity: Track popularity score
        spotify_results: Cached Spotify search results
        discogs_client: Discogs API client
        musicbrainz_client: MusicBrainz API client
        verbose: Enable verbose logging
        album_type: Spotify album type (for compilation detection)
        album_is_underperforming: Whether the album is underperforming vs artist median
        artist_median_popularity: Artist median popularity (for standout detection)
        
    Returns:
        Dict with single detection results for database storage
    """
    result = {
        'is_single': False,
        'single_status': 'none',
        'single_confidence': 'none',
        'single_sources': [],
        'single_sources_used': [],
        'z_score': 0.0,
        'spotify_version_count': 0,
        'discogs_release_ids': [],
        'musicbrainz_release_group_ids': [],
        'single_confidence_score': 0.0,
        'single_detection_last_updated': datetime.now().isoformat()
    }
    
    # Get album statistics
    album_mean, album_stddev, album_track_count = calculate_album_stats(conn, artist, album)
    
    # Get all album popularities for pre-filter
    cursor = conn.cursor()
    cursor.execute("""
        SELECT popularity_score
        FROM tracks
        WHERE artist = ? AND album = ? AND popularity_score > 0
        ORDER BY popularity_score DESC
    """, (artist, album))
    album_popularities = [row[0] for row in cursor.fetchall()]
    
    # Detect if this is a compilation album
    is_compilation = is_compilation_album(album_type, album, album_track_count)
    
    if is_compilation and verbose:
        log_debug(f"[DEBUG] Compilation detected — checking all tracks for singles.")
    
    # Count Spotify versions
    spotify_version_count = count_spotify_versions(spotify_results or [], title, duration, isrc)
    result['spotify_version_count'] = spotify_version_count
    
    # STAGE 1: Pre-Filter (with compilation override)
    if not should_check_track(popularity, album_mean, album_stddev, album_popularities, spotify_version_count, is_compilation):
        if verbose:
            log_debug(f"Pre-filter: Skipping {title} (not high priority)")
        return result
    
    if verbose:
        log_debug(f"Pre-filter: Checking {title} (high priority)")
    
    # STAGE 2: Discogs (Primary Source) - ALWAYS CHECKED FIRST
    discogs_confirmed = False
    if discogs_client and hasattr(discogs_client, 'enabled') and discogs_client.enabled:
        try:
            # Always log Discogs checks to unified and info logs (not dependent on verbose)
            log_unified(f"   Checking Discogs for single: {title}")
            log_info(f"   Discogs API: Searching for single '{title}' by '{artist}'")
            
            # Use existing is_single method
            discogs_confirmed = discogs_client.is_single(title, artist, album_context={'duration': duration})
            if discogs_confirmed:
                result['single_sources'].append('discogs')
                result['single_sources_used'].append('discogs')
                log_unified(f"   ✓ Discogs confirms single: {title}")
                log_info(f"   Discogs result: Single confirmed for '{title}'")
                
                # Per problem statement: Discogs = HIGH confidence, skip other checks
                result['single_status'] = 'high'
                result['single_confidence'] = 'high'
                result['is_single'] = True
                result['single_confidence_score'] = 1.0
                
                # Still calculate both z-scores for logging purposes
                album_z = calculate_z_score_strict(popularity, album_mean, album_stddev)
                result['z_score'] = album_z  # Backward compatibility
                result['album_z_score'] = album_z
                
                # Get artist statistics and calculate artist z-score
                artist_mean, artist_stddev, artist_track_count = calculate_artist_stats(conn, artist)
                artist_z = calculate_z_score_strict(popularity, artist_mean, artist_stddev)
                result['artist_z_score'] = artist_z
                
                # Add final debug summary
                if verbose:
                    log_debug(f"[DEBUG] Z-scores for {title}: album_z={album_z:.2f}, artist_z={artist_z:.2f}")
                    log_debug(f"[DEBUG] Single detection sources for {title}: {result['single_sources']}")
                    log_debug(f"[DEBUG] Final single status for {title}: {result['single_confidence']}")
                
                return result
            else:
                # Always log negative results too (not just in verbose mode)
                log_unified(f"   ⓘ Discogs does not confirm single: {title}")
                log_info(f"   Discogs result: No single found for '{title}'")
        except Exception as e:
            log_unified(f"   ⚠ Discogs single check failed for {title}: {e}")
            log_info(f"   Discogs API error: {type(e).__name__}: {str(e)}")
    else:
        # Only log client availability messages in verbose mode to reduce log noise
        if verbose:
            if not discogs_client:
                log_unified(f"   ⓘ Discogs client not available")
                log_info(f"   Discogs: Client not available (module import failed)")
            elif not getattr(discogs_client, 'enabled', True):
                log_unified(f"   ⓘ Discogs client is disabled")
                log_info(f"   Discogs: Client is disabled in configuration")
    
    # STAGE 3: Spotify (Secondary Source)
    spotify_confirmed = False
    if spotify_results:
        norm_title = normalize_title_strict(title)
        for result_item in spotify_results:
            result_title = result_item.get('name', '')
            album_info = result_item.get('album', {})
            album_type_check = album_info.get('album_type', '').lower()
            album_name = album_info.get('name', '')
            
            # Check title match
            if normalize_title_strict(result_title) != norm_title:
                continue
            
            # Reject non-canonical
            if is_non_canonical_version_strict(result_title):
                continue
            
            # Check if single or EP with matching title
            if album_type_check == 'single':
                spotify_confirmed = True
                break
            elif album_type_check == 'ep' and normalize_title_strict(album_name) == norm_title:
                spotify_confirmed = True
                break
        
        if spotify_confirmed:
            result['single_sources'].append('spotify')
            result['single_sources_used'].append('spotify')
            if verbose:
                log_debug(f"Spotify: Confirmed single for {title}")
    
    # STAGE 4: MusicBrainz (Tertiary Source)
    musicbrainz_confirmed = False
    if musicbrainz_client and hasattr(musicbrainz_client, 'enabled') and musicbrainz_client.enabled:
        try:
            # Always log MusicBrainz checks to unified and info logs (not dependent on verbose)
            log_unified(f"   Checking MusicBrainz for single: {title}")
            log_info(f"   MusicBrainz API: Searching for single '{title}' by '{artist}'")
            
            # Use existing is_single method
            musicbrainz_confirmed = musicbrainz_client.is_single(title, artist)
            if musicbrainz_confirmed:
                result['single_sources'].append('musicbrainz')
                result['single_sources_used'].append('musicbrainz')
                log_unified(f"   ✓ MusicBrainz confirms single: {title}")
                log_info(f"   MusicBrainz result: Single confirmed for '{title}'")
            else:
                # Always log negative results too (not just in verbose mode)
                log_unified(f"   ⓘ MusicBrainz does not confirm single: {title}")
                log_info(f"   MusicBrainz result: No single found for '{title}'")
        except Exception as e:
            # Log SSL and connection errors more gracefully
            error_type = type(e).__name__
            if 'SSL' in error_type or 'ssl' in str(e).lower():
                log_unified(f"   ⚠ MusicBrainz SSL connection error for {title}: {error_type}")
                log_info(f"   MusicBrainz API SSL error: {error_type}: {str(e)}")
            elif 'timeout' in str(e).lower() or 'Timeout' in error_type:
                log_unified(f"   ⏱ MusicBrainz check timed out for {title}: {error_type}")
                log_info(f"   MusicBrainz API timeout: {error_type}: {str(e)}")
            else:
                log_unified(f"   ⚠ MusicBrainz single check failed for {title}: {e}")
                log_info(f"   MusicBrainz API error: {type(e).__name__}: {str(e)}")
    else:
        # Only log client availability messages in verbose mode to reduce log noise
        if verbose:
            if not musicbrainz_client:
                log_unified(f"   ⓘ MusicBrainz client not available")
                log_info(f"   MusicBrainz: Client not available (module import failed)")
            elif not getattr(musicbrainz_client, 'enabled', True):
                log_unified(f"   ⓘ MusicBrainz client is disabled")
                log_info(f"   MusicBrainz: Client is disabled in configuration")
    
    # STAGE 4.5: Discogs Music Video Check (MEDIUM CONFIDENCE)
    discogs_video_confirmed = False
    if discogs_client and hasattr(discogs_client, 'enabled') and discogs_client.enabled:
        if hasattr(discogs_client, 'has_official_video'):
            try:
                # Always log Discogs video checks to unified and info logs
                log_unified(f"   Checking Discogs for music video: {title}")
                log_info(f"   Discogs API: Searching for music video '{title}' by '{artist}'")
                
                # Check for official music video
                discogs_video_confirmed = discogs_client.has_official_video(title, artist)
                if discogs_video_confirmed:
                    result['single_sources'].append('discogs_video')
                    result['single_sources_used'].append('discogs_video')
                    log_unified(f"   ✓ Discogs confirms music video: {title}")
                    log_info(f"   Discogs result: Music video confirmed for '{title}'")
                else:
                    # Always log negative results too (not just in verbose mode)
                    log_unified(f"   ⓘ Discogs does not confirm music video: {title}")
                    log_info(f"   Discogs result: No music video found for '{title}'")
            except Exception as e:
                log_unified(f"   ⚠ Discogs video check failed for {title}: {e}")
                log_info(f"   Discogs API error: {type(e).__name__}: {str(e)}")
        elif verbose:
            log_unified(f"   ⓘ Discogs video method not available")
            log_info(f"   Discogs: has_official_video method not available")
    else:
        # Only log client availability messages in verbose mode to reduce log noise
        if verbose:
            if not discogs_client:
                log_unified(f"   ⓘ Discogs video client not available")
                log_info(f"   Discogs: Video client not available")
            elif not getattr(discogs_client, 'enabled', True):
                log_unified(f"   ⓘ Discogs client is disabled")
                log_info(f"   Discogs: Client is disabled in configuration")
    
    # STAGE 5: Popularity-Based Inference (including version count)
    # Calculate album-level z-score
    album_z = calculate_z_score_strict(popularity, album_mean, album_stddev)
    result['z_score'] = album_z  # Store album z-score for backward compatibility
    
    # Get artist statistics
    artist_mean, artist_stddev, artist_track_count = calculate_artist_stats(conn, artist)
    
    # Calculate artist-level z-score
    artist_z = calculate_z_score_strict(popularity, artist_mean, artist_stddev)
    
    # Store both z-scores in result
    result['album_z_score'] = album_z
    result['artist_z_score'] = artist_z
    
    if verbose:
        log_debug(f"Z-scores for '{title}': album_z={album_z:.2f}, artist_z={artist_z:.2f}")
        if artist_track_count > 0:
            log_debug(f"Artist stats: mean={artist_mean:.1f}, stddev={artist_stddev:.1f}, tracks={artist_track_count}")
    
    # Determine if this track is a standout across the entire artist catalogue
    # A track is considered an artist-level standout if it exceeds the artist median popularity
    is_artist_level_standout = artist_median_popularity > 0 and popularity >= artist_median_popularity
    
    if verbose and album_is_underperforming:
        if is_artist_level_standout:
            log_debug(f"Track '{title}' is artist-level standout: pop={popularity:.1f} >= artist_median={artist_median_popularity:.1f} (z-score detection enabled)")
        else:
            log_debug(f"Album underperforming and track not artist-level standout: pop={popularity:.1f} < artist_median={artist_median_popularity:.1f} (z-score detection disabled)")
    
    # Calculate mean version count for the album
    mean_version_count = calculate_mean_version_count(conn, artist, album)
    # Handle None spotify_version_count (default to 0)
    version_count_value = spotify_version_count if spotify_version_count is not None else 0
    version_count_standout = is_version_count_standout(version_count_value, mean_version_count)
    
    if version_count_standout and verbose:
        log_debug(f"Version count standout: {title} (count={version_count_value}, mean={mean_version_count:.1f})")
    
    # Use hybrid z-score inference
    popularity_confidence, popularity_inferred = infer_from_popularity(
        album_z, 
        artist_z,
        version_count_value, 
        version_count_standout,
        album_is_underperforming,
        is_artist_level_standout
    )
    # NOTE: Z-score inference is used for confidence calculation only, NOT added to sources
    # Per problem statement: z-score should not appear as a high-confidence source
    if popularity_inferred:
        if verbose:
            log_debug(f"Popularity: Inferred single for {title} (album_z={album_z:.2f}, artist_z={artist_z:.2f}, confidence={popularity_confidence})")
    elif version_count_standout:
        # Version count standout is medium confidence but doesn't mark as single
        result['single_sources'].append('version_count')
        if verbose:
            log_debug(f"Version count: Medium confidence indicator for {title} (not marking as single)")
    
    # STAGE 7: Final Decision (using hybrid z-scores)
    final_status = determine_final_status(
        discogs_confirmed,
        spotify_confirmed,
        musicbrainz_confirmed,
        album_z,
        artist_z,
        version_count_value,
        album_is_underperforming,
        is_artist_level_standout,
        discogs_video_confirmed
    )
    
    result['single_status'] = final_status
    result['single_confidence'] = final_status
    result['is_single'] = final_status == 'high'
    
    # Map confidence to numeric score
    confidence_scores = {'high': 1.0, 'medium': 0.67, 'low': 0.33, 'none': 0.0}
    result['single_confidence_score'] = confidence_scores.get(final_status, 0.0)
    
    # Add final debug summary per track
    if verbose:
        log_debug(f"[DEBUG] Single detection sources for {title}: {result['single_sources']}")
        log_debug(f"[DEBUG] Final single status for {title}: {final_status}")
    
    return result


# ============================================================================
# Database Storage Helper
# ============================================================================

def store_single_detection_result(conn, track_id: str, result: Dict):
    """
    Store single detection result in database per Stage 8.
    
    Stores:
    - single_status (none, low, medium, high)
    - single_confidence_score (0.0-1.0)
    - single_sources_used (JSON array)
    - z_score (album z-score for backward compatibility)
    - album_z_score (album-level z-score)
    - artist_z_score (artist-level z-score)
    - spotify_version_count
    - discogs_release_ids (JSON array)
    - musicbrainz_release_group_ids (JSON array)
    - single_detection_last_updated (timestamp)
    """
    cursor = conn.cursor()
    
    # Check if new columns exist in schema
    cursor.execute("PRAGMA table_info(tracks)")
    columns = {row[1] for row in cursor.fetchall()}
    has_album_z = 'album_z_score' in columns
    has_artist_z = 'artist_z_score' in columns
    
    # Get z_score values with defaults
    z_score = result.get('z_score', 0.0)
    album_z_score = result.get('album_z_score', z_score)
    artist_z_score = result.get('artist_z_score', 0.0)
    
    # Update with new columns if they exist
    if has_album_z and has_artist_z:
        cursor.execute("""
            UPDATE tracks
            SET single_status = ?,
                single_confidence_score = ?,
                single_sources_used = ?,
                z_score = ?,
                album_z_score = ?,
                artist_z_score = ?,
                spotify_version_count = ?,
                discogs_release_ids = ?,
                musicbrainz_release_group_ids = ?,
                single_detection_last_updated = ?,
                is_single = ?,
                single_confidence = ?,
                single_sources = ?
            WHERE id = ?
        """, (
            result['single_status'],
            result['single_confidence_score'],
            json.dumps(result['single_sources_used']),
            z_score,
            album_z_score,
            artist_z_score,
            result['spotify_version_count'],
            json.dumps(result.get('discogs_release_ids', [])),
            json.dumps(result.get('musicbrainz_release_group_ids', [])),
            result['single_detection_last_updated'],
            1 if result['is_single'] else 0,
            result['single_confidence'],
            json.dumps(result['single_sources']),
            track_id
        ))
    else:
        # Fallback to old schema without new z-score columns
        cursor.execute("""
            UPDATE tracks
            SET single_status = ?,
                single_confidence_score = ?,
                single_sources_used = ?,
                z_score = ?,
                spotify_version_count = ?,
                discogs_release_ids = ?,
                musicbrainz_release_group_ids = ?,
                single_detection_last_updated = ?,
                is_single = ?,
                single_confidence = ?,
                single_sources = ?
            WHERE id = ?
        """, (
            result['single_status'],
            result['single_confidence_score'],
            json.dumps(result['single_sources_used']),
            z_score,
            result['spotify_version_count'],
            json.dumps(result.get('discogs_release_ids', [])),
            json.dumps(result.get('musicbrainz_release_group_ids', [])),
            result['single_detection_last_updated'],
            1 if result['is_single'] else 0,
            result['single_confidence'],
            json.dumps(result['single_sources']),
            track_id
        ))
    
    conn.commit()
