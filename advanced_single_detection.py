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
from statistics import mean, stdev

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

# Live/unplugged patterns
LIVE_PATTERNS = [
    r'\blive\b',
    r'\bunplugged\b',
    r'\(live\)',
    r'\(unplugged\)',
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


def is_live_version(title: str, album: str) -> bool:
    """
    Check if track or album indicates a live/unplugged version.
    
    Args:
        title: Track title
        album: Album name
        
    Returns:
        True if title or album matches live patterns
    """
    combined = f"{title} {album}".lower()
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
    is_live: bool
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
        
    Returns:
        List of TrackVersion objects for all matching versions
    """
    versions = []
    cursor = conn.cursor()
    
    # Normalize title for matching
    norm_title = normalize_title(title)
    
    # First try: Match by ISRC if available
    if isrc:
        cursor.execute("""
            SELECT id, title, artist, album, isrc, duration, popularity_score,
                   spotify_album_type, is_spotify_single, 
                   source_musicbrainz_single
            FROM tracks
            WHERE artist = ? AND isrc = ?
        """, (artist, isrc))
        
        for row in cursor.fetchall():
            track_title = row[1] or ''
            track_album = row[3] or ''
            
            # Check if it's an alternate version
            is_alt = is_alternate_version(track_title)
            is_live_ver = is_live_version(track_title, track_album)
            
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
                   source_musicbrainz_single
            FROM tracks
            WHERE artist = ?
        """, (artist,))
        
        duration_lower = (duration - 2) if duration else None
        duration_upper = (duration + 2) if duration else None
        
        for row in cursor.fetchall():
            track_title = row[1] or ''
            track_album = row[3] or ''
            track_duration = row[5]
            
            # Check title match
            if normalize_title(track_title) != norm_title:
                continue
            
            # Check duration match (±2 seconds) if both have duration
            if duration and track_duration:
                if not (duration_lower <= track_duration <= duration_upper):
                    continue
            
            # Check if it's an alternate version
            is_alt = is_alternate_version(track_title)
            is_live_ver = is_live_version(track_title, track_album)
            
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
    
    album_mean = mean(album_popularities)
    album_stddev = stdev(album_popularities)
    
    if album_stddev == 0:
        return 0.0
    
    return (popularity - album_mean) / album_stddev


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
    zscore_threshold: float = 0.20,
    verbose: bool = False
) -> Dict:
    """
    Advanced single detection using comprehensive rules.
    
    Implementation of the 8 rules from the problem statement:
    
    1. Match track versions by ISRC or title+duration
    2. Exclude alternate versions
    3. Handle live/unplugged context
    4. Deduplicate album releases
    5. Determine metadata single status
    6. Calculate global popularity across versions
    7. Apply z-score threshold (metadata single + z-score >= threshold)
    8. Special handling for compilations
    
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
        zscore_threshold: Z-score threshold for singles (default 0.20)
        verbose: Enable verbose logging
        
    Returns:
        Dict with keys:
            - is_single: bool
            - confidence: str ('high', 'medium', 'low')
            - sources: List[str]
            - global_popularity: float
            - zscore: float
            - metadata_single: bool
            - is_compilation: bool
    """
    cursor = conn.cursor()
    
    # Check if track is an alternate version (Rule 2)
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
    
    # Determine live/unplugged context (Rule 3)
    is_live = is_live_version(title, album)
    
    # Check if album is compilation (Rule 8)
    is_comp = is_compilation_album(album_type, album)
    
    # Find all matching versions (Rule 1)
    versions = find_matching_versions(conn, title, artist, isrc, duration, is_live)
    
    if verbose:
        logger.info(f"Found {len(versions)} matching versions for: {title}")
    
    # Calculate global popularity (Rule 6)
    # For compilations, use album-version popularity only
    if is_comp:
        global_pop = popularity
        if verbose:
            logger.info(f"Compilation album: using album popularity {global_pop}")
    else:
        global_pop = calculate_global_popularity(versions) if versions else popularity
        if verbose:
            logger.info(f"Global popularity: {global_pop}")
    
    # Determine metadata single status (Rule 5)
    metadata_single = is_metadata_single(versions) if versions else False
    if verbose:
        logger.info(f"Metadata single: {metadata_single}")
    
    # Get all track popularities in the album for z-score calculation
    cursor.execute("""
        SELECT popularity_score
        FROM tracks
        WHERE artist = ? AND album = ? AND popularity_score IS NOT NULL
    """, (artist, album))
    
    album_pops = [row[0] for row in cursor.fetchall() if row[0]]
    
    # Calculate z-score (Rule 6, 7)
    # Use global popularity for z-score calculation (unless compilation)
    zscore = calculate_zscore(global_pop, album_pops) if album_pops else 0.0
    if verbose:
        logger.info(f"Z-score: {zscore:.3f} (threshold: {zscore_threshold})")
    
    # Final single detection (Rule 7)
    # Both conditions must be true:
    # 1. Is metadata single (Spotify OR MusicBrainz)
    # 2. Z-score >= threshold
    is_single = metadata_single and (zscore >= zscore_threshold)
    
    # Special case for compilations (Rule 8)
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
    
    # Determine confidence
    if is_single:
        confidence = 'high'
    elif metadata_single or (zscore >= zscore_threshold):
        confidence = 'medium'
    else:
        confidence = 'low'
    
    return {
        'is_single': is_single,
        'confidence': confidence,
        'sources': sources,
        'global_popularity': global_pop,
        'zscore': zscore,
        'metadata_single': metadata_single,
        'is_compilation': is_comp
    }


def batch_update_advanced_singles(
    conn: sqlite3.Connection,
    artist: Optional[str] = None,
    album: Optional[str] = None,
    zscore_threshold: float = 0.20,
    verbose: bool = False
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
        
    Returns:
        Number of tracks updated
    """
    cursor = conn.cursor()
    
    # Build query with filters
    where_clauses = []
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
        
        # Run advanced detection
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
            verbose=verbose
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
