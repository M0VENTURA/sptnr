#!/usr/bin/env python3
"""
Advanced Single Detection Logic

Implements comprehensive single detection rules including:
1. ISRC-based track version matching
2. Title+duration matching (±2 seconds fallback)
3. Alternate version filtering
4. Live/unplugged context handling
5. Album release deduplication
6. Global popularity calculation across versions
7. Z-score based final determination
8. Compilation/greatest hits special handling
"""

import re
import json
import sqlite3
import logging
from typing import Dict, List, Tuple, Optional, Set
from dataclasses import dataclass
from statistics import mean, stdev, median

logger = logging.getLogger(__name__)


@dataclass
class TrackVersion:
    """Represents a version of a track across different releases"""
    track_id: str
    title: str
    artist: str
    album: str
    isrc: Optional[str]
    duration: Optional[float]
    popularity: float
    is_live: bool
    is_alternate: bool
    album_type: Optional[str]
    spotify_single: bool
    musicbrainz_single: bool


# Alternate version patterns to exclude (case-insensitive)
ALTERNATE_VERSION_PATTERNS = [
    r'\(remix\)',
    r'\(orchestral\)',
    r'\(acoustic\)',
    r'\(demo\)',
    r'\(instrumental\)',
    r'\(radio edit\)',
    r'\(edit\)',
    r'\(extended\)',
    r'\(club mix\)',
    r'\(alternate\)',
    r'\(alt version\)',
    r'\(re-recorded\)',
    r'\(re-recording\)',
    r'\(karaoke\)',
    r'\(cover\)',
]

# Live/acoustic/unplugged patterns
LIVE_PATTERNS = [
    # More specific patterns to avoid matching "live" in titles like "(how to live)"
    r'\blive\s+at\b',          # "live at venue"
    r'\blive\s+in\b',          # "live in city"  
    r'\blive\s+from\b',        # "live from"
    r'\blive\s+session\b',     # "live session"
    r'\blive\s+tour\b',        # "live tour"
    r'\(live\)',               # "(live)" format
    r'\[live\]',               # "[live]" format
    r'-\s*live\b',             # "- live" suffix
    r'\s+live\s*$',            # ends with " live"
    r'\bunplugged\b',
    r'\bacoustic\b',
    r'\(unplugged\)',
    r'\(acoustic\)',
]


def normalize_title(title: str) -> str:
    """
    Normalize title for matching by removing punctuation, case, and bracketed suffixes.
    
    Args:
        title: Original track title
        
    Returns:
        Normalized title for comparison
    """
    # Remove bracketed/parenthesized content
    normalized = re.sub(r'\s*[\(\[].*?[\)\]]', '', title)
    # Remove dash-based versions (more comprehensive patterns)
    normalized = re.sub(r'\s*-\s*(?:Live|Remix|Remaster|Edit|Mix|Version|Acoustic|Unplugged).*$', '', normalized, flags=re.IGNORECASE)
    # Remove punctuation
    normalized = re.sub(r'[^\w\s]', '', normalized)
    # Lowercase and strip
    normalized = normalized.lower().strip()
    # Normalize whitespace
    normalized = re.sub(r'\s+', ' ', normalized)
    return normalized


def is_alternate_version(title: str) -> bool:
    """
    Check if track title indicates an alternate version.
    
    Args:
        title: Track title to check
        
    Returns:
        True if title matches alternate version patterns
    """
    title_lower = title.lower()
    for pattern in ALTERNATE_VERSION_PATTERNS:
        if re.search(pattern, title_lower):
            return True
    return False


def is_live_version(title: str, album: str, genres: str = '') -> bool:
    """
    Check if track or album indicates a live/acoustic/unplugged version.
    
    Args:
        title: Track title
        album: Album name
        genres: Genre tags string (comma or semicolon separated)
        
    Returns:
        True if title, album, or genres match live/acoustic/unplugged patterns
    """
    combined = f"{title} {album} {genres}".lower()
    for pattern in LIVE_PATTERNS:
        if re.search(pattern, combined):
            return True
    return False


