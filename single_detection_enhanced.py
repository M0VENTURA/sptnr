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
from typing import Dict, List, Optional, Tuple
from statistics import mean, stdev
from datetime import datetime

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

def calculate_z_score_strict(popularity: float, album_mean: float, album_stddev: float) -> float:
    """Calculate z-score per Stage 5."""
    if album_stddev == 0:
        return 0.0
    return (popularity - album_mean) / album_stddev


def infer_from_popularity(z_score: float, spotify_version_count: int) -> Tuple[str, bool]:
    """
    Popularity-based inference per Stage 5.
    
    Returns:
        Tuple of (confidence_level, is_inferred_single)
    
    Thresholds:
    - z >= 1.0 → strong single (high)
    - z >= 0.5 → likely single (medium)
    - z >= 0.2 AND >= 3 versions → weak single (low)
    """
    if z_score >= 1.0:
        return 'high', True
    elif z_score >= 0.5:
        return 'medium', True
    elif z_score >= 0.2 and spotify_version_count >= 3:
        return 'low', True
    else:
        return 'none', False


# ============================================================================
# Stage 7: Final Decision
# ============================================================================

def determine_final_status(
    discogs_confirmed: bool,
    spotify_confirmed: bool,
    musicbrainz_confirmed: bool,
    z_score: float,
    spotify_version_count: int
) -> str:
    """
    Final single status per Stage 7.
    
    HIGH-CONFIDENCE:
    - Discogs confirms OR z >= 1.0
    
    MEDIUM-CONFIDENCE:
    - Spotify or MusicBrainz confirms OR z >= 0.5
    
    LOW-CONFIDENCE:
    - z >= 0.2 AND >= 3 Spotify versions
    
    NOT A SINGLE:
    - None of the above
    """
    # HIGH
    if discogs_confirmed or z_score >= 1.0:
        return 'high'
    
    # MEDIUM
    if spotify_confirmed or musicbrainz_confirmed or z_score >= 0.5:
        return 'medium'
    
    # LOW
    if z_score >= 0.2 and spotify_version_count >= 3:
        return 'low'
    
    return 'none'


# ============================================================================
# Main Enhanced Detection Function
# ============================================================================

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
    album_type: Optional[str] = None
) -> Dict:
    """
    Enhanced single detection implementing the 8-stage algorithm.
    
    This function implements the exact algorithm from the problem statement:
    1. Pre-Filter (with compilation detection)
    2. Discogs (Primary)
    3. Spotify (Secondary)
    4. MusicBrainz (Tertiary)
    5. Popularity Inference
    6. Version Matching (integrated)
    7. Final Decision
    8. Database Storage (returned as dict)
    
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
        logger.debug(f"[DEBUG] Compilation detected — checking all tracks for singles.")
    
    # Count Spotify versions
    spotify_version_count = count_spotify_versions(spotify_results or [], title, duration, isrc)
    result['spotify_version_count'] = spotify_version_count
    
    # STAGE 1: Pre-Filter (with compilation override)
    if not should_check_track(popularity, album_mean, album_stddev, album_popularities, spotify_version_count, is_compilation):
        if verbose:
            logger.debug(f"Pre-filter: Skipping {title} (not high priority)")
        return result
    
    if verbose:
        logger.debug(f"Pre-filter: Checking {title} (high priority)")
    
    # STAGE 2: Discogs (Primary Source) - ALWAYS CHECKED FIRST
    discogs_confirmed = False
    if discogs_client and hasattr(discogs_client, 'enabled') and discogs_client.enabled:
        try:
            if verbose:
                logger.debug(f"[DEBUG] Discogs lookup starting for: {title}")
            
            # Use existing is_single method
            discogs_confirmed = discogs_client.is_single(title, artist, album_context={'duration': duration})
            if discogs_confirmed:
                result['single_sources'].append('discogs')
                result['single_sources_used'].append('discogs')
                if verbose:
                    logger.info(f"[INFO] Discogs confirms single: {title}")
                
                # Per problem statement: Discogs = HIGH confidence, skip other checks
                result['single_status'] = 'high'
                result['single_confidence'] = 'high'
                result['is_single'] = True
                result['single_confidence_score'] = 1.0
                
                # Still calculate Z-score for logging purposes
                z_score = calculate_z_score_strict(popularity, album_mean, album_stddev)
                result['z_score'] = z_score
                
                # Add final debug summary
                if verbose:
                    logger.debug(f"[DEBUG] Single detection sources for {title}: {result['single_sources']}")
                    logger.debug(f"[DEBUG] Final single status for {title}: {result['single_confidence']}")
                
                return result
        except Exception as e:
            logger.error(f"Discogs check failed: {e}")
    
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
                logger.debug(f"Spotify: Confirmed single for {title}")
    
    # STAGE 4: MusicBrainz (Tertiary Source)
    musicbrainz_confirmed = False
    if musicbrainz_client and hasattr(musicbrainz_client, 'enabled') and musicbrainz_client.enabled:
        try:
            # Use existing is_single method
            musicbrainz_confirmed = musicbrainz_client.is_single(title, artist)
            if musicbrainz_confirmed:
                result['single_sources'].append('musicbrainz')
                result['single_sources_used'].append('musicbrainz')
                if verbose:
                    logger.debug(f"MusicBrainz: Confirmed single for {title}")
        except Exception as e:
            logger.error(f"MusicBrainz check failed: {e}")
    
    # STAGE 5: Popularity-Based Inference
    z_score = calculate_z_score_strict(popularity, album_mean, album_stddev)
    result['z_score'] = z_score
    
    popularity_confidence, popularity_inferred = infer_from_popularity(z_score, spotify_version_count)
    if popularity_inferred:
        result['single_sources'].append('z-score')
        if verbose:
            logger.debug(f"Popularity: Inferred single for {title} (z={z_score:.2f}, confidence={popularity_confidence})")
    
    # STAGE 7: Final Decision
    final_status = determine_final_status(
        discogs_confirmed,
        spotify_confirmed,
        musicbrainz_confirmed,
        z_score,
        spotify_version_count
    )
    
    result['single_status'] = final_status
    result['single_confidence'] = final_status
    result['is_single'] = final_status in ('high', 'medium', 'low')
    
    # Map confidence to numeric score
    confidence_scores = {'high': 1.0, 'medium': 0.67, 'low': 0.33, 'none': 0.0}
    result['single_confidence_score'] = confidence_scores.get(final_status, 0.0)
    
    # Add final debug summary per track
    if verbose:
        logger.debug(f"[DEBUG] Single detection sources for {title}: {result['single_sources']}")
        logger.debug(f"[DEBUG] Final single status for {title}: {final_status}")
    
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
    - z_score
    - spotify_version_count
    - discogs_release_ids (JSON array)
    - musicbrainz_release_group_ids (JSON array)
    - single_detection_last_updated (timestamp)
    """
    cursor = conn.cursor()
    
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
        result['z_score'],
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