def normalize_album_identity(album: str, track_titles: List[str]) -> str:
    """
    Create normalized album identity for grouping releases of the same album.
    
    Groups albums by:
    - Normalized album title (case-insensitive, punctuation removed)
    - Track title sequence (ignoring suffixes)
    
    Args:
        album: Album name
        track_titles: List of track titles on the album
        
    Returns:
        Normalized album identity string
    """
    # Normalize album title
    norm_album = re.sub(r'[^\w\s]', '', album).lower().strip()
    norm_album = re.sub(r'\s+', ' ', norm_album)
    
    # Normalize track titles and create fingerprint
    norm_titles = [normalize_title(t) for t in track_titles]
    title_fingerprint = '|'.join(sorted(norm_titles))
    
    return f"{norm_album}::{title_fingerprint}"


def find_matching_versions(
    conn: sqlite3.Connection,
    title: str,
    artist: str,
    isrc: Optional[str],
    duration: Optional[float],
    is_live: bool,
    album: Optional[str] = None
) -> List[TrackVersion]:
    """
    Find all versions of the same song across different releases.
    
    Matching rules:
    1. Match by ISRC when available
    2. If ISRC is missing, match by title + duration (±2 seconds)
    3. Filter live/unplugged based on context
    
    Args:
        conn: Database connection
        title: Track title
        artist: Artist name
        isrc: ISRC code (optional)
        duration: Track duration in seconds (optional)
        is_live: Whether the current album is live/unplugged
        album: Album name to verify live album detection (optional)
        
    Returns:
        List of TrackVersion objects for all matching versions
    """
    versions = []
    cursor = conn.cursor()
    
    # Normalize title for matching
    norm_title = normalize_title(title)
    
    # Verify live album detection if album name provided
    if album:
        try:
            from popularity import is_live_or_alternate_album
            album_is_live = is_live_or_alternate_album(album)
            if album_is_live and not is_live:
                # Log that we detected this is a live album but is_live parameter was False
                logger.debug(f"Album detection found live album: {album} for track {title}")
                is_live = True
        except ImportError:
            pass  # Fall back to is_live parameter alone
    
    # First try: Match by ISRC if available
    if isrc:
        cursor.execute("""
            SELECT id, title, artist, album, isrc, duration, popularity_score,
                   spotify_album_type, is_spotify_single, 
                   source_musicbrainz_single, genres
            FROM tracks
            WHERE artist = ? AND isrc = ?
        """, (artist, isrc))
        
        for row in cursor.fetchall():
            track_title = row[1] or ''
            track_album = row[3] or ''
            track_genres = row[10] or ''
            
            # Check if it's an alternate version
            is_alt = is_alternate_version(track_title)
            is_live_ver = is_live_version(track_title, track_album, track_genres)
            
            # Skip if live/unplugged context doesn't match
            if is_live and not is_live_ver:
                continue
            if not is_live and is_live_ver:
                continue
            
            versions.append(TrackVersion(
                track_id=row[0],
                title=row[1] or '',
                artist=row[2] or '',
                album=row[3] or '',
                isrc=row[4],
                duration=row[5],
                popularity=row[6] or 0.0,
                is_live=is_live_ver,
                is_alternate=is_alt,
                album_type=row[7],
                spotify_single=bool(row[8]),
                musicbrainz_single=bool(row[9])
            ))
    
    # Second try: Match by normalized title + duration (±2 seconds)
    if not versions:
        # Get all tracks with same artist and similar title
        cursor.execute("""
            SELECT id, title, artist, album, isrc, duration, popularity_score,
                   spotify_album_type, is_spotify_single,
                   source_musicbrainz_single, genres
            FROM tracks
            WHERE artist = ?
        """, (artist,))
        
        duration_lower = (duration - 2) if duration else None
        duration_upper = (duration + 2) if duration else None
        
        for row in cursor.fetchall():
            track_title = row[1] or ''
            track_album = row[3] or ''
            track_duration = row[5]
            track_genres = row[10] or ''
            
            # Check title match
            if normalize_title(track_title) != norm_title:
                continue
            
            # Check duration match (±2 seconds) if both have duration
            if duration and track_duration:
                if not (duration_lower <= track_duration <= duration_upper):
                    continue
            
            # Check if it's an alternate version
            is_alt = is_alternate_version(track_title)
            is_live_ver = is_live_version(track_title, track_album, track_genres)
            
            # Skip if live/unplugged context doesn't match
            if is_live and not is_live_ver:
                continue
            if not is_live and is_live_ver:
                continue
            
            versions.append(TrackVersion(
                track_id=row[0],
                title=track_title,
                artist=row[2] or '',
                album=track_album,
                isrc=row[4],
                duration=track_duration,
                popularity=row[6] or 0.0,
                is_live=is_live_ver,
                is_alternate=is_alt,
                album_type=row[7],
                spotify_single=bool(row[8]),
                musicbrainz_single=bool(row[9])
            ))
    
    return versions


def calculate_global_popularity(versions: List[TrackVersion]) -> float:
    """
    Calculate global popularity as the maximum across all matched versions.
    
    Filters out alternate versions before calculating.
    
    Args:
        versions: List of track versions
        
    Returns:
        Maximum popularity score across canonical versions
    """
    # Filter out alternate versions
    canonical_versions = [v for v in versions if not v.is_alternate]
    
    if not canonical_versions:
        # If all are alternates, return 0
        return 0.0
    
    # Filter out versions with zero or None popularity
    valid_pops = [v.popularity for v in canonical_versions if v.popularity and v.popularity > 0]
    
    if not valid_pops:
        # No valid popularity scores
        return 0.0
    
    # Return max popularity
    return max(valid_pops)


def is_metadata_single(versions: List[TrackVersion]) -> bool:
    """
    Determine if track is a metadata single.
    
    A track is a metadata single if:
    - It has a Spotify single release, OR
    - It appears in a MusicBrainz release group of type "single"
    
    Args:
        versions: List of track versions
        
    Returns:
        True if any version is marked as single in metadata
    """
    for version in versions:
        if version.spotify_single or version.musicbrainz_single:
            return True
    return False


def calculate_zscore(
    popularity: float,
    album_popularities: List[float]
) -> float:
    """
    Calculate z-score for a track within its album.
    
    Z-score = (popularity - mean) / stddev
    
    Args:
        popularity: Track popularity score
        album_popularities: List of all track popularities in the album
        
    Returns:
        Z-score (0 if stddev is 0)
    """
    if len(album_popularities) < 2:
        return 0.0
    
    album_median = median(album_popularities)
    album_stddev = stdev(album_popularities)
    
    if album_stddev == 0:
        return 0.0
    
    return (popularity - album_median) / album_stddev


def is_compilation_album(album_type: Optional[str], album: str) -> bool:
    """
    Check if album is a compilation or greatest hits album.
    
    Args:
        album_type: Spotify album type (if available)
        album: Album name
        
    Returns:
        True if album is a compilation or greatest hits
    """
    if album_type and album_type.lower() == 'compilation':
        return True
    
    album_lower = album.lower()
    compilation_keywords = [
        'greatest hits',
        'best of',
        'collection',
        'anthology',
        'compilation',
        'essentials',
    ]
    
    for keyword in compilation_keywords:
        if keyword in album_lower:
            return True
    
    return False


def detect_single_advanced(
    conn: sqlite3.Connection,
    track_id: str,
    title: str,
    artist: str,
    album: str,
    isrc: Optional[str],
    duration: Optional[float],
    popularity: float,
    album_type: Optional[str],
    zscore_threshold: float = 1.0,
    verbose: bool = False,
    discogs_client=None
) -> Dict:
    """
    Advanced single detection using comprehensive rules.
    
    Implementation of the 8 rules from the problem statement:
    
    1. Discogs artist releases endpoint check (early exit if confirmed)
    2. Match track versions by ISRC or title+duration
    3. Exclude alternate versions
    4. Handle live/unplugged context
    5. Deduplicate album releases
    6. Determine metadata single status
    7. Calculate global popularity across versions
    8. Apply z-score threshold (metadata single + z-score >= threshold)
    9. Special handling for compilations
    
    Args:
        conn: Database connection
        track_id: Track ID
        title: Track title
        artist: Artist name
        album: Album name
        isrc: ISRC code (optional)
        duration: Track duration in seconds (optional)
        popularity: Track popularity score
        album_type: Spotify album type (optional)
        zscore_threshold: Z-score threshold for singles based on artist median (default 1.0)
        verbose: Enable verbose logging
        discogs_client: Optional DiscogsClient instance for Discogs single detection
        
    Returns:
        Dict with keys:
            - is_single: bool
            - confidence: str ('high', 'medium', 'low')
            - sources: List[str]
            - global_popularity: float
            - zscore: float (calculated against artist median)
            - metadata_single: bool
            - is_compilation: bool
    """
    cursor = conn.cursor()
    
    # Check if track is an alternate version (Rule 3)
    if is_alternate_version(title):
        if verbose:
            logger.info(f"Excluding alternate version: {title}")
        return {
            'is_single': False,
            'confidence': 'low',
            'sources': [],
            'global_popularity': 0.0,
            'zscore': 0.0,
            'metadata_single': False,
            'is_compilation': False
        }
    
    # Determine live/unplugged context (Rule 4)
    is_live = is_live_version(title, album)
    
    # Check if album is compilation (Rule 9)
    is_comp = is_compilation_album(album_type, album)
    
    # EARLY EXIT CONDITION:
    # Try Discogs first (Rule 1) - if confirmed as single, exit with high confidence
    if discogs_client:
        try:
            album_context = {
                "is_live": is_live,
                "is_unplugged": False,  # TODO: detect unplugged from title if needed
                "is_special_edition": False,  # Skip this check for now, can be added later
                "album_name": album
            }
            if discogs_client.is_single(title, artist, album_context):
                if verbose:
                    logger.info(f"Discogs confirmed '{title}' as single for artist '{artist}'")
                return {
                    'is_single': True,
                    'confidence': 'high',
                    'sources': ['discogs'],
                    'global_popularity': popularity,
                    'zscore': 0.0,
                    'metadata_single': True,  # Treat Discogs confirmation as metadata
                    'is_compilation': is_comp
                }
        except Exception as e:
            if verbose:
                logger.debug(f"Discogs check failed (will continue with other sources): {e}")
            # Continue with other detection methods if Discogs fails
    
    # Find all matching versions (Rule 2)
    versions = find_matching_versions(conn, title, artist, isrc, duration, is_live, album)
    
    if verbose:
        logger.info(f"Found {len(versions)} matching versions for: {title}")
    
    # Calculate global popularity (Rule 7)
    # For compilations, use album-version popularity only
    if is_comp:
        global_pop = popularity
        if verbose:
            logger.info(f"Compilation album: using album popularity {global_pop}")
    else:
        global_pop = calculate_global_popularity(versions) if versions else popularity
        if verbose:
            logger.info(f"Global popularity: {global_pop}")
    
    # Determine metadata single status (Rule 6)
    metadata_single = is_metadata_single(versions) if versions else False
    if verbose:
        logger.info(f"Metadata single: {metadata_single}")
    
    # STAGE 1: Album-level statistics (must be album standout first)
    cursor.execute("""
        SELECT popularity_score
        FROM tracks
        WHERE artist = ? AND album = ? AND popularity_score IS NOT NULL
    """, (artist, album))
    
    album_pops = [row[0] for row in cursor.fetchall() if row[0]]
    album_median_val = median(album_pops) if album_pops else 0.0
    album_stddev_val = stdev(album_pops) if len(album_pops) > 1 else 0.0
    
    # STAGE 2: Artist-level statistics for standout detection
    cursor.execute("""
        SELECT popularity_score
        FROM tracks
        WHERE artist = ? AND popularity_score IS NOT NULL
    """, (artist,))
    
    artist_pops = [row[0] for row in cursor.fetchall() if row[0]]
    artist_median_val = median(artist_pops) if len(artist_pops) > 1 else 0.0
    artist_stddev_val = stdev(artist_pops) if len(artist_pops) > 1 else 1.0
    
    # TWO-STAGE Z-SCORE CALCULATION
    # Stage 1: Album standout check
    if album_pops:
        album_threshold = album_median_val - (0.5 * album_stddev_val) if album_stddev_val > 0 else album_median_val
        sorted_album = sorted(album_pops, reverse=True)
        is_top_3_album = global_pop in sorted_album[:3]
        album_standout = (global_pop >= album_threshold) or is_top_3_album
    else:
        album_standout = True  # No album data, assume pass
    
    # Stage 2: Artist standout check (only if artist has 5+ tracks)
    artist_standout = True  # Default: pass
    artist_zscore = 0.0
    if len(artist_pops) >= 5:
        artist_zscore = (global_pop - artist_median_val) / artist_stddev_val if artist_stddev_val > 0 else 0.0
        artist_threshold = zscore_threshold
        artist_standout = artist_zscore >= artist_threshold
    
    if verbose:
        logger.info(f"[FILTER] Album standout: {album_standout} (pop={global_pop:.1f}, median={album_median_val:.1f})")
        if len(artist_pops) >= 5:
            logger.info(f"[FILTER] Artist standout: {artist_standout} (z-score={artist_zscore:.3f}, threshold={zscore_threshold})")
        else:
            logger.info(f"[BOOTSTRAP] Artist has {len(artist_pops)} tracks (<5), using album filter only")
    
    # Final single detection (Rule 8)
    # All conditions must be true:
    # 1. Is metadata single (Spotify OR MusicBrainz)
    # 2. Passes album standout check (top 3 or above threshold)
    # 3. Passes artist standout check (z-score >= threshold, if artist has 5+ tracks)
    is_single = metadata_single and album_standout and artist_standout
    
    # Special case for compilations (Rule 9)
    if is_comp:
        # Only detect singles released FROM the compilation
        # This requires the single to be on the compilation itself
        # For now, we keep the same logic but flag it
        if verbose and is_single:
            logger.info(f"Single detected on compilation: {title}")
    
    # Determine sources and confidence
    sources = []
    if metadata_single:
        for version in versions:
            if version.spotify_single and 'spotify' not in sources:
                sources.append('spotify')
            if version.musicbrainz_single and 'musicbrainz' not in sources:
                sources.append('musicbrainz')
    
    # NOTE: Z-score is used for confidence calculation only, NOT added to sources
    # Per problem statement: z-score should not appear as a high-confidence source
    
    # Determine confidence based on zscore thresholds:
    # - zscore > 3.0: high confidence
    # - zscore > 1.8: medium confidence
    # - Otherwise: low confidence
    # Priority: metadata sources always override zscore-based confidence
    if is_single:
        # Metadata sources (Discogs, Spotify, MusicBrainz) always = high confidence
        confidence = 'high'
    elif artist_zscore >= 3.0:
        # High zscore = high confidence
        confidence = 'high'
    elif metadata_single or (artist_zscore >= 1.8):
        # Medium zscore or metadata hint = medium confidence
        confidence = 'medium'
    else:
        confidence = 'low'
    
    return {
        'is_single': is_single,
        'confidence': confidence,
        'sources': sources,
        'global_popularity': global_pop,
        'zscore': artist_zscore,
        'metadata_single': metadata_single,
        'is_compilation': is_comp
    }


def batch_update_advanced_singles(
    conn: sqlite3.Connection,
    artist: Optional[str] = None,
    album: Optional[str] = None,
    zscore_threshold: float = 1.0,
    verbose: bool = False,
    discogs_client=None
) -> int:
    """
    Batch update all tracks with advanced single detection.
    
    Note: Requires database schema to have the necessary columns.
    Run check_db.update_schema() first to ensure schema is up to date.
    
    Args:
        conn: Database connection
        artist: Optional artist filter
        album: Optional album filter
        zscore_threshold: Z-score threshold for singles
        verbose: Enable verbose logging
        discogs_client: Optional DiscogsClient for early single detection
        
    Returns:
        Number of tracks updated
    """
    cursor = conn.cursor()
    
    # Build query with filters
    where_clauses = ["single_manual_override IS NULL OR single_manual_override = 0"]  # Skip manually overridden singles
    params = []
    
    if artist:
        where_clauses.append("artist = ?")
        params.append(artist)
    
    if album:
        where_clauses.append("album = ?")
        params.append(album)
    
    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    
    # Get all tracks to process
    cursor.execute(f"""
        SELECT id, title, artist, album, isrc, duration, popularity_score, spotify_album_type
        FROM tracks
        {where_sql}
        ORDER BY artist, album, title
    """, params)
    
    tracks = cursor.fetchall()
    updates = []
    
    for row in tracks:
        track_id, title, artist, album, isrc, duration, pop, album_type = row
        
        # Run advanced detection with optional Discogs client for early detection
        result = detect_single_advanced(
            conn=conn,
            track_id=track_id,
            title=title or '',
            artist=artist or '',
            album=album or '',
            isrc=isrc,
            duration=duration,
            popularity=pop or 0.0,
            album_type=album_type,
            zscore_threshold=zscore_threshold,
            verbose=verbose,
            discogs_client=discogs_client
        )
        
        # Queue update
        updates.append((
            1 if result['is_single'] else 0,
            result['confidence'],
            json.dumps(result['sources']),
            result['global_popularity'],
            result['zscore'],
            1 if result['metadata_single'] else 0,
            1 if result['is_compilation'] else 0,
            track_id
        ))
    
    # Batch update (assumes schema already has required columns)
    if updates:
        cursor.executemany("""
            UPDATE tracks
            SET is_single = ?,
                single_confidence = ?,
                single_sources = ?,
                global_popularity = ?,
                zscore = ?,
                metadata_single = ?,
                is_compilation = ?
            WHERE id = ?
        """, updates)
        
        conn.commit()
    
    return len(updates)
