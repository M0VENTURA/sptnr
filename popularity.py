#!/usr/bin/env python3
"""
Popularity Scanner - Detects track popularity from external sources (Spotify, Last.fm, ListenBrainz).
Calculates popularity scores and updates database.
Note: Singles detection is handled separately by sptnr.py rate_artist() function.
"""

import os
import sqlite3
try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    psycopg2 = None  # type: ignore[assignment]
import logging
import json
import math
import yaml
import atexit
import time
import heapq
import re
import difflib
import unicodedata
import requests
from contextlib import contextmanager
from datetime import datetime, timedelta
from statistics import median, mean, stdev
from concurrent.futures import ThreadPoolExecutor
import concurrent.futures
from api_clients import session, timeout_safe_session
from helpers.helpers import find_matching_spotify_single, strip_cover_attribution
from helpers.matching_utils import normalize_album
from database_abstraction import DatabaseQuery, is_postgres_connection

# Import centralized logging
from helpers.logging_config import setup_logging, log_unified, log_info, log_debug

# Set up logging for popularity service
setup_logging("popularity")

# Import API clients for single detection at module level
try:
    from api_clients.musicbrainz import MusicBrainzClient, get_artist_country  # type: ignore
    HAVE_MUSICBRAINZ = True
except ImportError as e:
    log_debug(f"MusicBrainz client unavailable: {e}")
    HAVE_MUSICBRAINZ = False
    MusicBrainzClient = None  # type: ignore
    
try:
    from api_clients.discogs import DiscogsClient  # type: ignore
    HAVE_DISCOGS = True
    HAVE_DISCOGS_VIDEO = True
except ImportError as e:
    log_debug(f"Discogs client unavailable: {e}")
    HAVE_DISCOGS = False
    HAVE_DISCOGS_VIDEO = False
    DiscogsClient = None  # type: ignore

try:
    from api_clients.audiodb import get_artist_biography, get_artist_fanart
    HAVE_AUDIODB = True
except ImportError as e:
    log_debug(f"AudioDB client unavailable: {e}")
    HAVE_AUDIODB = False

try:
    from cover_detector import CoverDetector
    HAVE_COVER_DETECTOR = True
except ImportError as e:
    log_debug(f"Cover detector unavailable: {e}")
    HAVE_COVER_DETECTOR = False
    CoverDetector = None  # type: ignore

# Timeout-safe clients for use within _run_with_timeout() context
# These use timeout_safe_session with reduced retry count to prevent exceeding timeout
_timeout_safe_mb_client = None
_timeout_safe_discogs_clients = {}  # token -> client mapping

def _get_timeout_safe_musicbrainz_client():
    """Get or create timeout-safe MusicBrainz client for use in popularity scanner."""
    global _timeout_safe_mb_client
    if _timeout_safe_mb_client is None and HAVE_MUSICBRAINZ:
        _timeout_safe_mb_client = MusicBrainzClient(http_session=timeout_safe_session, enabled=True)
    return _timeout_safe_mb_client

def _get_timeout_safe_discogs_client(token: str):
    """Get or create timeout-safe Discogs client for use in popularity scanner."""
    global _timeout_safe_discogs_clients, HAVE_DISCOGS, HAVE_DISCOGS_VIDEO, DiscogsClient
    if not token:
        return None

    # Lazy import fallback: if module-level import failed early, retry at runtime.
    # This keeps singles detection aligned with batch genre fetch behavior.
    if not HAVE_DISCOGS or DiscogsClient is None:
        try:
            from api_clients.discogs import DiscogsClient as RuntimeDiscogsClient
            DiscogsClient = RuntimeDiscogsClient  # type: ignore
            HAVE_DISCOGS = True
            HAVE_DISCOGS_VIDEO = True
            log_debug("Discogs client recovered via lazy runtime import for singles detection")
        except Exception as e:
            log_debug(f"Discogs lazy runtime import failed: {e}")
            return None

    if token not in _timeout_safe_discogs_clients:
        try:
            _timeout_safe_discogs_clients[token] = DiscogsClient(token, http_session=timeout_safe_session, enabled=True)
        except Exception as e:
            log_debug(f"Discogs client initialization failed for singles detection: {e}")
            return None
    return _timeout_safe_discogs_clients.get(token)

# Module-level logger
logger = logging.getLogger(__name__)

# Keyword filter for non-singles (defined at module level for performance)
# Filters out alternate versions: live, acoustic, orchestral, remixes, demos, etc.
# NOTE: Remastered versions are NOT filtered - they are the same songs and should be processed normally
#       (unlike remixes, live versions, or acoustic versions which are substantially different)
# Note: List is used (not set) since we perform substring matching with 'any(k in title...)'
IGNORE_SINGLE_KEYWORDS = [
    "intro", "outro", "jam",  # intros/outros/jams
    "live", "unplugged",  # live performances
    "remix", "edit", "mix",  # remixes and edits
    "acoustic", "orchestral",  # alternate arrangements
    "demo", "instrumental", "karaoke",  # alternate versions
    # NOTE: Removed "remaster", "remastered" - these are same songs, just reprocessed for quality
]

# Minimum tracks required for artist-level standout/star rating comparison
MIN_TRACKS_FOR_ARTIST_COMPARISON = 10
# Subset of keywords to check in Spotify album names (for album-level filtering)
# These are the most common alternate version album types
# Note: List is used (not set) since we perform substring matching with 'any(k in album_name...)'
SPOTIFY_ALBUM_EXCLUDE_KEYWORDS = [
    "live", "remix", "acoustic", "unplugged", "orchestral", "demo", "instrumental"
]

# Genre weighting configuration for multi-source aggregation
GENRE_WEIGHTS = {
    "musicbrainz": 0.40,   # Most trusted
    "discogs": 0.25,       # Still strong
    "audiodb": 0.20,       # Good for fallback
    "lastfm": 0.10,        # Reduce slightly (tags can be messy)
    "spotify": 0.05        # Keep low (too granular)
}


# --- Standout & Star Rating Config ---
STANDOUT_CONFIG = {
    'album_zscore_threshold': 0.8,         # Album standout: z >= 0.8 (medium confidence, lowered from 1.0)
    'album_top_n': 2,                     # (DEPRECATED - using gap-based clustering instead)
    'artist_zscore_threshold': 2.2,       # Artist standout: z >= 2.2 (popularity standout threshold)
    'artist_top_percentile': 0.10,        # Top 10% of artist catalog
    'artist_min_tracks': 10,              # Min tracks for artist-level filter
    'star_5': {'album_z': 1.0, 'artist_z': 1.2, 'artist_pct': 0.10},  # Requires z >= 1.0 for 5★
    'star_4': {'album_z': 0.8, 'artist_z': 1.0, 'artist_pct': 0.20},  # Uses lower 0.8 threshold
    'star_3': {'album_z': 0.0},
    'star_2': {'album_mean': True},
    'star_1': {'default': True},
}

# Threshold for detecting underperforming albums (album median < artist_median * this value)
UNDERPERFORMING_THRESHOLD = 0.7

# --- Single Detection Confidence Thresholds ---
# These are loaded from config, with fallbacks to defaults
def get_zscore_thresholds():
    """Load z-score thresholds from config, or use defaults"""
    try:
        from helpers.config_loader import get_zscore_thresholds
        thresholds = get_zscore_thresholds()
        return {
            'medium': thresholds.get('medium', 0.6),
            'high': thresholds.get('high', 1.0),
            'standout_gap_z': thresholds.get('standout_gap_z', 0.75),
        }
    except Exception:
        return {'medium': 0.6, 'high': 1.0, 'standout_gap_z': 0.75}


def apply_standout_config_overrides() -> None:
    """Apply standout/star-rating threshold overrides from config.yaml when available."""
    try:
        thresholds = get_zscore_thresholds()
        STANDOUT_CONFIG['star_5']['standout_gap_z'] = float(thresholds.get('standout_gap_z', 0.75))
    except Exception:
        STANDOUT_CONFIG['star_5']['standout_gap_z'] = 0.75


apply_standout_config_overrides()

DEFAULT_HIGH_CONF_OFFSET = 1.0        # High confidence: z-score >= 1.0 (loaded from config)
DEFAULT_MEDIUM_CONF_THRESHOLD = 0.6   # Medium confidence: z-score >= 0.6 (loaded from config)
DEFAULT_POPULARITY_MEAN = 50          # Default mean popularity if no valid scores available (0-100 scale)

# --- End Config ---

# Metadata source display constant
POPULARITY_METADATA_SOURCE_NAME = "Spotify/Last.fm popularity"  # Display name for tracks with popularity data but no single sources


def get_lastfm_config(config: dict) -> dict:
    """Return Last.fm config, supporting both lastfm and last_fm keys."""
    api_integrations = config.get("api_integrations", {}) if isinstance(config, dict) else {}
    return api_integrations.get("lastfm") or api_integrations.get("last_fm") or {}


def strip_parentheses(title: str) -> str:
    """
    Remove TRAILING parenthesized content from track title to get base version.
    
    This differs from helpers.strip_parentheses() which removes ALL parentheses.
    For alternate take detection, we only want to remove trailing parentheses
    (e.g., "Track (Live)" -> "Track") but keep middle ones (e.g., "Track (One) Two").
    
    Example: "Track (Live)" -> "Track"
    Example: "Track (One) Two" -> "Track (One) Two"  (no change)
    """
    return re.sub(r'\s*\([^)]*\)\s*$', '', title).strip()


def is_compilation_type(album_type: str) -> bool:
    """
    Check if album type indicates compilation.
    
    Handles both:
    - Old format: 'compilation' (standalone)
    - New MusicBrainz secondary type format: 'album+compilation'
    
    Args:
        album_type: Album type string from database or MusicBrainz
        
    Returns:
        True if album is a compilation, False otherwise
    """
    if not album_type:
        return False
    album_type_lower = album_type.lower()
    return album_type_lower == 'compilation' or '+compilation' in album_type_lower


def should_exclude_track_from_stats(title: str, album: str = "") -> bool:
    """
    Determine if a track should be excluded from album/artist statistics calculations.
    
    Excludes tracks that are:
    - Live versions
    - Remixes
    - Acoustic/orchestral versions
    - Demos
    - Instrumentals
    - Other alternate versions
    
    NOTE: Remastered versions are NOT excluded - they are the same songs and should be
          included in statistics calculations (unlike live/remix versions which are substantially different).
    
    This ensures that album median, mean, stddev calculations reflect the core album tracks
    and are not skewed by bonus/alternate versions.
    
    Args:
        title: Track title to check
        album: Album name to check (optional, for live album detection)
        
    Returns:
        True if track should be excluded from statistics, False otherwise
    """
    # Strip cover attributions first to get the base version of the track
    # This way "(Live Cover)" becomes just the base title before checking filters
    base_title = strip_cover_attribution(title)
    
    # Check base title and album name for keywords
    combined_text = f"{base_title} {album}".lower()
    return any(keyword in combined_text for keyword in IGNORE_SINGLE_KEYWORDS)


def is_live_or_alternate_album(album: str) -> bool:
    """
    Determine if an album is a live, unplugged, or acoustic album.
    
    This helps identify albums where the recorded versions differ from studio versions,
    such as "Alice in Chains - Unplugged in New York" where tracks should not be matched
    with their studio counterparts.
    
    Only matches format indicators, not "live" as part of the actual album title.
    Examples:
    - "Album (Live)" -> True
    - "Album Live at Venue" -> True
    - "(how to live) AS GHOSTS" -> False
    
    Args:
        album: Album name to check
        
    Returns:
        True if this is a live/unplugged/acoustic album, False otherwise
    """
    if not album:
        return False
    
    album_lower = album.lower()
    
    # More specific live album indicators (avoid matching "live" within titles)
    live_patterns = [
        r'\blive\s+at\b',          # "live at venue"
        r'\blive\s+in\b',          # "live in city"
        r'\blive\s+from\b',        # "live from venue"
        r'\blive\s+session\b',     # "live session"
        r'\blive\s+recording\b',   # "live recording"
        r'\blive\s+tour\b',        # "live tour"
        r'\(live\)\s*$',           # "(live)" at the end
        r'\[live\]\s*$',           # "[live]" at the end
        r'-\s*live\s*$',           # "- live" at the end
        r'\s+live\s*$',            # ends with " live"
        r'\s+live\s*[\)\]]\s*$',   # "live)" or "live]" at the end
        r'\bunplugged\b',          # "unplugged"
        r'\bacoustic\b',           # "acoustic"
        r'\bconcert\b',            # "concert" album
        r'\bon\s+stage\b',         # "on stage"
        r'\bin\s+concert\b',       # "in concert"
    ]
    
    import re
    return any(re.search(pattern, album_lower) for pattern in live_patterns)


def detect_alternate_takes(tracks: list) -> dict:
    """
    Detect alternate takes in a list of tracks by comparing titles with/without parentheses.
    
    An alternate take is a track whose title:
    1. Ends with a parenthesized suffix (e.g., "Track (Live)")
    2. Has a base version (without parentheses) that matches another track
    3. Appears later in the track list (lower track number or at end of album)
    
    Args:
        tracks: List of track dicts with 'id', 'title', 'track_number' fields
        
    Returns:
        Dict mapping track_id -> base_track_id for all detected alternate takes
    """
    alternate_takes = {}
    title_to_track = {}  # Map base title -> track info
    
    for track in tracks:
        track_id = track['id']
        title = row_get(track, 'title', '')
        track_number = row_get(track, 'track_number', 999)
        
        # Check if this track has parentheses at the end
        if re.match(r'^.*\([^)]*\)$', title):
            # Get base title without parentheses
            base_title = strip_parentheses(title)
            base_title_lower = base_title.lower()
            
            # Check if we have a track with this base title
            if base_title_lower in title_to_track:
                # This is an alternate take - link to base track
                base_track = title_to_track[base_title_lower]
                alternate_takes[track_id] = base_track['id']
                # Safe logging - avoid f-string interpolation with user data
                log_verbose("   Detected alternate take: '%s' -> base: '%s'" % (title, base_track['title']))
            else:
                # No base track yet - record this one as a potential base
                # (in case we see a non-parenthesis version later)
                title_to_track[base_title_lower] = {
                    'id': track_id,
                    'title': title,
                    'track_number': track_number
                }
        else:
            # No parentheses - this is a base version
            title_lower = title.lower()
            
            # Check if we already saw an alternate take for this title
            if title_lower in title_to_track:
                existing_track = title_to_track[title_lower]
                # If existing track has parentheses, mark it as alternate
                if re.match(r'^.*\([^)]*\)$', existing_track['title']):
                    alternate_takes[existing_track['id']] = track_id
                    # Safe logging - avoid f-string interpolation with user data
                    log_verbose("   Detected alternate take: '%s' -> base: '%s'" % (existing_track['title'], title))
            
            # Record this as the base track
            title_to_track[title_lower] = {
                'id': track_id,
                'title': title,
                'track_number': track_number
            }
    
    return alternate_takes


def detect_compilation_album(artist: str, album: str, tracks: list, album_artist: str = None, spotify_album_type: str = None) -> bool:
    """
    Detect if an album is a compilation using local heuristics only (no API calls).
    
    Checks:
    1. album_artist field for Various Artists, Compilation, Soundtrack
    2. Spotify album type is "compilation"
    3. Multiple distinct artists in track listing
    
    Args:
        artist: Primary artist name
        album: Album name
        tracks: List of tracks in the album
        album_artist: Album artist from metadata (if available)
        spotify_album_type: Spotify album type classification
        
    Returns:
        True if album appears to be a compilation, False otherwise
    """
    # Check album_artist field first (most reliable)
    if album_artist:
        album_artist_lower = album_artist.lower()
        if album_artist_lower in ('various artists', 'various', 'compilation', 'soundtrack'):
            log_debug(f'Compilation detected for "{album}": album_artist="{album_artist}"')
            return True
    
    # Check Spotify classification
    if spotify_album_type and spotify_album_type.lower() == 'compilation':
        log_debug(f'Compilation detected for "{album}": spotify_album_type="compilation"')
        return True
    
    # Check if there are multiple distinct artists in the track listing
    # (indicates compilation even if not explicitly marked)
    try:
        track_artists = set()
        for track in tracks:
            track_artist = row_get(track, 'artist', '')
            if track_artist and track_artist.lower() != artist.lower():
                track_artists.add(track_artist.lower())
        
        # If we found 3+ different artists on this album, it's likely a compilation
        if len(track_artists) >= 3:
            log_debug(f'Compilation likely for "{album}": found {len(track_artists)} distinct artists ({", ".join(list(track_artists)[:3])}...)')
            return True
    except Exception as e:
        log_debug(f'Error checking track artists for compilation detection: {e}')
    
    return False


def detect_greatest_hits_album(album: str, artist: str, conn: sqlite3.Connection, album_tracks: list = None) -> bool:
    """
    Detect if an album is a greatest hits compilation.
    
    Checks:
    1. Album name contains greatest hits patterns
    2. Album's average popularity is significantly higher than artist's median (if tracks provided)
    
    Args:
        album: Album name
        artist: Artist name
        conn: Database connection for artist stats lookup
        album_tracks: Optional list of tracks with popularity_score for verification
        
    Returns:
        True if album appears to be a greatest hits compilation, False otherwise
    """
    album_lower = album.lower()
    
    # Check for greatest hits patterns in album name
    greatest_hits_patterns = [
        'greatest hits',
        'best of',
        'the best',
        'collection',
        'anthology',
        'essentials',
        'hits',
        'singles',
        'the very best',
        'gold',
        'platinum',
        'ultimate collection',
        'complete',
        'definitive'
    ]
    
    for pattern in greatest_hits_patterns:
        if pattern in album_lower:
            log_debug(f'Greatest hits pattern detected in album name: "{album}" contains "{pattern}"')
            
            # If we have tracks, verify with popularity check
            if album_tracks:
                try:
                    # Get artist's median popularity for comparison
                    is_pg = is_postgres_connection(conn)
                    placeholder = "%s" if is_pg else "?"
                    cursor = conn.cursor()
                    cursor.execute(f"""
                        SELECT AVG(popularity_score) as avg_pop, COUNT(*) as track_count
                        FROM tracks
                        WHERE artist = {placeholder} AND popularity_score > 0
                    """, (artist,))
                    row = cursor.fetchone()
                    
                    if row and row[0] and row[1] > 10:  # Need at least 10 tracks for meaningful comparison
                        artist_avg_pop = row[0]
                        
                        # Calculate this album's average popularity
                        album_pops = [t.get('popularity_score', 0) for t in album_tracks if t.get('popularity_score')]
                        if album_pops:
                            album_avg_pop = sum(album_pops) / len(album_pops)
                            
                            # If album average is 30%+ higher than artist average, it's likely greatest hits
                            if album_avg_pop > (artist_avg_pop * 1.3):
                                log_info(f'Greatest hits confirmed: "{album}" avg popularity ({album_avg_pop:.1f}) is {(album_avg_pop/artist_avg_pop - 1)*100:.0f}% higher than artist average ({artist_avg_pop:.1f})')
                                return True
                            else:
                                log_debug(f'Greatest hits name pattern but normal popularity: album={album_avg_pop:.1f} vs artist={artist_avg_pop:.1f}')
                except Exception as e:
                    log_debug(f'Error verifying greatest hits with popularity check: {e}')
            
            # Name pattern alone is strong indicator
            return True
    
    return False


def should_skip_spotify_lookup(track_id: str, conn: sqlite3.Connection) -> bool:
    """
    Check if Spotify lookup should be skipped based on 24-hour cache.
    
    Returns True if:
    - Track has last_spotify_lookup timestamp
    - Timestamp is less than 24 hours old
    - Track has valid popularity_score in database
    
    Args:
        track_id: Track ID to check
        conn: Database connection
        
    Returns:
        True if lookup should be skipped (use cached data), False otherwise
    """
    try:
        is_pg = is_postgres_connection(conn)
        placeholder = "%s" if is_pg else "?"
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT last_spotify_lookup, popularity_score 
            FROM tracks 
            WHERE id = {placeholder}
        """, (track_id,))
        row = cursor.fetchone()
        
        if not row or not row[0]:
            # No cached lookup timestamp
            return False
        
        last_lookup_str = row[0]
        popularity_score = row[1]
        
        # Check if we have a valid popularity score (None or 0 means no valid data)
        if popularity_score is None or popularity_score <= 0:
            return False
        
        # Parse timestamp and check if it's less than 24 hours old
        try:
            last_lookup = datetime.fromisoformat(last_lookup_str)
            age = datetime.now() - last_lookup
            
            if age < timedelta(hours=24):
                log_verbose(f"   Using cached Spotify data (age: {age.total_seconds() / 3600:.1f}h)")
                return True
        except (ValueError, TypeError) as e:
            log_verbose(f"   Invalid timestamp format: {last_lookup_str} ({e})")
            return False
        
        return False
    except Exception as e:
        log_verbose(f"   Error checking Spotify cache: {e}")
        return False


def row_get(row, key, default=None):
    """
    Get a value from a sqlite3.Row object with a default fallback.
    
    sqlite3.Row objects don't have a .get() method like dictionaries,
    so this helper provides similar functionality.
    
    Args:
        row: sqlite3.Row object
        key: Column name to retrieve
        default: Default value if key doesn't exist or value is None
        
    Returns:
        Value from row or default
    """
    try:
        value = row[key]
        # Return default if value is None (NULL in database)
        return value if value is not None else default
    except (KeyError, IndexError):
        return default


def get_cache_duration_hours(track_year: int = None) -> int:
    """
    Determine cache duration based on track age.
    
    Older albums change less frequently, so we can cache longer:
    - Albums > 3 years old: 7 days (168 hours)
    - Albums 1-3 years old: 3 days (72 hours)
    - Recent albums < 1 year: 24 hours
    - No year data: 24 hours (conservative)
    
    Args:
        track_year: Year the track was released
        
    Returns:
        Cache duration in hours
    """
    if not track_year:
        return 24  # Default: 24 hours
    
    try:
        current_year = datetime.now().year
        age_years = current_year - int(track_year)
        
        if age_years >= 3:
            return 168  # 7 days for albums over 3 years old
        elif age_years >= 1:
            return 72   # 3 days for albums 1-3 years old
        else:
            return 24   # 24 hours for recent albums
    except (ValueError, TypeError):
        return 24  # Default on error


def should_use_cached_score(track: sqlite3.Row, cache_field: str, last_lookup_field: str = 'last_spotify_lookup') -> bool:
    """
    Check if a cached API score should be reused instead of fetching from API.
    
    Uses age-based cache duration - older albums are cached longer.
    
    Args:
        track: Track row (sqlite3.Row) with cached values
        cache_field: Name of the field containing cached score
        last_lookup_field: Name of the field containing last lookup timestamp
        
    Returns:
        True if cached value should be used, False if API lookup needed
    """
    try:
        cached_value = row_get(track, cache_field)
        last_lookup = row_get(track, last_lookup_field)
        
        # No cached data available
        if not cached_value or cached_value <= 0:
            return False
        
        if not last_lookup:
            return False
        
        # Parse timestamp and check age
        try:
            last_lookup_time = datetime.fromisoformat(last_lookup)
            age = datetime.now() - last_lookup_time
            
            # Determine cache duration based on track year
            cache_duration_hours = get_cache_duration_hours(row_get(track, 'year'))
            
            if age < timedelta(hours=cache_duration_hours):
                log_debug(f"Using cached {cache_field} (age: {age.total_seconds() / 3600:.1f}h, limit: {cache_duration_hours}h)")
                return True
        except (ValueError, TypeError) as e:
            log_debug(f"Invalid timestamp in {last_lookup_field}: {last_lookup} ({e})")
            return False
        
        return False
    except Exception as e:
        log_debug(f"Error checking cache for {cache_field}: {e}")
        return False


def calculate_artist_popularity_stats(artist_name: str, conn: sqlite3.Connection) -> dict:
    """
    Calculate artist-level popularity statistics from all albums.
    
    This helps identify underperforming albums/singles within an artist's catalog
    AND identify tracks that are standout popular even if not singles.
    
    NOTE: Filters out live/remix/alternate versions to ensure statistics reflect
    the core catalog and are not skewed by bonus tracks or alternate versions.
    
    Args:
        artist_name: Name of the artist
        conn: Database connection
        
    Returns:
        Dict with keys:
        - avg_popularity: Average popularity across all tracks
        - median_popularity: Median popularity
        - stddev_popularity: Standard deviation
        - track_count: Total tracks analyzed
        - top_15_percentile: Popularity threshold for top 15% of artist's tracks
        - top_20_percentile: Popularity threshold for top 20% of artist's tracks
    """
    try:
        is_pg = is_postgres_connection(conn)
        placeholder = "%s" if is_pg else "?"
        cursor = conn.cursor()
        
        # Try to get album column if it exists, otherwise use empty string
        # This ensures backward compatibility with databases that don't have album column
        try:
            cursor.execute(f"""
                SELECT popularity_score, title, album
                FROM tracks 
                WHERE COALESCE(NULLIF(album_artist, ''), artist) = {placeholder} AND popularity_score > 0
            """, (artist_name,))
            rows = cursor.fetchall()
            has_album_column = True
        except sqlite3.OperationalError as e:
            # Fallback: album column doesn't exist (OperationalError: no such column: album)
            # Only handle the specific "no such column" error
            if "no such column" in str(e).lower():
                cursor.execute(f"""
                    SELECT popularity_score, title
                    FROM tracks 
                    WHERE COALESCE(NULLIF(album_artist, ''), artist) = {placeholder} AND popularity_score > 0
                """, (artist_name,))
                rows = cursor.fetchall()
                has_album_column = False
            else:
                # Re-raise if it's a different OperationalError
                raise
        
        # Filter out live/remix/alternate tracks before calculating statistics
        scores = []
        for row in rows:
            popularity_score = row[0]
            title = row[1] if row[1] else ""
            album = row[2] if (has_album_column and row[2]) else ""
            
            # Exclude live/remix/alternate versions from artist statistics
            if not should_exclude_track_from_stats(title, album):
                scores.append(popularity_score)
        
        if not scores:
            return {
                'avg_popularity': 0,
                'median_popularity': 0,
                'stddev_popularity': 0,
                'track_count': 0,
                'top_15_percentile': 0,
                'top_20_percentile': 0
            }
        
        # Sort scores to calculate percentiles
        sorted_scores = sorted(scores, reverse=True)
        track_count = len(sorted_scores)
        
        # Calculate percentile thresholds
        # Top 15%: This is approximately 85th percentile (top artists of artist's work)
        # Top 20%: This is approximately 80th percentile (broader standout tracks)
        top_15_index = max(0, int(track_count * 0.15) - 1)  # -1 for 0-based index
        top_20_index = max(0, int(track_count * 0.20) - 1)
        
        top_15_threshold = sorted_scores[top_15_index] if top_15_index < track_count else 0
        top_20_threshold = sorted_scores[top_20_index] if top_20_index < track_count else 0
        
        # Calculate MAD (Median Absolute Deviation)
        # MAD is more robust to outliers than standard deviation
        median_val = median(scores)
        absolute_deviations = [abs(score - median_val) for score in scores]
        mad_raw = median(absolute_deviations)
        # Scale MAD to be comparable to standard deviation (1.4826 is the constant for normal distribution)
        mad_scaled = mad_raw * 1.4826 if mad_raw > 0 else 0
        
        return {
            'avg_popularity': mean(scores),
            'median_popularity': median(scores),
            'stddev_popularity': stdev(scores) if len(scores) > 1 else 0,
            'mad_popularity': mad_scaled,  # NEW: MAD for robust z-score calculation
            'track_count': len(scores),
            'top_15_percentile': top_15_threshold,  # Top 15% of artist's tracks
            'top_20_percentile': top_20_threshold   # Top 20% of artist's tracks
        }
    except Exception as e:
        log_verbose(f"   Error calculating artist stats: {e}")
        return {
            'avg_popularity': 0,
            'median_popularity': 0,
            'stddev_popularity': 0,
            'mad_popularity': 0,
            'track_count': 0,
            'top_15_percentile': 0,
            'top_20_percentile': 0
        }


def should_exclude_from_stats(tracks_with_scores, alternate_takes_map: dict = None):
    r"""
    Identify tracks that should be excluded from popularity statistics calculation.
    
    Excludes tracks at the end of an album whose titles end with a parenthesized suffix
    (e.g., "Track Title (Single)", "Track Title (Live in Wacken 2022)"), as these 
    bonus/alternate versions can skew the popularity mean, standard deviation, z-scores,
    and top 50% calculations.
    
    NEW: Also excludes tracks marked as alternate takes (via alternate_takes_map).
    
    Excluded tracks are NOT included in:
        - Mean calculation for the album
        - Standard deviation calculation
        - Z-score calculation
        - Top 50% z-score calculation (used for medium confidence threshold)
    
    A track is excluded if:
        - It appears after the last "normal" track, AND
        - The title matches the pattern: `^.*\([^)]*\)$`
        OR
        - It is marked as an alternate take in alternate_takes_map
    
    Args:
        tracks_with_scores: List of track dictionaries ordered by popularity (descending)
        alternate_takes_map: Optional dict mapping track_id -> base_track_id for alternate takes
        
    Returns:
        Set of track indices to exclude from statistics
    """
    
    if not tracks_with_scores or len(tracks_with_scores) < 3:
        # Don't filter albums with too few tracks
        return set()
    
    excluded_indices = set()
    
    # Exclude tracks marked as alternate takes
    if alternate_takes_map:
        for i, track in enumerate(tracks_with_scores):
            track_id = track["id"]
            if track_id and track_id in alternate_takes_map:
                excluded_indices.add(i)
                log_verbose(f"   Excluding alternate take from stats: {track['title']}")
    
    # Check for titles ending with parenthesized suffix
    # Pattern: ^.*\([^)]*\)$ - matches titles that end with (something)
    # Tracks are ordered by popularity DESC, so the end of album (low popularity) is at the end of the list
    tracks_with_suffix = []
    for i, track in enumerate(tracks_with_scores):
        title = track["title"] or ""
        # Check if title ends with a parenthesized suffix
        if re.match(r'^.*\([^)]*\)$', title):
            tracks_with_suffix.append(i)
    
    # Only exclude if we have multiple tracks with suffix
    if len(tracks_with_suffix) < 2:
        return excluded_indices
    
    # Find consecutive tracks with suffix at the END of the track list
    # Since tracks are sorted by popularity DESC, the last indices are the end of the album
    tracks_with_suffix_set = set(tracks_with_suffix)  # O(1) membership testing
    
    # Build a list of consecutive tracks starting from the last track index
    consecutive_at_end = []
    last_track_idx = len(tracks_with_scores) - 1
    
    # Start from the last track and work backwards
    for i in range(last_track_idx, -1, -1):
        if i in tracks_with_suffix_set:
            # This track has suffix
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
            # We've started building a sequence but hit a track without suffix
            # This means the sequence is not at the end
            break
    
    # Only exclude if we have at least 2 consecutive tracks with suffix at the end
    if len(consecutive_at_end) >= 2:
        excluded_indices.update(consecutive_at_end)
    
    return excluded_indices


def get_metadata_sources_info(single_sources):
    """
    Extract metadata information from single_sources list.
    
    Args:
        single_sources: List of sources (e.g., ["discogs", "spotify"])
        
    Returns:
        Dictionary with:
            - has_discogs: bool
            - has_spotify: bool
            - has_musicbrainz: bool
            - has_lastfm: bool
            - has_version_count: bool
            - has_metadata: bool (any metadata source, excluding score-based indicators)
            - sources_list: list of display names
    """
    has_discogs = "discogs" in single_sources or "discogs_video" in single_sources
    has_spotify = "spotify" in single_sources
    has_musicbrainz = "musicbrainz" in single_sources
    has_lastfm = "lastfm" in single_sources
    has_version_count = "version_count" in single_sources
    
    # Exclude score-based indicators from metadata confirmation
    # Allowed metadata sources: discogs, spotify, musicbrainz, lastfm
    # Excluded: z-score, popularity_zscore, score (these are popularity inference indicators, not metadata)
    has_metadata = has_discogs or has_spotify or has_musicbrainz or has_lastfm
    
    sources_list = []
    if has_discogs:
        sources_list.append("Discogs")
    if has_spotify:
        sources_list.append("Spotify")
    if has_musicbrainz:
        sources_list.append("MusicBrainz")
    if has_lastfm:
        sources_list.append("Last.fm")
    if has_version_count:
        sources_list.append("Version Count")
    
    return {
        'has_discogs': has_discogs,
        'has_spotify': has_spotify,
        'has_musicbrainz': has_musicbrainz,
        'has_lastfm': has_lastfm,
        'has_version_count': has_version_count,
        'has_metadata': has_metadata,
        'sources_list': sources_list
    }


def normalize_genre(genre):
    """
    Normalize genre names to avoid duplicates and inconsistencies.
    """
    genre = genre.lower().strip()
    synonyms = {
        "hip hop": "hip-hop",
        "r&b": "rnb"
    }
    return synonyms.get(genre, genre)


def clean_conflicting_genres(genres):
    """
    Remove conflicting or irrelevant genres based on dominant tags.
    Example: If 'punk' exists, drop 'electronic'.
    """
    genres_lower = [g.lower() for g in genres]

    # If punk dominates, remove electronic/electro
    if any("punk" in g for g in genres_lower):
        genres_lower = [g for g in genres_lower if g not in ["electronic", "electro"]]

    # If metal dominates, remove electronic
    if any("metal" in g for g in genres_lower):
        genres_lower = [g for g in genres_lower if g not in ["electronic", "electro"]]

    # Remove generic tags if specific ones exist
    if any("progressive metal" in g for g in genres_lower):
        genres_lower = [g for g in genres_lower if g not in ["metal", "heavy metal"]]

    return genres_lower


def get_top_genres_with_navidrome(sources, nav_genres, title="", album=""):
    """
    Combine online-sourced genres with Navidrome genres for comparison.
    Uses weighted scoring, contextual filtering, and deduplication.
    
    Args:
        sources: Dict of {source_name: [genres]} from various APIs
        nav_genres: List of genres from Navidrome
        title: Track title for contextual boosts
        album: Album name for contextual boosts
        
    Returns:
        Tuple of (online_top_genres, navidrome_cleaned_genres)
    """
    from collections import defaultdict

    genre_scores = defaultdict(float)

    # Aggregate weighted genres from sources
    for source, genres in sources.items():
        weight = GENRE_WEIGHTS.get(source, 0)
        for genre in genres:
            norm = normalize_genre(genre)
            genre_scores[norm] += weight

    # Apply contextual boosts
    if "live" in title.lower() or "live" in album.lower():
        genre_scores["live"] += 0.5
    if any(word in title.lower() or word in album.lower() for word in ["christmas", "xmas"]):
        genre_scores["christmas"] += 0.5

    # Sort by weighted score
    sorted_genres = sorted(genre_scores.items(), key=lambda x: x[1], reverse=True)
    filtered = [g for g, _ in sorted_genres]

    # Contextual filtering
    filtered = clean_conflicting_genres(filtered)

    # Deduplicate and normalize
    filtered = list(dict.fromkeys(filtered))

    # Remove "heavy metal" if other metal sub-genres exist
    metal_subgenres = [g for g in filtered if "metal" in g.lower() and g.lower() != "heavy metal"]
    if metal_subgenres:
        filtered = [g for g in filtered if g.lower() != "heavy metal"]

    # Fallback if filtering removes everything
    if not filtered:
        filtered = [g for g, _ in sorted_genres]

    # Pick top 3
    online_top = [g.capitalize() for g in filtered[:3]]

    # Clean Navidrome genres
    nav_cleaned = [normalize_genre(g).capitalize() for g in nav_genres if g]

    return online_top, nav_cleaned


# Timeout configuration for API calls (in seconds)
API_CALL_TIMEOUT = int(os.environ.get("POPULARITY_API_TIMEOUT", "30"))

# Comprehensive metadata timeout (allows for parallel API calls)
# With parallel fetching: track_meta (~30.5s) + max(audio, artist, album) (~30.5s) = ~61s
# Setting to 90s provides buffer for retries and network latency
COMPREHENSIVE_METADATA_TIMEOUT = int(os.environ.get("COMPREHENSIVE_METADATA_TIMEOUT", "5"))

# Discogs API rate limiting constants
_DISCOGS_LAST_REQUEST_TIME = 0
_DISCOGS_MIN_INTERVAL = 0.35


def detect_cover_and_normalize_title(title: str) -> tuple[bool, str]:
    """
    Detect if a track is a cover song based on title patterns and return normalized title.
    
    Detects patterns like:
    - "Song Title (Artist Cover)"
    - "Song Title [Artist Cover]"
    - "Song Title (Cover)"
    - "Song Title [Cover]"
    
    Args:
        title: Original track title
        
    Returns:
        Tuple of (is_cover: bool, normalized_title: str)
        - is_cover: True if title indicates cover version
        - normalized_title: Title with cover notation stripped for API lookups
    """
    # Check for cover patterns in parentheses or brackets
    cover_pattern = r'\s*[\(\[](?:.*?\s)?[Cc]over[\)\]]'
    
    is_cover = bool(re.search(cover_pattern, title))
    
    # Normalize title by removing cover notation for API lookups
    normalized_title = re.sub(cover_pattern, '', title).strip()
    
    return is_cover, normalized_title


def _throttle_discogs():
    """Respect Discogs rate limit (1 request per 0.35 seconds per token)."""
    global _DISCOGS_LAST_REQUEST_TIME
    elapsed = time.time() - _DISCOGS_LAST_REQUEST_TIME
    if elapsed < _DISCOGS_MIN_INTERVAL:
        time.sleep(_DISCOGS_MIN_INTERVAL - elapsed)
    _DISCOGS_LAST_REQUEST_TIME = time.time()


def _get_discogs_session():
    """
    Get or create a requests session for Discogs API calls.
    Returns the shared session from api_clients module.
    """
    return session


def _discogs_search(session, headers, query, kind="release", per_page=15, timeout=(5, 10)):
    """
    Search Discogs database.
    
    Args:
        session: requests.Session object
        headers: Dict with User-Agent and optional Authorization headers
        query: Search query string
        kind: Type of search (release, master, artist, label)
        per_page: Number of results per page (max 100)
        timeout: Request timeout tuple (connect, read) or single value
        
    Returns:
        List of search results from Discogs API
        
    Raises:
        Exception on API errors or rate limiting
    """
    _throttle_discogs()
    
    search_url = "https://api.discogs.com/database/search"
    params = {
        "q": query,
        "type": kind,
        "per_page": min(per_page, 100)
    }
    
    try:
        response = session.get(search_url, headers=headers, params=params, timeout=timeout)
        
        # Handle rate limiting
        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", 60))
            logger.warning(f"Discogs rate limit hit, sleeping for {retry_after} seconds")
            time.sleep(retry_after)
            # Retry once after rate limit
            _throttle_discogs()
            response = session.get(search_url, headers=headers, params=params, timeout=timeout)
        
        response.raise_for_status()
        data = response.json()
        results = data.get("results", [])
        
        logger.debug(f"Discogs search for '{query}' returned {len(results)} results")
        return results
        
    except Exception as e:
        logger.error(f"Discogs search failed for query '{query}': {e}")
        raise


# Shared thread pool for timeout enforcement (prevents resource exhaustion)
# Using a larger pool to handle multiple concurrent API calls without blocking.
# Increased from 10 to 20 to reduce risk of thread pool exhaustion when API calls
# with retry logic occupy threads longer than the _run_with_timeout() timeout.
# Example: API_CALL_TIMEOUT=30s, but HTTP request with 3 retries can take 46-61s.
_timeout_executor = ThreadPoolExecutor(max_workers=20, thread_name_prefix="api_timeout")


def _cleanup_timeout_executor():
    """Cleanup function to shutdown the timeout executor gracefully."""
    global _timeout_executor
    if _timeout_executor:
        _timeout_executor.shutdown(wait=False)
        _timeout_executor = None


# Register cleanup handler to shutdown executor on exit
atexit.register(_cleanup_timeout_executor)


class TimeoutError(Exception):
    """Raised when an API call exceeds the timeout limit"""
    pass


def _run_with_timeout(func, timeout_seconds, error_message, *args, **kwargs):
    """
    Execute a function with a timeout using a shared ThreadPoolExecutor.
    
    This is thread-safe and works in background threads (unlike signal-based timeout).
    Uses a shared thread pool to prevent resource exhaustion from creating new
    executors for each call.
    
    Args:
        func: Function to execute
        timeout_seconds: Timeout in seconds
        error_message: Error message if timeout occurs
        *args: Positional arguments for func
        **kwargs: Keyword arguments for func
    
    Returns:
        Result of func(*args, **kwargs)
    
    Raises:
        TimeoutError: If execution exceeds timeout_seconds
    
    Note:
        Tasks that timeout will continue running in the background until completion
        or until the executor shuts down. This can lead to thread pool exhaustion
        if API calls hang for extended periods despite having their own timeouts.
        
        To mitigate this, the api_clients module provides timeout_safe_session with
        reduced retry counts. Future enhancement: modify API clients to use this
        session for calls made within _run_with_timeout.
    """
    global _timeout_executor
    if _timeout_executor is None:
        raise RuntimeError("Timeout executor has been shut down")
    
    log_verbose(f"[TIMEOUT DEBUG] Submitting task {func.__name__} with timeout {timeout_seconds}s")
    future = _timeout_executor.submit(func, *args, **kwargs)
    log_verbose(f"[TIMEOUT DEBUG] Task submitted, waiting for result...")
    try:
        result = future.result(timeout=timeout_seconds)
        log_verbose(f"[TIMEOUT DEBUG] Task completed successfully")
        return result
    except concurrent.futures.TimeoutError:
        # Task will continue running in the background but we won't wait for it.
        # WARNING: This can lead to thread pool exhaustion if many tasks timeout
        # and continue running. Monitor thread pool health if this happens frequently.
        log_verbose(f"[TIMEOUT DEBUG] Task timed out after {timeout_seconds}s, continuing in background")
        raise TimeoutError(error_message)


@contextmanager
def api_timeout(seconds: int, error_message: str = "API call timed out"):
    """
    Context manager for API timeout enforcement (no-op for backwards compatibility).
    
    Note: This is kept for backwards compatibility but doesn't enforce timeouts.
    Use _run_with_timeout() function for actual timeout enforcement on API calls.
    
    Args:
        seconds: Timeout in seconds (ignored)
        error_message: Error message (ignored)
    """
    yield


# Legacy configuration for backward compatibility
VERBOSE = (
    os.environ.get("SPTNR_VERBOSE_POPULARITY") or os.environ.get("SPTNR_VERBOSE") or "0"
) == "1"
# Force rescan of albums even if they were already scanned
FORCE_RESCAN = os.environ.get("SPTNR_FORCE_RESCAN", "0") == "1"

# Legacy logging functions - now redirected to centralized logging
def log_basic(msg):
    """Legacy function - logs to info.log"""
    log_info(msg)

def log_verbose(msg):
    """Legacy function - logs to debug.log"""
    if VERBOSE:
        log_debug(msg)




DB_PATH = os.environ.get("DB_PATH", "/database/sptnr.db")
POPULARITY_PROGRESS_FILE = os.environ.get("POPULARITY_PROGRESS_FILE", "/database/popularity_scan_progress.json")
NAVIDROME_PROGRESS_FILE = os.environ.get("NAVIDROME_PROGRESS_FILE", "/database/navidrome_scan_progress.json")
PG_HOST = os.environ.get("PG_HOST", "")
PG_PORT = int(os.environ.get("PG_PORT", 5432))
PG_USER = os.environ.get("PG_USER", "")
PG_PASSWORD = os.environ.get("PG_PASSWORD", "")
PG_DATABASE = os.environ.get("PG_DATABASE", "")
from popularity_helpers import (
    get_spotify_artist_id,
    search_spotify_track,
    get_lastfm_track_info,
    calculate_lastfm_popularity_score,
    score_by_age,
    update_artist_id_for_artist,
    get_lastfm_client,
    SPOTIFY_WEIGHT,
    LASTFM_WEIGHT,
    LISTENBRAINZ_WEIGHT,
    AGE_WEIGHT,
)
from helpers.api_rate_limiter import get_rate_limiter

# Import scan history tracker
try:
    from scan_history import log_album_scan, was_album_scanned  # type: ignore
except ImportError:
    def log_album_scan(*args, **kwargs):  # type: ignore
        pass  # Fallback if scan_history not available
    def was_album_scanned(*args, **kwargs):  # type: ignore
        return False  # Fallback if scan_history not available


# ============================================================================
# Helper Functions for Two-Stage Single Detection Filtering
# ============================================================================

def calculate_album_stats(conn, artist: str, album: str) -> tuple:
    """
    Calculate album popularity statistics for album-level filtering.
    
    Returns:
        Tuple of (mean, stddev, median, count)
    """
    is_pg = is_postgres_connection(conn)
    placeholder = "%s" if is_pg else "?"
    cursor = conn.cursor()
    cursor.execute(f"""
        SELECT popularity_score
        FROM tracks
        WHERE artist = {placeholder} AND album = {placeholder} AND popularity_score > 0
    """, (artist, album))
    
    popularities = [row[0] for row in cursor.fetchall()]
    
    if len(popularities) < 2:
        return 0.0, 0.0, 0.0, len(popularities)
    
    album_median = median(popularities)
    album_stddev = stdev(popularities)
    
    return album_median, album_stddev, album_median, len(popularities)


def calculate_artist_stats(conn, artist: str) -> tuple:
    """
    Calculate artist-level popularity statistics across entire catalogue.
    
    Returns:
        Tuple of (mean, stddev, count)
    """
    is_pg = is_postgres_connection(conn)
    placeholder = "%s" if is_pg else "?"
    cursor = conn.cursor()
    cursor.execute(f"""
        SELECT popularity_score
        FROM tracks
        WHERE artist = {placeholder} AND popularity_score > 0
    """, (artist,))
    
    popularities = [row[0] for row in cursor.fetchall()]
    
    if len(popularities) < 2:
        return 0.0, 0.0, len(popularities)
    
    artist_mean = mean(popularities)
    artist_stddev = stdev(popularities)
    
    return artist_mean, artist_stddev, len(popularities)


# --- DEBUG: Test log_unified and print log path ---
if __name__ == "__main__":
    try:
        log_unified("TEST ENTRY: log_unified() at script start")
    except Exception as e:
        print("log_unified() test failed:", e)

def get_db_connection():
    """Get database connection (PostgreSQL if configured, else SQLite)."""
    # Check if PostgreSQL is configured
    if PG_HOST and PG_USER and PG_DATABASE:
        # Connect to PostgreSQL
        if psycopg2 is None:
            raise RuntimeError("psycopg2 not available - install with: pip install psycopg2-binary")
        try:
            conn = psycopg2.connect(
                host=PG_HOST,
                port=PG_PORT,
                user=PG_USER,
                password=PG_PASSWORD,
                dbname=PG_DATABASE,
                cursor_factory=psycopg2.extras.RealDictCursor
            )
            return conn
        except Exception as e:
            log_debug(f"PostgreSQL connection failed, falling back to SQLite: {e}")
    
    # Fallback to SQLite
    conn = sqlite3.connect(DB_PATH, timeout=120.0, isolation_level='DEFERRED')
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout = 120000")  # 120 seconds
    conn.execute("PRAGMA synchronous = NORMAL")  # Reduces fsync calls, improves throughput with WAL
    conn.execute("PRAGMA wal_autocheckpoint = 1000")  # More aggressive checkpointing
    conn.row_factory = sqlite3.Row
    return conn


def _navidrome_scan_running() -> bool:
    """Return True if Navidrome scan progress file says a scan is running."""
    try:
        if os.path.exists(NAVIDROME_PROGRESS_FILE):
            with open(NAVIDROME_PROGRESS_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
                return bool(state.get("is_running"))
    except Exception as e:
        log_verbose(f"Could not read Navidrome progress file: {e}")
    return False

def sync_track_rating_to_navidrome(track_id: str, stars: int) -> bool:
    """
    Sync a single track rating to Navidrome using the Subsonic API.
    
    Args:
        track_id: Navidrome track ID
        stars: Star rating (1-5)
        
    Returns:
        True if successful, False otherwise
    """
    try:
        # Get Navidrome credentials from environment first, then fall back to config file
        nav_url = os.environ.get("NAV_BASE_URL", "").strip("/")
        nav_user = os.environ.get("NAV_USER", "")
        nav_pass = os.environ.get("NAV_PASS", "")
        
        # If not in environment, try loading from config file
        if not all([nav_url, nav_user, nav_pass]):
            try:
                config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")
                with open(config_path, 'r') as f:
                    config = yaml.safe_load(f)
                
                # Try navidrome_users first (multi-user config)
                nav_users = config.get('navidrome_users', [])
                if nav_users and len(nav_users) > 0:
                    first_user = nav_users[0]
                    nav_url = first_user.get('base_url', '').strip('/')
                    nav_user = first_user.get('user', '')
                    nav_pass = first_user.get('pass', '')
                else:
                    # Fall back to single navidrome config
                    nav_config = config.get('navidrome', {})
                    nav_url = nav_config.get('base_url', '').strip('/')
                    nav_user = nav_config.get('user', '')
                    nav_pass = nav_config.get('pass', '')
            except Exception as e:
                log_verbose(f"Failed to load Navidrome config from file: {e}")
                return False
        
        if not all([nav_url, nav_user, nav_pass]):
            log_verbose("Navidrome credentials not configured, skipping rating sync")
            return False
        
        # Build Subsonic API parameters
        params = {
            "u": nav_user,
            "p": nav_pass,
            "v": "1.16.1",
            "c": "sptnr",
            "f": "json",
            "id": track_id,
            "rating": stars
        }
        
        # Call setRating API
        response = session.get(f"{nav_url}/rest/setRating.view", params=params, timeout=10)
        response.raise_for_status()
        
        # Check if response indicates success
        result = response.json()
        if result.get("subsonic-response", {}).get("status") == "ok":
            return True
        else:
            error_msg = result.get("subsonic-response", {}).get("error", {}).get("message", "Unknown error")
            log_basic(f"Navidrome API error for track {track_id}: {error_msg}")
            return False
            
    except Exception as e:
        log_basic(f"Failed to sync rating to Navidrome for track {track_id}: {e}")
        return False

def save_popularity_progress(processed_artists: int, total_artists: int, current_artist: str = None):
    """Save popularity scan progress to file"""
    try:
        progress_data = {
            "is_running": True,
            "scan_type": "popularity_scan",
            "processed_artists": processed_artists,
            "total_artists": total_artists,
            "percent_complete": int((processed_artists / total_artists * 100)) if total_artists > 0 else 0,
            "current_artist": current_artist,
            "last_updated": datetime.now().isoformat()
        }
        with open(POPULARITY_PROGRESS_FILE, 'w') as f:
            json.dump(progress_data, f)
    except Exception as e:
        log_basic(f"Error saving popularity progress: {e}")


def get_resume_artist_from_db():
    """
    Get the last artist that was scanned from the database scan history.
    This allows resuming a popularity scan from where it left off.
    Returns the artist name if found, None otherwise.
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Get the most recently scanned artist from scan_history table
        cursor.execute("""
            SELECT artist_name, MAX(scan_timestamp) as last_scan
            FROM scan_history
            WHERE scan_type = 'popularity'
            GROUP BY artist_name
            ORDER BY last_scan DESC
            LIMIT 1
        """)
        
        result = cursor.fetchone()
        conn.close()
        
        if result and result[0]:
            return result[0]
        return None
    except Exception as e:
        log_basic(f"Error getting resume artist from database: {e}")
        return None


def fetch_album_art_from_audiodb(artist: str, album: str) -> str | None:
    """
    Fetch album art URL from AudioDB as fallback source.
    
    Args:
        artist: Artist name
        album: Album name
        
    Returns:
        Album art URL if found, None otherwise
    """
    try:
        from api_clients.audiodb import get_album_artwork
        art_url = get_album_artwork(artist, album, enabled=True)
        if art_url:
            log_debug(f"[ALBUM_ART_FALLBACK] Found album art via AudioDB for {artist} - {album}")
        return art_url
    except Exception as e:
        log_debug(f"[ALBUM_ART_FALLBACK] AudioDB lookup failed for {artist} - {album}: {e}")
        return None


def fetch_album_art_from_discogs(artist: str, album: str, discogs_token: str = None) -> str | None:
    """
    Fetch album art URL from Discogs as fallback source.
    
    Args:
        artist: Artist name
        album: Album name
        discogs_token: Optional Discogs API token
        
    Returns:
        Album art URL if found, None otherwise
    """
    try:
        from api_clients.discogs import DiscogsClient
        
        if not discogs_token:
            # Try to load from config
            config_path = os.path.join(os.path.dirname(__file__), "config", "config.yaml")
            if os.path.exists(config_path):
                with open(config_path, 'r') as f:
                    config = yaml.safe_load(f) or {}
                    discogs_token = config.get("api_integrations", {}).get("discogs", {}).get("token")
        
        if not discogs_token:
            log_debug(f"[ALBUM_ART_FALLBACK] No Discogs token available, skipping Discogs lookup")
            return None
        
        # Validate token - reject placeholders
        if discogs_token.lower() in ("your_discogs_token", "your_token", "placeholder", "") or len(discogs_token) < 10:
            log_debug(f"[ALBUM_ART_FALLBACK] Discogs token is invalid or placeholder - skipping Discogs lookup")
            return None
        
        client = DiscogsClient(token=discogs_token)
        
        # Search for the album on Discogs
        search_url = "https://api.discogs.com/database/search"
        params = {
            "q": f"{album}",
            "artist": artist,
            "type": "release",
            "token": discogs_token
        }
        headers = {"User-Agent": "sptnr/1.0 (https://github.com/discogs)"}
        
        resp = requests.get(search_url, params=params, headers=headers, timeout=5)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        
        if not results:
            log_debug(f"[ALBUM_ART_FALLBACK] No Discogs release found for {artist} - {album}")
            return None
        
        # Get the first result
        release = results[0]
        thumb_url = release.get("thumb")
        
        if thumb_url and thumb_url != "":
            log_debug(f"[ALBUM_ART_FALLBACK] Found album art via Discogs for {artist} - {album}")
            return thumb_url
        
        log_debug(f"[ALBUM_ART_FALLBACK] Discogs found release but no thumbnail for {artist} - {album}")
        return None
        
    except Exception as e:
        log_debug(f"[ALBUM_ART_FALLBACK] Discogs lookup failed for {artist} - {album}: {e}")
        return None


def fetch_album_art_url_from_musicbrainz(artist: str, album: str) -> str | None:
    """
    Fetch album art URL from MusicBrainz Cover Art Archive.
    
    Args:
        artist: Artist name
        album: Album name
        
    Returns:
        Cover Art Archive URL if found, None otherwise
    """
    try:
        import requests
        
        # Try to get MBID from database using canonical column name.
        conn = get_db_connection()
        db_query = DatabaseQuery(conn)
        
        # Determine database placeholder syntax.
        is_pg = is_postgres_connection(conn)
        placeholder = "%s" if is_pg else "?"

        # Check for column existence (SQLite-specific, but needed for compatibility)
        track_columns = set()
        if not is_pg:
            cursor = db_query.execute("PRAGMA table_info(tracks)")
            track_columns = {row[1] for row in cursor.fetchall()}
        else:
            # For PostgreSQL, use information_schema
            cursor = db_query.execute("""
                SELECT column_name FROM information_schema.columns 
                WHERE table_schema = 'public' AND table_name = 'tracks'
            """)
            track_columns = {row[0] for row in cursor.fetchall()}

        mb_album_column = "musicbrainz_album_mbid" if "musicbrainz_album_mbid" in track_columns else None

        result = None
        if mb_album_column:
            cursor = db_query.execute(
                f"""
                SELECT {mb_album_column} AS album_mbid FROM tracks
                WHERE COALESCE(NULLIF(album_artist, ''), artist) = {placeholder}
                  AND album = {placeholder}
                  AND {mb_album_column} IS NOT NULL
                LIMIT 1
                """,
                (artist, album),
            )
            result = cursor.fetchone()
        conn.close()
        
        album_mbid = result['album_mbid'] if result else None
        
        # If we don't have MBID, try to search for it
        if not album_mbid:
            try:
                search_url = "https://musicbrainz.org/ws/2/release-group"
                params = {
                    "query": f'release:"{album}" AND artist:"{artist}"',
                    "fmt": "json",
                    "limit": 1
                }
                headers = {"User-Agent": "sptnr/1.0 (https://github.com/sptnr)"}
                resp = requests.get(search_url, params=params, headers=headers, timeout=3)
                resp.raise_for_status()
                data = resp.json()
                rgs = data.get("release-groups", [])
                if rgs:
                    album_mbid = rgs[0].get("id")
                    log_debug(f"[ALBUM_ART] Found MBID via search - {artist} - {album}: {album_mbid}")
            except Exception as e:
                log_debug(f"[ALBUM_ART] MusicBrainz album search failed: {e}")
                return None
        
        if not album_mbid:
            log_debug(f"[ALBUM_ART] No MBID found for {artist} - {album}")
            return None
        
        # Construct Cover Art Archive URL
        cover_url = f"https://coverartarchive.org/release-group/{album_mbid}/front-500"
        log_debug(f"[ALBUM_ART] Constructed CAA URL for {artist} - {album}: {cover_url}")
        return cover_url
        
    except Exception as e:
        log_debug(f"[ALBUM_ART] Failed to fetch album art URL from MusicBrainz: {e}")
        return None


def download_and_save_album_art(artist: str, album: str, art_url: str, conn=None, cursor=None, source: str = "unknown") -> bool:
    """
    Download album art image from URL and save to database.
    
    Args:
        artist: Artist name
        album: Album name
        art_url: URL to album art image
        conn: Optional existing database connection (avoids creating new one)
        cursor: Optional existing database cursor
        source: Source of the art URL (musicbrainz, audiodb, discogs, etc.)
        
    Returns:
        True if successfully saved, False otherwise
    """
    try:
        import requests
        
        if not art_url:
            return False
        
        # Download image from URL
        resp = requests.get(art_url, timeout=5)
        if resp.status_code != 200:
            log_debug(f"[ALBUM_ART] Failed to download image from {source} for {artist} - {album}: HTTP {resp.status_code}")
            return False
        
        image_data = resp.content
        if not image_data or len(image_data) == 0:
            log_debug(f"[ALBUM_ART] Downloaded image is empty for {artist} - {album}")
            return False
        
        # Save to database
        own_connection = False
        if conn is None:
            conn = get_db_connection()
            own_connection = True
        if cursor is None:
            cursor = conn.cursor()
        
        # Determine database type for proper placeholder syntax
        is_pg = is_postgres_connection(conn)
        placeholder = "%s" if is_pg else "?"
        
        if is_pg:
            cursor.execute("""
                INSERT INTO album_art 
                (artist_name, album_name, image_data, image_mime_type, source, downloaded_at)
                VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (artist_name, album_name)
                DO UPDATE SET
                    image_data = EXCLUDED.image_data,
                    image_mime_type = EXCLUDED.image_mime_type,
                    source = EXCLUDED.source,
                    downloaded_at = EXCLUDED.downloaded_at
            """, (artist, album, image_data, "image/jpeg", source))
        else:
            cursor.execute("""
                INSERT OR REPLACE INTO album_art 
                (artist_name, album_name, image_data, image_mime_type, source, downloaded_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """, (artist, album, image_data, "image/jpeg", source))
        
        # Only commit if we created our own connection
        if own_connection:
            conn.commit()
            conn.close()
        
        
        log_info(f"[ALBUM_ART] Successfully downloaded and saved album art for {artist} - {album} from {source} ({len(image_data)} bytes)")
        return True
        
    except requests.exceptions.Timeout:  # type: ignore
        log_debug(f"[ALBUM_ART] Timeout downloading image from {source} for {artist} - {album}")
        return False
    except Exception as e:
        log_debug(f"[ALBUM_ART] Failed to download/save album art for {artist} - {album}: {e}")
        return False


def fetch_and_save_album_art_with_fallback(artist: str, album: str, conn=None, cursor=None, discogs_token: str = None) -> bool:
    """
    Fetch album art with intelligent fallback strategy.
    
    Tries sources in this order:
    1. MusicBrainz Cover Art Archive (preferred)
    2. AudioDB
    3. Discogs (if token available)
    
    Args:
        artist: Artist name
        album: Album name
        conn: Optional existing database connection
        cursor: Optional existing database cursor
        discogs_token: Optional Discogs API token
        
    Returns:
        True if album art was successfully downloaded, False otherwise
    """
    sources = [
        ("musicbrainz", lambda: fetch_album_art_url_from_musicbrainz(artist, album)),
        ("audiodb", lambda: fetch_album_art_from_audiodb(artist, album)),
        ("discogs", lambda: fetch_album_art_from_discogs(artist, album, discogs_token)),
    ]
    
    for source_name, fetch_func in sources:
        try:
            art_url = fetch_func()
            if art_url:
                log_debug(f"[ALBUM_ART] Attempting download from {source_name}: {artist} - {album}")
                if download_and_save_album_art(artist, album, art_url, conn, cursor, source=source_name):
                    return True
                else:
                    log_debug(f"[ALBUM_ART] Download from {source_name} failed, trying next source...")
        except Exception as e:
            log_debug(f"[ALBUM_ART] {source_name} source error for {artist} - {album}: {e}")
    
    log_debug(f"[ALBUM_ART] All fallback sources exhausted for {artist} - {album}")
    return False


def detect_single_for_track(
    title: str,
    artist: str,
    album_track_count: int = 1,
    spotify_results_cache: dict = None,
    verbose: bool = False,
    discogs_token: str = None,
    # New parameters for advanced detection
    track_id: str = None,
    album: str = None,
    isrc: str = None,
    duration: float = None,
    popularity: float = None,
    album_type: str = None,
    use_advanced_detection: bool = True,
    zscore_threshold: float = 1.0,
    # New parameters for conditional z-score detection
    album_is_underperforming: bool = False,
    artist_median_popularity: float = 0.0
) -> dict:
    """
    Detect if a track is a single using multiple data sources.
    
    This is the canonical single detection logic used by popularity.py.
    Other modules should call this function to ensure consistent behavior.
    
    NEW: Enhanced with advanced single detection logic including:
    - ISRC-based track version matching
    - Title+duration matching (Â±2 seconds)
    - Alternate version filtering
    - Live/unplugged context handling
    - Album release deduplication
    - Global popularity calculation
    - Z-score based final determination
    - Compilation/greatest hits special handling
    
    Args:
        title: Track title
        artist: Artist name
        album_track_count: Number of tracks on the album (for context-based confidence)
        spotify_results_cache: Optional dict mapping title to Spotify search results
        verbose: Enable verbose logging
        discogs_token: Optional Discogs API token (will load from config if not provided)
        track_id: Track ID for advanced detection (optional)
        album: Album name for advanced detection (optional)
        isrc: ISRC code for advanced detection (optional)
        duration: Track duration in seconds for advanced detection (optional)
        popularity: Track popularity score for advanced detection (optional)
        album_type: Album type for advanced detection (optional)
        use_advanced_detection: Enable advanced detection logic (default True)
        zscore_threshold: Z-score threshold for singles based on artist median (default 1.0)
        
    Returns:
        Dict with keys:
            - sources: List of sources that confirmed single (e.g. ['spotify', 'musicbrainz'])
            - confidence: 'high', 'medium', or 'low'
            - is_single: True if confidence is 'high', False otherwise
            - global_popularity: Global popularity across versions (if advanced)
            - zscore: Z-score against artist median (if advanced)
            - metadata_single: Metadata single status (if advanced)
            - is_compilation: Compilation status (if advanced)
    """
    # Use enhanced detection algorithm per problem statement if enabled
    # This implements the exact 8-stage algorithm with pre-filter, Discogs primary, etc.
    log_debug(f"Advanced detection check - use_advanced_detection={use_advanced_detection}, track_id={track_id}, album={album}, title={title}, artist={artist}")
    if use_advanced_detection and track_id and album:
        conn = None
        try:
            from deprecated.single_detection_enhanced import detect_single_enhanced, store_single_detection_result
            # get_db_connection is already available in this module
            conn = get_db_connection()
            
            # Get Spotify results if cached
            spotify_search_results = None
            if spotify_results_cache is not None:
                spotify_search_results = spotify_results_cache.get(title)
            
            # Get API clients
            discogs_client = None
            if discogs_token:
                discogs_client = _get_timeout_safe_discogs_client(discogs_token)
                if discogs_client:
                    log_debug(f"[SINGLE DETECTION] Discogs client initialized for single detection")
                else:
                    log_debug(f"[SINGLE DETECTION] Discogs client initialization failed - Discogs single detection unavailable")
            else:
                log_debug(f"[SINGLE DETECTION] Discogs token not available - Discogs single detection disabled")
            
            musicbrainz_client = None
            if HAVE_MUSICBRAINZ:
                musicbrainz_client = _get_timeout_safe_musicbrainz_client()
            
            # Get Last.fm client
            lastfm_client = get_lastfm_client()
            
            # Run enhanced detection
            result = detect_single_enhanced(
                conn=conn,
                track_id=track_id,
                title=title,
                artist=artist,
                album=album,
                duration=duration,
                isrc=isrc,
                popularity=popularity or 0.0,
                spotify_results=spotify_search_results,
                discogs_client=discogs_client,
                musicbrainz_client=musicbrainz_client,
                lastfm_client=lastfm_client,
                verbose=verbose,
                album_type=album_type,
                album_is_underperforming=album_is_underperforming,
                artist_median_popularity=artist_median_popularity
            )
            
            # CRITICAL: Close the read connection before storing results.
            # detect_single_enhanced() creates multiple cursors and may leave read locks open.
            # Close the read connection, then use a fresh connection for writes.
            try:
                if conn is not None:
                    conn.close()
                    conn = None
                write_conn = get_db_connection()  # Get fresh connection for write operations
                store_single_detection_result(write_conn, track_id, result)
                write_conn.close()
            except Exception as write_error:
                log_debug(f"Warning: Could not write single detection result for {track_id}: {write_error}")
                import traceback
                log_debug(f"Write error: {traceback.format_exc()}")
            
            # Return in expected format
            # CRITICAL: Deduplicate sources to prevent same source appearing twice
            # (e.g., lastfm appearing twice due to multiple code paths)
            result['single_sources'] = list(dict.fromkeys(result['single_sources']))
            
            return {
                "sources": result['single_sources'],
                "confidence": result['single_confidence'],
                "is_single": result['is_single']
            }
        except ImportError as e:
            if verbose:
                log_unified(f"   âš  Enhanced detection module not available: {e}")
            # Fall through to standard detection
        except Exception as e:
            if verbose:
                log_unified(f"   âš  Enhanced detection failed, falling back to standard: {e}")
            import traceback
            if verbose:
                log_unified(f"   Error details: {traceback.format_exc()}")
            # Fall through to standard detection
        finally:
            if conn is not None:
                conn.close()
    else:
        # Advanced detection skipped
        skip_reason = []
        if not use_advanced_detection:
            skip_reason.append("use_advanced_detection=False")
        if not track_id:
            skip_reason.append(f"track_id={track_id}")
        if not album:
            skip_reason.append(f"album={album}")
        log_debug(f"Skipping advanced detection for {title}: {', '.join(skip_reason)}")
    
    # Ignore obvious non-singles by keywords
    # Strip cover attributions first so "Song (Live Cover)" becomes "Song (Live)" before checking
    base_title = strip_cover_attribution(title)
    if any(k in base_title.lower() for k in IGNORE_SINGLE_KEYWORDS):
        if verbose:
            log_verbose(f"   âŠ— Skipping non-single: {title} (keyword filter)")
        return {
            "sources": [],
            "confidence": "low",
            "is_single": False
        }
    
    # ALBUM-LEVEL POPULARITY FILTER (for standard detection path)
    # If album and popularity are provided, check against album mean
    if album and popularity and popularity > 0:
        try:
            conn = get_db_connection()
            is_pg = is_postgres_connection(conn)
            placeholder = "%s" if is_pg else "?"
            cursor = conn.cursor()
            
            # STAGE 1: Album-level filter (must be album standout)
            cursor.execute(f"""
                SELECT popularity_score 
                FROM tracks 
                WHERE artist = {placeholder} AND album = {placeholder} AND popularity_score > 0
            """, (artist, album))
            album_popularities = [row['popularity_score'] for row in cursor.fetchall()]
            
            album_passed = True
            if album_popularities:
                from statistics import stdev as stat_stdev, median as stat_median
                album_median = stat_median(album_popularities)
                album_stddev = stdev(album_popularities) if len(album_popularities) > 1 else 0
                
                # Must be in top 3 of album OR above album median - 0.5*stddev
                sorted_album = sorted(album_popularities, reverse=True)
                is_top_3_album = popularity in sorted_album[:3]
                album_threshold = album_median - (0.5 * album_stddev) if album_stddev > 0 else album_median
                meets_album_threshold = popularity >= album_threshold
                
                if not (is_top_3_album or meets_album_threshold):
                    if verbose:
                        log_verbose(f"   ⊗ Album filter blocked: {title} (pop {popularity:.1f}, album median {album_median:.1f})")
                    conn.close()
                    return {
                        "sources": [],
                        "confidence": "low",
                        "is_single": False,
                        "stage_blocked": "album_filter"
                    }
            
            # STAGE 2: Artist-level filter (must be artist standout)
            # Skip this filter for compilations and greatest hits albums
            is_compilation_or_greatest_hits = album_type in ["various_artists", "greatest_hits", "compilation"]
            
            if is_compilation_or_greatest_hits:
                if verbose:
                    log_verbose(f"   ⓘ Skipping artist z-score filter for compilation/greatest hits album")
            else:
                cursor.execute(f"""
                    SELECT popularity_score 
                    FROM tracks 
                    WHERE artist = {placeholder} AND popularity_score > 0
                """, (artist,))
                artist_popularities = [row['popularity_score'] for row in cursor.fetchall()]
                artist_passed = True
                artist_zscore = 0.0
                artist_mean = 0.0
                
                if len(artist_popularities) >= 5:
                    # Established artist: use artist-level z-score
                    from statistics import stdev as stat_stdev, mean as stat_mean
                    artist_mean = stat_mean(artist_popularities)
                    artist_stddev = stat_stdev(artist_popularities) if len(artist_popularities) > 1 else 1
                    artist_zscore = (popularity - artist_mean) / artist_stddev if artist_stddev > 0 else 0
                    
                    artist_threshold = 0.5  # Configurable threshold
                    if artist_zscore < artist_threshold:
                        if verbose:
                            log_verbose(f"   ⊗ Artist filter blocked: {title} (z-score {artist_zscore:.2f} < {artist_threshold})")
                        artist_passed = False
                elif verbose:
                    log_verbose(f"   ⚠ Bootstrap: Artist has {len(artist_popularities)} tracks (< 5), skipping artist filter")
                
                if artist_popularities and not artist_passed:
                    conn.close()
                    return {
                        "sources": [],
                        "confidence": "low",
                        "is_single": False,
                        "stage_blocked": "artist_filter",
                        "artist_zscore": artist_zscore
                    }
                if verbose:
                    artist_zscore_value = artist_zscore if 'artist_zscore' in locals() else 0.0
                    if artist_zscore_value > 0:
                        log_verbose(f"   ✓ Passed both filters: {title} (album top 3/threshold, z-score {artist_zscore_value:.2f})")
            
            conn.close()
        except Exception as e:
            if verbose:
                log_verbose(f"   âš  Could not calculate album mean for popularity filter: {e}")
            # Continue with detection if we can't calculate album mean
    
    single_sources = []
    
    # Load discogs token from config if not provided
    if discogs_token is None:
        discogs_token = ""
        try:
            config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f)
            discogs_token = config.get("api_integrations", {}).get("discogs", {}).get("token", "")
            if discogs_token:
                if verbose:
                    log_unified(f"   âœ“ Loaded Discogs token from config.yaml")
        except Exception as e:
            # Always log config loading errors, not just in verbose mode
            log_unified(f"   âš  Could not load Discogs token from config at {config_path}: {e}")
    
    # First check: Spotify single detection
    try:
        # Use cached results if available
        spotify_results = None
        if spotify_results_cache is not None:
            spotify_results = spotify_results_cache.get(title)
        
        if spotify_results is None:
            # Query Spotify
            if verbose:
                log_verbose(f"   Spotify results not cached for {title}, querying...")
            spotify_results = _run_with_timeout(
                search_spotify_track,
                API_CALL_TIMEOUT,
                f"Spotify single detection timed out after {API_CALL_TIMEOUT}s",
                title, artist
            )
        else:
            if verbose:
                log_verbose(f"   âœ“ Reusing cached Spotify results for {title}")
        
        if spotify_results and isinstance(spotify_results, list) and len(spotify_results) > 0:
            # Use new sophisticated matching logic
            # Convert duration from seconds to milliseconds if provided
            duration_ms = int(duration * 1000) if duration else None
            
            # Log all releases before filtering if verbose
            if verbose:
                log_verbose(f"   Spotify returned {len(spotify_results)} releases for {title}")
            
            # Use the sophisticated version-aware matching with improved fuzzy matching
            matched_release = find_matching_spotify_single(
                spotify_results=spotify_results,
                track_title=title,
                track_duration_ms=duration_ms,
                track_artist=artist,  # Pass artist for improved fuzzy matching
                track_album=album,    # Pass album for improved fuzzy matching
                track_isrc=isrc,      # Pass ISRC for perfect matching
                duration_tolerance_sec=2,
                logger=logger if verbose else None
            )
            
            if matched_release:
                single_sources.append("spotify")
                album_info = matched_release.get("album", {})
                if verbose:
                    log_verbose(f"   âœ“ Spotify confirms single: {title}")
                    log_verbose(f"      Matched release: {matched_release.get('name')}")
                    log_verbose(f"      Album: {album_info.get('name')} (type: {album_info.get('album_type')})")
            else:
                if verbose:
                    log_verbose(f"   â“˜ No matching Spotify single found for {title}")
    except TimeoutError as e:
        if verbose:
            log_verbose(f"Spotify single check timed out for {title}: {e}")
    except Exception as e:
        if verbose:
            log_verbose(f"Spotify single check failed for {title}: {e}")
    
    # Second check: MusicBrainz single detection
    if HAVE_MUSICBRAINZ:
        try:
            log_info(f"   Checking MusicBrainz for single: {title}")
            # Use timeout-safe client to prevent retries from exceeding timeout
            mb_client = _get_timeout_safe_musicbrainz_client()
            if mb_client:
                result = _run_with_timeout(
                    mb_client.is_single,
                    API_CALL_TIMEOUT,
                    f"MusicBrainz single detection timed out after {API_CALL_TIMEOUT}s",
                    title, artist
                )
                if result:
                    single_sources.append("musicbrainz")
                    log_info(f"   âœ“ MusicBrainz confirms single: {title}")
                else:
                    log_info(f"   â“˜ MusicBrainz does not confirm single: {title}")
                
                # Additional MusicBrainz checks (medium confidence)
                # Check for music video relationship
                try:
                    has_video = _run_with_timeout(
                        mb_client.has_video_relationship,
                        API_CALL_TIMEOUT,
                        f"MusicBrainz video check timed out after {API_CALL_TIMEOUT}s",
                        title, artist
                    )
                    if has_video:
                        single_sources.append("musicbrainz_video")
                        log_info(f"   ✅ MusicBrainz: Track has music video relationship: {title}")
                except TimeoutError:
                    log_debug(f"   ⏱ MusicBrainz video check timed out for {title}")
                except Exception as e:
                    log_debug(f"   MusicBrainz video check error for {title}: {e}")
                
                # Check for Various Artists appearances
                try:
                    on_compilations = _run_with_timeout(
                        mb_client.appears_on_various_artists,
                        API_CALL_TIMEOUT,
                        f"MusicBrainz compilation check timed out after {API_CALL_TIMEOUT}s",
                        title, artist
                    )
                    if on_compilations:
                        single_sources.append("musicbrainz_compilation")
                        log_info(f"   ✅ MusicBrainz: Track appears on multiple compilation albums: {title}")
                except TimeoutError:
                    log_debug(f"   ⏱ MusicBrainz compilation check timed out for {title}")
                except Exception as e:
                    log_debug(f"   MusicBrainz compilation check error for {title}: {e}")
        except TimeoutError as e:
            log_info(f"   â± MusicBrainz single check timed out for {title}: {e}")
        except Exception as e:
            log_info(f"   âš  MusicBrainz single check failed for {title}: {e}")
    else:
        log_info(f"   â“˜ MusicBrainz client not available")
    
    # Third check: Discogs single detection
    if discogs_token:
        try:
            log_info(f"   Checking Discogs for single: {title}")
            log_debug(f"   Discogs API: Searching for single '{title}' by '{artist}'")
            # Use timeout-safe client to prevent retries from exceeding timeout
            discogs_client = _get_timeout_safe_discogs_client(discogs_token)
            if discogs_client:
                result = _run_with_timeout(
                    lambda: discogs_client.is_single(title, artist, album_context=None),
                    API_CALL_TIMEOUT,
                    f"Discogs single detection timed out after {API_CALL_TIMEOUT}s"
                )
                if result:
                    single_sources.append("discogs")
                    log_info(f"   âœ“ Discogs confirms single: {title}")
                    log_debug(f"   Discogs result: Single confirmed for '{title}'")
                else:
                    log_info(f"   â“˜ Discogs does not confirm single: {title}")
                    log_debug(f"   Discogs result: No single found for '{title}'")
        except TimeoutError as e:
            log_info(f"   â± Discogs single check timed out for {title}: {e}")
            log_debug(f"   Discogs API: Timeout after {API_CALL_TIMEOUT}s for '{title}'")
        except Exception as e:
            log_info(f"   âš  Discogs single check failed for {title}: {e}")
            log_debug(f"   Discogs API error: {type(e).__name__}: {str(e)}")
    else:
        log_info(f"   â“˜ Discogs token not configured")
        log_debug(f"   Discogs: Token not configured in config.yaml")
    
    # Fourth check: Discogs video detection
    if discogs_token:
        try:
            log_info(f"   Checking Discogs for music video: {title}")
            log_debug(f"   Discogs API: Searching for music video '{title}' by '{artist}'")
            # Use timeout-safe client to prevent retries from exceeding timeout
            discogs_client = _get_timeout_safe_discogs_client(discogs_token)
            if discogs_client:
                result = _run_with_timeout(
                    lambda: discogs_client.has_official_video(title, artist),
                    API_CALL_TIMEOUT,
                    f"Discogs video detection timed out after {API_CALL_TIMEOUT}s"
                )
                if result:
                    single_sources.append("discogs_video")
                    log_info(f"   âœ“ Discogs confirms music video: {title}")
                    log_debug(f"   Discogs result: Music video confirmed for '{title}'")
                else:
                    log_info(f"   â“˜ Discogs does not confirm music video: {title}")
                    log_debug(f"   Discogs result: No music video found for '{title}'")
        except TimeoutError as e:
            log_info(f"   â± Discogs video check timed out for {title}: {e}")
            log_debug(f"   Discogs API: Video search timeout after {API_CALL_TIMEOUT}s for '{title}'")
        except Exception as e:
            log_info(f"   âš  Discogs video check failed for {title}: {e}")
            log_debug(f"   Discogs API error: {type(e).__name__}: {str(e)}")
    else:
        log_info(f"   â“˜ Discogs token not configured for video detection")
        log_debug(f"   Discogs: Token not configured for video detection")
    
    # Iterative z-score detection (required method)
    iterative_zscore_passed = False
    if album and popularity and popularity > 0 and track_id:
        try:
            from popularity_helpers import detect_via_iterative_zscore
            db_conn = get_db_connection()
            iterative_zscore_passed = detect_via_iterative_zscore(
                current_track_score=popularity,
                artist=artist,
                album=album,
                conn=db_conn,
                verbose=verbose
            )
            db_conn.close()
            if iterative_zscore_passed:
                single_sources.append("iterative_zscore")
                log_info(f"   Iterative z-score method: {title} passed album standout test")
            else:
                log_debug(f"   Iterative z-score: {title} did not meet threshold")
        except Exception as e:
            log_debug(f"   Iterative z-score detection error for {title}: {e}")
    
    # Calculate confidence based on sources with iterative z-score as required baseline
    # High confidence: iterative_zscore + at least one other method
    # Medium confidence: iterative_zscore only
    # Low confidence: no iterative_zscore
    has_iterative_zscore = "iterative_zscore" in single_sources
    has_discogs_single = "discogs" in single_sources
    has_discogs_video = "discogs_video" in single_sources
    has_other_sources = any(s in single_sources for s in ["spotify", "musicbrainz", "lastfm"])
    
    if has_iterative_zscore and (has_discogs_single or has_discogs_video or has_other_sources):
        single_confidence = "high"
    elif has_iterative_zscore:
        single_confidence = "medium"
    elif has_other_sources or has_discogs_video or has_discogs_single:
        single_confidence = "medium"
    else:
        single_confidence = "low"
    
    # Album context rule: downgrade medium -> low if album has >3 tracks
    # Skip downgrade when iterative z-score is present (required method)
    if single_confidence == "medium" and album_track_count > 3 and not has_iterative_zscore:
        single_confidence = "low"
        if verbose:
            log_verbose(f"   Downgraded {title} confidence to low (album has {album_track_count} tracks)")
    
    # is_single = True only for high confidence singles (5* singles)
    is_single = single_confidence == "high"
    
    # Deduplicate sources to ensure no duplicates slip through
    single_sources_dedup = list(dict.fromkeys(single_sources))
    
    return {
        "sources": single_sources_dedup,
        "confidence": single_confidence,
        "is_single": is_single
    }


def get_artist_listenbrainz_context(artist_mbid: str) -> dict:
    """
    Fetch ListenBrainz top recordings for an artist to determine top 10% threshold.
    
    Uses ListenBrainz popularity API to get top recordings sorted by listen count.
    This provides a community-based popularity ranking independent of Last.fm.
    
    Args:
        artist_mbid: MusicBrainz ID of the artist
        
    Returns:
        Dict with keys:
        - top_10_percentile_threshold: Listen count for top 10% position
        - total_recordings: Total recordings found
        - source: 'listenbrainz' if successful, 'error' if failed
        - listen_counts: List of listen counts for debugging
    """
    try:
        import requests
        
        url = f"https://api.listenbrainz.org/1/popularity/top-recordings-for-artist/{artist_mbid}"
        
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            recordings = response.json()
            
            if not recordings:
                return {'top_10_percentile_threshold': 0, 'total_recordings': 0, 'source': 'error', 'listen_counts': []}
            
            listen_counts = [r.get('total_listen_count', 0) for r in recordings if r.get('total_listen_count')]
            
            if not listen_counts:
                return {'top_10_percentile_threshold': 0, 'total_recordings': len(recordings), 'source': 'error', 'listen_counts': []}
            
            total_recordings = len(recordings)
            top_10_count = max(1, total_recordings // 10)
            top_10_threshold = listen_counts[min(top_10_count - 1, len(listen_counts) - 1)]
            
            log_debug(f"ListenBrainz: {total_recordings} total recordings, top 10% = {top_10_count} recordings, threshold: {top_10_threshold} listens")
            
            return {
                'top_10_percentile_threshold': top_10_threshold,
                'total_recordings': total_recordings,
                'source': 'listenbrainz',
                'listen_counts': listen_counts
            }
        else:
            log_debug(f"ListenBrainz API error: {response.status_code} for artist {artist_mbid}")
            return {'top_10_percentile_threshold': 0, 'total_recordings': 0, 'source': 'error', 'listen_counts': []}
    except Exception as e:
        log_debug(f"Failed to fetch ListenBrainz context for artist {artist_mbid}: {e}")
        return {'top_10_percentile_threshold': 0, 'total_recordings': 0, 'source': 'error', 'listen_counts': []}


def blend_top_10_thresholds(lastfm_threshold: int, lastfm_total: int, listenbrainz_threshold: int, listenbrainz_total: int) -> tuple:
    """
    Blend thresholds from Last.fm and ListenBrainz for more robust top 10% detection.
    
    Applies weighted averaging based on data availability:
    - Both sources available: 60% Last.fm + 40% ListenBrainz
    - Last.fm only: Use Last.fm
    - ListenBrainz only: Use ListenBrainz  
    - Neither: Return 0
    
    Args:
        lastfm_threshold: Top 10% threshold from Last.fm (listener count)
        lastfm_total: Total tracks from Last.fm
        listenbrainz_threshold: Top 10% threshold from ListenBrainz (listen count)
        listenbrainz_total: Total recordings from ListenBrainz
        
    Returns:
        Tuple of (blended_threshold, source_info)
        - blended_threshold: Final threshold to use
        - source_info: String describing which sources were used
    """
    has_lastfm = lastfm_threshold > 0 and lastfm_total > 0
    has_listenbrainz = listenbrainz_threshold > 0 and listenbrainz_total > 0
    
    if has_lastfm and has_listenbrainz:
        # Blend both sources: Last.fm weighted slightly higher as it's more mature
        blended = int((lastfm_threshold * 0.6) + (listenbrainz_threshold * 0.4))
        source_info = f"Blended (Last.fm {lastfm_threshold} + ListenBrainz {listenbrainz_threshold} → {blended})"
        return blended, source_info
    elif has_lastfm:
        source_info = f"Last.fm only ({lastfm_threshold})"
        return lastfm_threshold, source_info
    elif has_listenbrainz:
        source_info = f"ListenBrainz only ({listenbrainz_threshold})"
        return listenbrainz_threshold, source_info
    else:
        return 0, "No data available"


def get_artist_lastfm_context(artist_name: str, conn: sqlite3.Connection, artist_mbid: str = None) -> dict:
    """
    Pre-fetch Last.fm listener data for all tracks by an artist to enable dynamic weight adjustment.
    
    Uses artist-level statistics to identify tracks that are outliers in the artist's catalogue,
    allowing intelligent weight redistribution during popularity scoring.
    
    Falls back to Last.fm's top tracks API, then to ListenBrainz if Last.fm has no data.
    Also fetches artist info to determine top 10% threshold.
    
    Example:
        - Colossus by Borknagar: 43,991 Last.fm listeners
        - Borknagar has ~150 total tracks → Top 10% = top 15 tracks
        - Colossus is in top 15 globally AND outlier on album → 5 stars
    
    Args:
        artist_name: Name of the artist
        conn: Database connection
        artist_mbid: MusicBrainz ID for ListenBrainz fallback (optional)
        
    Returns:
        Dict with keys:
        - mean: Average Last.fm listeners across artist's tracks
        - stdev: Standard deviation of Last.fm listeners
        - min: Minimum listener count
        - max: Maximum listener count
        - track_count: Number of tracks analyzed (for stats)
        - total_tracks: Total artist tracks on Last.fm (from API)
        - top_10_percentile_threshold: Listener count for top 10% of artist tracks (blended if both sources available)
        - track_zscores: Dict mapping track_id → z-score for database tracks
        - source: 'database_plus_api', 'listenbrainz_fallback', or 'error' indicating stats data source
        - threshold_source: Detail on which sources were used for top 10% threshold
    """
    try:
        cursor = conn.cursor()
        
        # Determine database type for proper placeholder syntax
        is_pg = is_postgres_connection(conn)
        placeholder = "%s" if is_pg else "?"
        
        # Get all tracks by artist with Last.fm listener data
        # Exclude live/remix/alternate versions to avoid skewing stats
        cursor.execute(f"""
            SELECT id, title, album, lastfm_track_playcount
            FROM tracks
            WHERE artist = {placeholder} AND lastfm_track_playcount > 0 AND is_single = 0 
                AND album NOT IN (
                    SELECT DISTINCT album FROM tracks WHERE artist = {placeholder} AND album_context_live = 1
                )
                AND album NOT IN (
                    SELECT DISTINCT album FROM tracks WHERE artist = {placeholder} AND discogs_format_descriptions LIKE '%live%'
                )
        """, (artist_name, artist_name, artist_name))
        
        tracks = cursor.fetchall()
        listeners_list = [row[3] for row in tracks if row[3] > 0]
        
        # Fetch artist info to get total track count and top 10% threshold
        total_tracks = 0
        top_10_percentile_threshold = 0
        threshold_source = "none"
        try:
            import requests
            from helpers.config_loader import load_config
            
            config = load_config()
            lastfm_api_key = config.get("api_integrations", {}).get("lastfm", {}).get("api_key") if config else None
            if lastfm_api_key:
                # Fetch top 50 tracks and artist info
                params = {
                    "method": "artist.getTopTracks",
                    "artist": artist_name,
                    "api_key": lastfm_api_key,
                    "limit": 50,
                    "format": "json"
                }
                
                response = requests.get("https://ws.audioscrobbler.com/2.0/", params=params, timeout=10)
                if response.status_code == 200:
                    data = response.json()
                    top_tracks = data.get("toptracks", {}).get("track", [])
                    
                    # Get total track count from response (if available)
                    toptracks_attr = data.get("toptracks", {}).get("@attr", {})
                    total_tracks = int(toptracks_attr.get("total", 0) or 0)
                    
                    # Extract listener counts and find top 10% threshold
                    api_listeners_list = []
                    for track in top_tracks:
                        listeners = int(track.get("playcount", 0) or 0)
                        if listeners > 0:
                            api_listeners_list.append(listeners)
                            listeners_list.append(listeners)
                    
                    # Top 10% threshold = 90th percentile of listener counts
                    if total_tracks > 0:
                        top_10_count = max(1, total_tracks // 10)
                        # Estimate threshold from available top tracks
                        if len(api_listeners_list) >= top_10_count:
                            sorted_listeners = sorted(api_listeners_list, reverse=True)
                            top_10_percentile_threshold = sorted_listeners[min(top_10_count - 1, len(sorted_listeners) - 1)]
                        
                        log_debug(f"Artist info: {artist_name} has {total_tracks} total tracks, top 10% = ~{top_10_count} tracks, threshold: {top_10_percentile_threshold} listeners")
                        threshold_source = "last.fm"
                    
                    if listeners_list:
                        log_debug(f"Added {len(api_listeners_list)} top tracks from Last.fm API for {artist_name}")
        except Exception as e:
            log_debug(f"Failed to fetch Last.fm artist info for {artist_name}: {e}")
        
        # If Last.fm returned no threshold data, try ListenBrainz as fallback
        listenbrainz_threshold = 0
        listenbrainz_total = 0
        if (top_10_percentile_threshold == 0 or total_tracks == 0) and artist_mbid:
            try:
                listenbrainz_data = get_artist_listenbrainz_context(artist_mbid)
                if listenbrainz_data['source'] == 'listenbrainz':
                    listenbrainz_threshold = listenbrainz_data['top_10_percentile_threshold']
                    listenbrainz_total = listenbrainz_data['total_recordings']
                    
                    # Use ListenBrainz if Last.fm failed
                    if top_10_percentile_threshold == 0:
                        top_10_percentile_threshold = listenbrainz_threshold
                        total_tracks = listenbrainz_total
                        threshold_source = "listenbrainz"
                        log_debug(f"Using ListenBrainz as fallback for {artist_name}: threshold={listenbrainz_threshold}, total={listenbrainz_total}")
            except Exception as e:
                log_debug(f"ListenBrainz fallback failed for {artist_name}: {e}")
        
        # Blend thresholds if both sources have data
        if top_10_percentile_threshold > 0 and listenbrainz_threshold > 0:
            blended_threshold, blend_source = blend_top_10_thresholds(
                top_10_percentile_threshold, total_tracks,
                listenbrainz_threshold, listenbrainz_total
            )
            top_10_percentile_threshold = blended_threshold
            threshold_source = blend_source
            log_debug(f"Threshold blend for {artist_name}: {blend_source}")
        
        if not listeners_list or len(listeners_list) < 2:
            return {
                'median': 0,
                'stdev': 0,
                'min': 0,
                'max': 0,
                'track_count': len(listeners_list) if listeners_list else 0,
                'total_tracks': total_tracks,
                'top_10_percentile_threshold': top_10_percentile_threshold,
                'track_zscores': {},
                'source': 'error',
                'threshold_source': threshold_source
            }
        
        # Calculate artist-level statistics (using mean-centered z-scores)
        artist_mean = mean(listeners_list)
        artist_stdev = stdev(listeners_list) if len(listeners_list) > 1 else 0
        artist_min = min(listeners_list)
        artist_max = max(listeners_list)
        
        # Calculate z-score for each database track
        track_zscores = {}
        for track_row in tracks:
            track_id = track_row[0]
            title = track_row[1]
            listeners = track_row[3]
            
            if artist_stdev > 0:
                z = (listeners - artist_mean) / artist_stdev
                track_zscores[track_id] = z
                # Log tracks that are significant outliers (for debugging)
                if abs(z) >= 2.0:
                    in_top_10 = "✓ in top 10%" if listeners >= top_10_percentile_threshold else "✗ not in top 10%"
                    log_debug(f"Artist outlier detected: {title} (z={z:.2f}, listeners={listeners:.0f}, artist_mean={artist_mean:.0f}, {in_top_10})")
        
        return {
            'mean': artist_mean,
            'stdev': artist_stdev,
            'min': artist_min,
            'max': artist_max,
            'track_count': len(listeners_list),
            'total_tracks': total_tracks,
            'top_10_percentile_threshold': top_10_percentile_threshold,
            'track_zscores': track_zscores,
            'source': 'database_plus_api' if listeners_list else 'error',
            'threshold_source': threshold_source
        }
        
    except Exception as e:
        log_debug(f"Error calculating artist Last.fm context: {e}")
        return {
            'mean': 0,
            'stdev': 0,
            'min': 0,
            'max': 0,
            'track_count': 0,
            'total_tracks': 0,
            'top_10_percentile_threshold': 0,
            'track_zscores': {},
            'source': 'error',
            'threshold_source': 'none'
        }


def get_dynamic_weights(
    spotify_score: float,
    lastfm_score: float,
    artist_context: dict,
    track_lastfm_listeners: int,
    base_spotify_weight: float = 0.4,
    base_lastfm_weight: float = 0.3
) -> tuple:
    """
    Calculate dynamically adjusted weights based on artist catalogue context.
    
    For tracks that are outliers in their artist's catalogue, boost the weight
    of the more reliable source signal (e.g., Last.fm for heavily-streamed tracks).
    
    Example:
        - Colossus: Spotify 28, Last.fm 43,991 listeners (z=3.9 above artist mean)
        - Base weights: Spotify 0.4, Last.fm 0.3
        - Colossus is extreme outlier → boost Last.fm to 0.5 (indicates real popularity)
        - Result: Weighted more toward Last.fm's signal
    
    Args:
        spotify_score: Spotify popularity score (0-100)
        lastfm_score: Last.fm popularity score (0-100)
        artist_context: Dict from get_artist_lastfm_context()
        track_lastfm_listeners: Track's Last.fm listener count (raw value)
        base_spotify_weight: Default Spotify weight (typically 0.4)
        base_lastfm_weight: Default Last.fm weight (typically 0.3)
        
    Returns:
        Tuple of (adjusted_spotify_weight, adjusted_lastfm_weight)
    """
    try:
        artist_mean = artist_context.get('mean', 0)
        artist_stdev = artist_context.get('stdev', 0)
        
        # If insufficient artist context, return base weights
        if artist_stdev == 0 or artist_mean == 0:
            return (base_spotify_weight, base_lastfm_weight)
        
        # Calculate z-score for this track relative to artist mean
        if track_lastfm_listeners > 0:
            track_zscore = (track_lastfm_listeners - artist_mean) / artist_stdev
        else:
            return (base_spotify_weight, base_lastfm_weight)
        
        # Boost weight for outliers (tracks significantly above/below artist mean)
        # z-score magnitude indicates how unusual this track is
        abs_zscore = abs(track_zscore)
        
        if abs_zscore >= 2.0:
            # Track is 2+ standard deviations from mean (highly unusual in artist's catalogue)
            if track_lastfm_listeners > artist_mean * 1.5:
                # Above mean outlier - Last.fm signal is stronger, boost it
                adjustment = 1.5 - abs(track_zscore) * 0.05  # Cap adjustment at ~1.5x
                new_lastfm = base_lastfm_weight * adjustment
                # Redistribute unused weight to Spotify proportionally
                weight_freed = base_lastfm_weight * (adjustment - 1.0)
                new_spotify = base_spotify_weight + weight_freed * 0.3
                log_debug(f"Outlier boost (above mean): Last.fm weight {base_lastfm_weight:.2f} → {new_lastfm:.2f} (z={track_zscore:.2f})")
                return (max(0.1, new_spotify), min(0.6, new_lastfm))
            else:
                # Below mean outlier - Spotify signal relatively more important
                adjustment = 1.3
                new_spotify = base_spotify_weight * adjustment
                weight_freed = base_spotify_weight * (adjustment - 1.0)
                new_lastfm = base_lastfm_weight + weight_freed * 0.3
                log_debug(f"Outlier adjustment (below mean): Spotify weight {base_spotify_weight:.2f} → {new_spotify:.2f} (z={track_zscore:.2f})")
                return (min(0.6, new_spotify), max(0.1, new_lastfm))
        
        # Return base weights for normal tracks
        return (base_spotify_weight, base_lastfm_weight)
        
    except Exception as e:
        log_debug(f"Error calculating dynamic weights: {e}")
        return (base_spotify_weight, base_lastfm_weight)


def popularity_scan(
    verbose: bool = False, 
    resume_from: str = None,
    artist_filter: str = None,
    album_filter: str = None,
    skip_header: bool = False,
    force: bool = False,
    filter_missing: bool = False,
    singles_only: bool = False,
    clear_single_detection_sources: list = None,
    stop_progress_file: str = None
):
    """
    Detect track popularity from external sources.
    
    Args:
        verbose: Enable verbose logging
        resume_from: Artist name to resume from (for interrupted scans)
        artist_filter: Only scan tracks for this specific artist
        album_filter: Only scan tracks for this specific album (requires artist_filter)
        skip_header: Skip logging the header (useful when called from unified_scan)
        force: Force re-scan of albums even if they were already scanned (also clears single detection cache)
        filter_missing: Only scan artists/albums with missing popularity data
        singles_only: Only rescan singles detection, skip popularity scoring
        clear_single_detection_sources: List of sources to clear from cache (e.g., ['discogs', 'spotify'])
                                       If force=True, all sources are cleared automatically
        stop_progress_file: Optional progress file path used to cooperatively stop an in-flight scan
    """

    def _stop_requested() -> bool:
        """Return True if caller requested scan cancellation via progress file."""
        if not stop_progress_file:
            return False
        try:
            if not os.path.exists(stop_progress_file):
                return False
            with open(stop_progress_file, "r", encoding="utf-8") as f:
                state = json.load(f)
            if state.get("stop_requested") is True:
                return True
            return (state.get("status") == "stopped") and (not bool(state.get("is_running", False)))
        except Exception:
            return False
    if not skip_header:
        log_unified("Popularity Scan - Starting Popularity Scan")
        log_info("=" * 60)
        log_info("Popularity Scanner Started")
        log_info("=" * 60)
        log_info(f"Popularity scan started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        log_debug(f"Popularity scan params - verbose: {verbose}, resume: {resume_from}, artist: {artist_filter}, album: {album_filter}, force: {force}, filter_missing: {filter_missing}, singles_only: {singles_only}")
    
    # Log scan mode details to info
    if singles_only:
        log_info("Singles-only mode enabled - will only rescan singles detection")
    elif FORCE_RESCAN or force:
        log_info("Force rescan mode enabled - will rescan all albums regardless of scan history")
    else:
        log_info("Normal scan mode - will skip albums that were already scanned")
    
    if filter_missing:
        log_info("Filter missing mode enabled - will only scan albums with missing popularity data")

    # Log filter mode details to info
    if artist_filter:
        if album_filter:
            log_info(f"Filtering: artist='{artist_filter}', album='{album_filter}'")
        else:
            log_info(f"Filtering: artist='{artist_filter}'")
    elif resume_from:
        log_info(f"Resuming from artist: '{resume_from}'")

    # Handle single detection cache invalidation
    conn_for_cache = None
    try:
        if force or clear_single_detection_sources:
            conn_for_cache = get_db_connection()
            cursor_for_cache = conn_for_cache.cursor()
            cache_is_pg = is_postgres_connection(conn_for_cache)
            cache_placeholder = "%s" if cache_is_pg else "?"
            
            if force:
                # Clear entire single detection cache on --force
                log_info("Clearing ALL single detection cache (force scan enabled)")
                cursor_for_cache.execute("""
                    UPDATE tracks 
                    SET single_detection_last_updated = NULL
                    WHERE single_manual_override = 0
                """)
                cleared_count = cursor_for_cache.rowcount
                conn_for_cache.commit()
                log_info(f"Cleared single detection cache for {cleared_count} tracks")
            elif clear_single_detection_sources:
                # Clear cache only for specific sources that were changed
                for source in clear_single_detection_sources:
                    log_info(f"Clearing single detection cache for source: {source}")
                    cursor_for_cache.execute(f"""
                        UPDATE tracks 
                        SET single_detection_last_updated = NULL
                        WHERE single_manual_override = 0
                        AND single_sources LIKE {cache_placeholder}
                    """, (f'%{source}%',))
                    cleared_count = cursor_for_cache.rowcount
                    conn_for_cache.commit()
                    log_info(f"Cleared single detection cache for {cleared_count} tracks using source '{source}'")
            
            conn_for_cache.close()
    except Exception as e:
        log_debug(f"Failed to clear single detection cache: {e}")
        if conn_for_cache:
            conn_for_cache.close()

    # Initialize popularity helpers to configure Spotify client
    from popularity_helpers import configure_popularity_helpers
    try:
        configure_popularity_helpers()
        if not skip_header:
            log_info("Spotify client configured successfully")
        log_debug("Spotify client configuration complete")
    except Exception as e:
        log_info(f"Warning: Failed to configure Spotify client: {e}")
        log_info("Popularity scan will continue but Spotify lookups may fail")
        import traceback
        log_debug(f"Configuration error details: {traceback.format_exc()}")

    log_debug("Connecting to database for popularity scan...")
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Determine database type for proper placeholder syntax
        is_pg = is_postgres_connection(conn)
        placeholder = "%s" if is_pg else "?"

        # Load strict matching configuration from config.yaml
        # Initialize config to empty dict to ensure it's always defined
        config = {}
        strict_spotify_matching = False
        duration_tolerance_sec = 2
        album_skip_days = 7  # Default value
        try:
            config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f)
            features = config.get('features', {})
            strict_spotify_matching = features.get('strict_spotify_matching', False)
            duration_tolerance_sec = features.get('spotify_duration_tolerance', 2)
            album_skip_days = features.get('album_skip_days', 7)
            log_debug(f"Configuration loaded - strict_spotify_matching: {strict_spotify_matching}, duration_tolerance: {duration_tolerance_sec}s, album_skip_days: {album_skip_days}")
            if strict_spotify_matching:
                log_info(f"Strict Spotify matching enabled (duration tolerance: Â±{duration_tolerance_sec}s)")
            else:
                log_info("Standard Spotify matching mode (highest popularity)")
            log_info(f"Album skip days: {album_skip_days} (albums scanned within {album_skip_days} days will be skipped)")
        except Exception as e:
            log_debug(f"Could not load strict matching config (using defaults): {e}")

        # Build SQL query with optional filters
        sql_conditions = []
        
        # Only filter by popularity_score if not forcing rescan
        if not (FORCE_RESCAN or force):
            sql_conditions.append("(popularity_score IS NULL OR popularity_score = 0)")
        
        sql_params = []
        
        if artist_filter:
            # Artist scans should only include albums owned by that album artist.
            # Fall back to track artist only when album_artist is missing.
            sql_conditions.append(f"(COALESCE(NULLIF(album_artist, ''), artist) = {placeholder})")
            sql_params.append(artist_filter)
        
        if album_filter and artist_filter:
            sql_conditions.append(f"album = {placeholder}")
            sql_params.append(album_filter)
        
        sql = f"""
            SELECT id, artist, title, album, isrc, duration, spotify_album_type, track_number, mbid, year,
                   spotify_popularity, lastfm_track_playcount, last_spotify_lookup, popularity_score, album_artist,
                   writer
            FROM tracks
            {('WHERE ' + ' AND '.join(sql_conditions)) if sql_conditions else ''}
            ORDER BY artist, album, title
        """
        
        log_debug(f"Executing SQL: {sql.strip()} with params: {sql_params}")
        cursor.execute(sql, sql_params)

        tracks_raw = cursor.fetchall()
        # Convert sqlite3.Row objects to dictionaries to allow item assignment
        tracks = [dict(row) for row in tracks_raw]
        log_info(f"Found {len(tracks)} tracks to scan for popularity")
        log_debug(f"Fetched {len(tracks)} tracks from database")

        def _writer_is_empty(writer_value) -> bool:
            """Return True when writer credits are missing/empty in stored track data."""
            if writer_value is None:
                return True
            if isinstance(writer_value, (list, tuple, set)):
                return not any(str(v).strip() for v in writer_value)
            if isinstance(writer_value, str):
                raw = writer_value.strip()
                if not raw or raw.lower() in ('[]', 'null', 'none'):
                    return True
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, list):
                        return not any(str(v).strip() for v in parsed)
                except Exception:
                    # Non-JSON non-empty string counts as populated writer data.
                    return False
                return False
            return False

        if not tracks:
            log_info("No tracks found for popularity scan. Exiting.")
            return

        # Group tracks by album_artist and album
        # For compilation albums, album_artist is "Various Artists", so all tracks group together
        # For collaboration albums with guest artists, ensure all tracks from same album group together
        # Each track still uses its individual track["artist"] field for API lookups
        from collections import defaultdict
        
        # First pass: determine canonical album_artist for each album
        # This ensures tracks with NULL or missing album_artist still group with their album
        album_canonical_artist = {}  # Map of album_name -> canonical_album_artist
        
        for track in tracks:
            album_name = track["album"]
            album_artist_value = track.get('album_artist', '') if isinstance(track, dict) else (
                track['album_artist'] if hasattr(track, '__getitem__') and 'album_artist' in track.keys() else ''
            )
            
            # If we don't have a canonical artist yet, use the first non-empty album_artist found
            # Otherwise, preserve existing (only update if current is empty and we find a non-empty one)
            if album_name not in album_canonical_artist:
                # First track with this album - set initial value
                album_canonical_artist[album_name] = album_artist_value if album_artist_value else track["artist"]
            elif not album_canonical_artist[album_name] and album_artist_value:
                # Update if canonical is empty but current track has a value
                album_canonical_artist[album_name] = album_artist_value
        
        log_debug(f"Determined canonical artists for {len(album_canonical_artist)} album(s)")
        for album_name, canonical_artist in list(album_canonical_artist.items())[:5]:  # Log first 5
            log_debug(f"  Album '{album_name}' -> canonical artist: '{canonical_artist}'")
        
        # Second pass: group tracks using canonical album_artist
        artist_album_tracks = defaultdict(lambda: defaultdict(list))
        
        for track in tracks:
            album_name = track["album"]
            # Use the canonical artist we determined in first pass
            grouping_artist = album_canonical_artist.get(album_name, track["artist"])
            
            artist_album_tracks[grouping_artist][album_name].append(track)
            
            log_debug(f"Track grouping: album='{album_name}', grouping_artist='{grouping_artist}', track_artist='{track['artist']}', title='{track['title']}'")

        # Handle resume logic
        resume_hit = False if resume_from else True
        if resume_from:
            log_info(f"Resuming scan from artist: {resume_from}")
        
        scanned_count = 0
        skipped_count = 0
        
        # Calculate total artists for progress tracking
        total_artists = len(artist_album_tracks)
        processed_artists = 0
        log_info(f"Found {total_artists} artists to scan")
        
        # Determine which APIs are enabled
        enabled_apis = []
        # Check if Spotify is available based on configuration
        try:
            config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f)
            if config.get("api_integrations", {}).get("spotify", {}).get("enabled", True):
                enabled_apis.append("Spotify")
            if config.get("api_integrations", {}).get("lastfm", {}).get("api_key"):
                enabled_apis.append("Last.FM")
            if config.get("api_integrations", {}).get("listenbrainz", {}).get("token"):
                enabled_apis.append("ListenBrainz")
        except (FileNotFoundError, yaml.YAMLError, KeyError, AttributeError) as e:
            log_debug(f"Could not load API configuration: {e}")
            # If config loading fails, default to Spotify enabled for backward compatibility
            enabled_apis.append("Spotify")
        
        if enabled_apis:
            log_unified(f"Popularity Scan - Scanning {', '.join(enabled_apis)} for Metadata")
            log_debug(f"Enabled APIs: {enabled_apis}")
        
        for artist, albums in artist_album_tracks.items():
            if _stop_requested():
                log_info("Stop requested via progress file; exiting popularity scan before next artist")
                return False

            # Skip until resume match, then rescan the matched artist (in case albums were still processing)
            if not resume_hit:
                if artist.lower() == resume_from.lower():
                    resume_hit = True
                    log_info(f"Found resume artist: {artist} (rescanning from this point)")
                    # Do NOT skip - rescan this artist in case albums were still processing
                elif resume_from.lower() in artist.lower():
                    resume_hit = True
                    log_info(f"Fuzzy resume match: {resume_from} → {artist} (rescanning from this point)")
                    # Do NOT skip - rescan this artist in case albums were still processing
                else:
                    log_debug(f"Skipping {artist} (before resume point)")
                    continue
            
            log_unified(f"Popularity Scan - Scanning Artist {artist} ({len(albums)} album(s))")
            log_debug(f"Processing artist/album group: {artist} with {len(albums)} albums")
            
            # Get artist MBID from database cache for Last.fm context enrichment
            artist_mbid = None
            try:
                cursor.execute(f"""
                    SELECT musicbrainz_artist_id 
                    FROM tracks 
                    WHERE artist = {placeholder} AND musicbrainz_artist_id IS NOT NULL 
                    LIMIT 1
                """, (artist,))
                row = cursor.fetchone()
                if row and row[0]:
                    artist_mbid = row[0]
                    log_debug(f"Using cached MusicBrainz artist ID for {artist}: {artist_mbid}")
            except Exception as e:
                log_debug(f"Failed to get cached MusicBrainz artist ID for {artist}: {e}")
            
            # Pre-fetch artist's Last.fm context for dynamic weight adjustment
            # This allows us to boost Last.fm weight for tracks that are outliers in the artist's catalogue
            artist_lastfm_context = get_artist_lastfm_context(artist, conn, artist_mbid)
            if artist_lastfm_context['track_count'] > 0:
                log_info(f"Artist Last.fm context: {artist_lastfm_context['track_count']} tracks, mean={artist_lastfm_context['mean']:.0f} listeners, stdev={artist_lastfm_context['stdev']:.0f}")
                log_debug(f"Artist catalogue range: {artist_lastfm_context['min']:.0f} - {artist_lastfm_context['max']:.0f} listeners")
            else:
                log_debug(f"No Last.fm listener data available for artist {artist} - will use base weights")
            
            # Get Spotify artist ID once per artist (before album loop)
            # Skip for compilation albums (Various Artists, Compilation, Soundtrack)
            # Also skip if Spotify weight is 0 (API calls would be wasted)
            spotify_artist_id = None
            is_compilation_group = artist.lower() in ('various artists', 'various', 'compilation', 'soundtrack')
            
            if not is_compilation_group and SPOTIFY_WEIGHT > 0:
                # Lookup Spotify artist ID for non-compilation artists
                try:
                    # First, try to get cached artist ID from database
                    cursor.execute(f"""
                        SELECT spotify_artist_id 
                        FROM tracks 
                        WHERE artist = {placeholder} AND spotify_artist_id IS NOT NULL 
                        LIMIT 1
                    """, (artist,))
                    row = cursor.fetchone()
                    
                    if row and row[0]:
                        spotify_artist_id = row[0]
                        log_info(f'Using cached Spotify artist ID for {artist}: {spotify_artist_id}')
                        log_debug(f'Cached Spotify artist ID: {spotify_artist_id}')
                    else:
                        log_info(f'Looking up Spotify artist ID for: {artist}')
                        rate_limiter = get_rate_limiter()
                        can_proceed, reason = rate_limiter.check_spotify_limit()
                        if not can_proceed:
                            log_debug(f'Spotify rate limit check failed: {reason}')
                            if rate_limiter.wait_if_needed_spotify(max_wait_seconds=5.0):
                                can_proceed = True  # Successfully waited, can proceed now
                            else:
                                log_info(f'Skipping Spotify artist ID lookup for {artist} due to rate limits')
                        
                        if can_proceed:
                            spotify_artist_id = _run_with_timeout(
                                get_spotify_artist_id, 
                                API_CALL_TIMEOUT, 
                                f"Spotify artist ID lookup timed out after {API_CALL_TIMEOUT}s",
                                artist
                            )
                            # Record API request for rate limiting
                            rate_limiter.record_spotify_request()
                            log_debug(f'Spotify API call recorded for rate limiting')
                            
                    if spotify_artist_id:
                        log_info(f'Spotify artist ID found: {spotify_artist_id}')
                        log_debug(f'Updating all tracks for artist {artist} with Spotify artist ID: {spotify_artist_id}')
                        # Batch update all tracks for this artist with the artist ID
                        update_artist_id_for_artist(artist, spotify_artist_id)
                        
                        # Fetch and update Discogs artist ID from Discogs API during popularity scan
                        try:
                            from popularity_helpers import update_discogs_artist_id_for_artist
                            from api_clients.discogs import DiscogsClient
                            
                            # Get Discogs client if available
                            discogs_config = config.get("api_integrations", {}).get("discogs", {})
                            if discogs_config.get("enabled") and discogs_config.get("token"):
                                try:
                                    discogs_client = DiscogsClient(token=discogs_config.get("token"))
                                    discogs_artist_id = _run_with_timeout(
                                        discogs_client.get_artist_id,
                                        12,  # 12 second timeout for Discogs artist lookup
                                        f"Discogs artist ID lookup timed out after 12s",
                                        artist
                                    )
                                    
                                    if discogs_artist_id:
                                        log_info(f'Discogs artist ID found: {artist} -> {discogs_artist_id}')
                                        # Update all tracks for this artist
                                        update_discogs_artist_id_for_artist(artist, discogs_artist_id)
                                        log_debug(f'Updated artist Discogs ID in database: {artist} -> {discogs_artist_id}')
                                    else:
                                        log_debug(f'No Discogs artist ID found for artist: {artist}')
                                except TimeoutError as e:
                                    log_debug(f"Discogs artist ID lookup timed out for {artist}: {e}")
                                except Exception as e:
                                    log_debug(f"Discogs artist ID lookup failed for {artist}: {e}")
                            else:
                                log_debug(f"Discogs not enabled or token missing for artist: {artist}")
                        except Exception as e:
                            log_debug(f"Discogs artist lookup initialization failed for {artist}: {e}")
                        
                    else:
                        log_info(f'No Spotify artist ID found for: {artist}')
                except TimeoutError as e:
                    log_info(f"Spotify artist ID lookup timed out for {artist}")
                    log_debug(f"Timeout error: {e}")
                except Exception as e:
                    log_info(f"Spotify artist ID lookup failed for {artist}: {e}")
                    log_debug(f"Exception details: {type(e).__name__}: {str(e)}")
            else:
                log_info(f"Skipping Spotify/Discogs/MusicBrainz lookups for compilation album group: {artist}")
            
            # Fetch and update artist metadata (country, bio, image) for ALL artists
            # This is independent of Spotify lookup success and applies to all artists
            try:
                if HAVE_MUSICBRAINZ:
                    log_debug(f'Fetching artist country from MusicBrainz for: {artist}')
                    artist_country = _run_with_timeout(
                        get_artist_country,
                        12,  # 12 second timeout for country lookup
                        f"Artist country lookup timed out after 12s",
                        artist,
                        enabled=True
                    )
                    
                    if artist_country:
                        log_info(f'Artist country found: {artist} -> {artist_country}')
                        # Update or insert artist entry using UPSERT
                        cursor.execute(f"""
                            INSERT INTO artists (id, name, country) 
                            VALUES ({placeholder}, {placeholder}, {placeholder})
                            ON CONFLICT(id) DO UPDATE SET country = excluded.country
                        """, (artist, artist, artist_country))
                        
                        # Update tracks table with artist country
                        cursor.execute(f"UPDATE tracks SET artist_country = {placeholder} WHERE COALESCE(album_artist, artist) = {placeholder}", 
                                     (artist_country, artist))
                        conn.commit()
                        log_debug(f'Updated artist country in database: {artist} -> {artist_country}')
                    else:
                        log_debug(f'No country information found for artist: {artist}')
                    
                    # Fetch and save artist bio/image from AudioDB during scan
                    if HAVE_AUDIODB:
                        try:
                            log_debug(f'Fetching artist bio and image from AudioDB for: {artist}')
                            
                            # Fetch artist biography
                            artist_bio = _run_with_timeout(
                                get_artist_biography,
                                8,  # 8 second timeout for bio lookup
                                f"Artist bio lookup timed out after 8s",
                                artist,
                                enabled=True
                            )
                            
                            # Fetch artist image/fanart
                            artist_image = _run_with_timeout(
                                get_artist_fanart,
                                8,  # 8 second timeout for image lookup
                                f"Artist image lookup timed out after 8s",
                                artist,
                                enabled=True
                            )
                            
                            if artist_bio or artist_image:
                                # Update artist entry with bio and image
                                cursor.execute(f"""
                                    INSERT INTO artists (id, name, bio, image_url) 
                                    VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder})
                                    ON CONFLICT(id) DO UPDATE SET 
                                        bio = excluded.bio,
                                        image_url = excluded.image_url
                                """, (artist, artist, artist_bio or "", artist_image or ""))
                                conn.commit()
                                
                                if artist_bio:
                                    log_info(f'Saved artist bio for {artist} ({len(artist_bio)} chars)')
                                if artist_image:
                                    log_info(f'Saved artist image URL for {artist}: {artist_image[:60]}...')
                            else:
                                log_debug(f'No bio or image found from AudioDB for artist: {artist}')
                                # Fall back to Last.fm for bio and CoverArtArchive for image
                                try:
                                    lastfm_config = get_lastfm_config(config)
                                    if lastfm_config.get("enabled") and lastfm_config.get("api_key"):
                                        from api_clients.lastfm import LastFmClient
                                        from api_clients.coverartarchive import get_artist_image_from_caa
                                        
                                        lastfm_client = LastFmClient(lastfm_config.get("api_key"))
                                        
                                        # Fetch artist info from Last.fm
                                        artist_info = _run_with_timeout(
                                            lastfm_client.get_artist_info,
                                            8,
                                            "Last.fm artist info lookup timed out after 8s",
                                            artist
                                        )
                                        
                                        lastfm_bio = artist_info.get("bio", "") or artist_info.get("bio_text", "")
                                        lastfm_image = artist_info.get("image", "")
                                        
                                        # Prefer Last.fm image
                                        final_image = lastfm_image
                                        
                                        if lastfm_bio or final_image:
                                            cursor.execute(f"""
                                                INSERT INTO artists (id, name, bio, image_url) 
                                                VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder})
                                                ON CONFLICT(id) DO UPDATE SET 
                                                    bio = excluded.bio,
                                                    image_url = excluded.image_url
                                            """, (artist, artist, lastfm_bio or "", final_image or ""))
                                            conn.commit()
                                            
                                            if lastfm_bio:
                                                log_info(f'Saved artist bio from Last.fm for {artist} ({len(lastfm_bio)} chars)')
                                            if final_image:
                                                source = "CoverArtArchive" if caa_image else "Last.fm"
                                                log_info(f'Saved artist image URL from {source} for {artist}: {final_image[:60]}...')
                                        else:
                                            log_debug(f'No bio or image found from Last.fm for artist: {artist}')
                                except Exception as e:
                                    log_debug(f"Last.fm/CoverArtArchive fallback failed for {artist}: {e}")
                        except TimeoutError as e:
                            log_debug(f"Artist bio/image lookup timed out for {artist}: {e}")
                            # Still try Last.fm as fallback on timeout
                            try:
                                lastfm_config = get_lastfm_config(config)
                                if lastfm_config.get("enabled") and lastfm_config.get("api_key"):
                                    from api_clients.lastfm import LastFmClient
                                    lastfm_client = LastFmClient(lastfm_config.get("api_key"))
                                    artist_info = lastfm_client.get_artist_info(artist)
                                    lastfm_bio = artist_info.get("bio", "") or artist_info.get("bio_text", "")
                                    if lastfm_bio:
                                        cursor.execute(f"""
                                            INSERT INTO artists (id, name, bio) 
                                            VALUES ({placeholder}, {placeholder}, {placeholder})
                                            ON CONFLICT(id) DO UPDATE SET bio = excluded.bio
                                        """, (artist, artist, lastfm_bio))
                                        conn.commit()
                                        log_info(f'Saved artist bio from Last.fm for {artist} (AudioDB timed out)')
                            except:
                                pass
                        except Exception as e:
                            log_debug(f"Artist bio/image lookup failed for {artist}: {e}")
            except TimeoutError as e:
                log_debug(f"Artist country lookup timed out for {artist}: {e}")
            except Exception as e:
                log_debug(f"Artist country lookup failed for {artist}: {e}")

            # Fallback: Save artist bio/image even when MusicBrainz is unavailable.
            # Country lookup depends on MusicBrainz, but biography and image should not.
            if not HAVE_MUSICBRAINZ:
                try:
                    log_debug(f"MusicBrainz unavailable; fetching artist bio/image without country lookup for: {artist}")

                    artist_bio = ""
                    artist_image = ""

                    if HAVE_AUDIODB:
                        try:
                            artist_bio = _run_with_timeout(
                                get_artist_biography,
                                8,
                                f"Artist bio lookup timed out after 8s",
                                artist,
                                enabled=True
                            ) or ""

                            artist_image = _run_with_timeout(
                                get_artist_fanart,
                                8,
                                f"Artist image lookup timed out after 8s",
                                artist,
                                enabled=True
                            ) or ""
                        except Exception as e:
                            log_debug(f"AudioDB bio/image lookup failed for {artist} (MusicBrainz unavailable): {e}")

                    # If AudioDB has no metadata (or is unavailable), try Last.fm bio/image.
                    if not artist_bio and not artist_image:
                        try:
                            lastfm_config = get_lastfm_config(config)
                            if lastfm_config.get("enabled") and lastfm_config.get("api_key"):
                                from api_clients.lastfm import LastFmClient
                                lastfm_client = LastFmClient(lastfm_config.get("api_key"))
                                artist_info = _run_with_timeout(
                                    lastfm_client.get_artist_info,
                                    8,
                                    "Last.fm artist info lookup timed out after 8s",
                                    artist
                                )

                                artist_bio = artist_info.get("bio", "") or artist_info.get("bio_text", "") or ""
                                artist_image = artist_info.get("image", "") or ""
                        except Exception as e:
                            log_debug(f"Last.fm bio/image fallback failed for {artist} (MusicBrainz unavailable): {e}")

                    if artist_bio or artist_image:
                        cursor.execute(f"""
                            INSERT INTO artists (id, name, bio, image_url)
                            VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder})
                            ON CONFLICT(id) DO UPDATE SET
                                bio = excluded.bio,
                                image_url = excluded.image_url
                        """, (artist, artist, artist_bio or "", artist_image or ""))
                        conn.commit()

                        if artist_bio:
                            log_info(f"Saved artist bio for {artist} (MusicBrainz unavailable) ({len(artist_bio)} chars)")
                        if artist_image:
                            log_info(f"Saved artist image URL for {artist} (MusicBrainz unavailable): {artist_image[:60]}...")
                    else:
                        log_debug(f"No artist bio/image found for {artist} without MusicBrainz")
                except Exception as e:
                    log_debug(f"MusicBrainz-unavailable artist metadata save failed for {artist}: {e}")
            
            # Load Discogs token ONCE before album loop (needed for both popularity scan and singles detection)
            discogs_token = os.environ.get("DISCOGS_TOKEN", "")
            if not discogs_token:
                try:
                    config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")
                    with open(config_path, 'r') as f:
                        config = yaml.safe_load(f)
                    discogs_token = config.get("api_integrations", {}).get("discogs", {}).get("token", "")
                    if discogs_token:
                        log_debug(f"Loaded Discogs token from config.yaml for use in metadata extraction")
                except Exception as e:
                    log_debug(f"Could not load Discogs token from config: {e}")
            
            # Validate Discogs token - reject placeholder or empty tokens
            if discogs_token and discogs_token.lower() in ("your_discogs_token", "your_token", "placeholder", ""):
                log_info(f"⚠ Discogs token is a placeholder ('{discogs_token}') - Discogs features disabled")
                log_debug(f"Discogs token validation failed: token='{discogs_token}' is a placeholder value. Update config.yaml with actual Discogs API token")
                discogs_token = None  # Disable Discogs if placeholder detected
            elif discogs_token and len(discogs_token) < 10:
                log_info(f"⚠ Discogs token appears invalid (too short: {len(discogs_token)} chars) - Discogs features disabled")
                log_debug(f"Discogs token validation failed: token length={len(discogs_token)}, expected 20+ characters")
                discogs_token = None  # Disable Discogs if token looks invalid
            elif discogs_token:
                log_debug(f"✓ Discogs token validated ({len(discogs_token)} chars) - Discogs features enabled")
            
            # Fetch similar artists from Last.fm and ListenBrainz for artist-contextual popularity weighting
            # This enables boosting tracks that are popular among listeners of similar artists
            similar_artists_lastfm = []
            similar_artists_listenbrainz = []
            similar_artists_json = None
            
            # Fetch similar artists for all artists (including compilations for recommendation purposes)
            try:
                # Get Last.fm client for similar artists lookup
                lastfm_config = get_lastfm_config(config)
                if lastfm_config.get("enabled") and lastfm_config.get("api_key"):
                    from api_clients.lastfm import LastFmClient
                    lastfm_client = LastFmClient(lastfm_config.get("api_key"))
                    
                    try:
                        # Check rate limit and wait if needed
                        rate_limiter = get_rate_limiter()
                        can_proceed, reason = rate_limiter.check_lastfm_limit()
                        if not can_proceed:
                            log_debug(f"Rate limit hit for similar artists lookup: {reason}, waiting...")
                            if rate_limiter.wait_if_needed_lastfm(max_wait_seconds=2.0):
                                can_proceed = True
                        
                        if can_proceed:
                            similar_artists_lastfm = _run_with_timeout(
                                lastfm_client.get_similar_artists,
                                8,  # 8 second timeout
                                f"Last.fm similar artists lookup timed out after 8s",
                                artist,
                                limit=10
                            )
                            rate_limiter.record_lastfm_request()
                            
                            if similar_artists_lastfm:
                                log_info(f"Found {len(similar_artists_lastfm)} similar artists for '{artist}' from Last.fm")
                                log_debug(f"Last.fm similar artists: {[a.get('name') for a in similar_artists_lastfm]}")
                            else:
                                log_debug(f"No similar artists found for '{artist}' from Last.fm")
                        else:
                            log_debug(f"Rate limit still active for similar artists lookup after wait, skipping for {artist}")
                    except TimeoutError as e:
                        log_debug(f"Last.fm similar artists lookup timed out for {artist}: {e}")
                    except Exception as e:
                        log_debug(f"Last.fm similar artists lookup failed for {artist}: {e}")
                else:
                    log_debug(f"Last.fm not enabled or API key missing - skipping Last.fm similar artists lookup")
                
                # Try ListenBrainz for similar artists using the public API
                # This requires the artist's MusicBrainz MBID
                try:
                    if HAVE_MUSICBRAINZ:
                        # Try to get artist MBID from MusicBrainz
                        mb_client = _get_timeout_safe_musicbrainz_client()
                        if mb_client:
                            try:
                                # Use get_suggested_mbid to find the artist MBID
                                artist_mbid, confidence = mb_client.get_suggested_mbid(
                                    title=artist,  # For artist, title is the artist name
                                    artist="",     # Empty artist parameter when searching for artist name itself
                                    limit=1
                                )
                                
                                if artist_mbid:
                                    log_debug(f"Found MusicBrainz MBID for artist '{artist}': {artist_mbid}")
                                    
                                    # Now fetch similar artists from ListenBrainz using the public API
                                    # ListenBrainz similar-artists endpoint: https://labs.api.listenbrainz.org/similar-artists/json
                                    try:
                                        import urllib.parse
                                        lb_url = "https://labs.api.listenbrainz.org/similar-artists/json"
                                        params = {
                                            "artist_mbids": artist_mbid  # Can be comma-separated for multiple MBIDs
                                        }
                                        # Use default session to make the request
                                        res = session.get(lb_url, params=params, timeout=(5, 10))
                                        res.raise_for_status()
                                        
                                        lb_results = res.json()
                                        
                                        # Extract similar artists from the response
                                        # ListenBrainz returns: {"payload": {"artists": [{"artist_name": "...", "artist_mbid": "..."}, ...]}}
                                        if lb_results and "payload" in lb_results:
                                            similar_records = lb_results.get("payload", {}).get("artists", [])
                                            
                                            if similar_records:
                                                similar_artists_listenbrainz = [
                                                    {
                                                        "name": record.get("artist_name", ""),
                                                        "mbid": record.get("artist_mbid", "")
                                                    }
                                                    for record in similar_records[:10]  # Limit to top 10
                                                ]
                                                log_info(f"Found {len(similar_artists_listenbrainz)} similar artists for '{artist}' from ListenBrainz")
                                                log_debug(f"ListenBrainz similar artists: {[a.get('name') for a in similar_artists_listenbrainz]}")
                                            else:
                                                log_debug(f"No similar artists found for MBID {artist_mbid} from ListenBrainz")
                                        else:
                                            log_debug(f"ListenBrainz returned unexpected response format for {artist}")
                                    except Exception as e:
                                        log_debug(f"ListenBrainz API call failed for artist {artist}: {e}")
                                else:
                                    log_debug(f"Could not find MusicBrainz MBID for artist '{artist}'")
                            except Exception as e:
                                log_debug(f"MusicBrainz MBID lookup failed for artist '{artist}': {e}")
                        else:
                            log_debug(f"MusicBrainz client not available - skipping ListenBrainz similar artists lookup")
                    else:
                        log_debug(f"MusicBrainz not enabled - skipping ListenBrainz similar artists lookup")
                except Exception as e:
                    log_debug(f"ListenBrainz similar artists lookup failed for {artist}: {e}")
                
                # Store similar artists in database and prepare JSON for later use
                if similar_artists_lastfm or similar_artists_listenbrainz:
                    try:
                        # Prepare combined list for storage
                        similar_artists_json = json.dumps({
                            "lastfm": similar_artists_lastfm,
                            "listenbrainz": similar_artists_listenbrainz,
                            "fetched_at": datetime.now().isoformat()
                        })
                        
                        # Update or insert artist with similar artists data
                        cursor.execute(f"""
                            INSERT INTO artists (id, name, similar_artists_lastfm, similar_artists_listenbrainz, similar_artists_last_updated)
                            VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder})
                            ON CONFLICT(id) DO UPDATE SET 
                                similar_artists_lastfm = excluded.similar_artists_lastfm,
                                similar_artists_listenbrainz = excluded.similar_artists_listenbrainz,
                                similar_artists_last_updated = excluded.similar_artists_last_updated
                        """, (
                            artist,
                            artist,
                            json.dumps(similar_artists_lastfm) if similar_artists_lastfm else None,
                            json.dumps(similar_artists_listenbrainz) if similar_artists_listenbrainz else None,
                            datetime.now().isoformat()
                        ))
                        conn.commit()
                        log_debug(f"Stored similar artists for '{artist}' in database")
                    except Exception as e:
                        log_debug(f"Failed to store similar artists for '{artist}': {e}")
                
                # Fetch and store artist tags from Last.fm
                try:
                    lastfm_config = get_lastfm_config(config)
                    if lastfm_config.get("enabled") and lastfm_config.get("api_key"):
                        api_key = lastfm_config.get("api_key")
                        # Skip if placeholder key
                        if api_key not in ["your_lastfm_api_key", "YOUR_API_KEY", "<your_api_key>", ""]:
                            from api_clients.lastfm import LastFmClient
                            lastfm_client = LastFmClient(api_key)
                        
                        artist_tags = _run_with_timeout(
                            lastfm_client.get_artist_top_tags,
                            8,
                            "Last.fm artist tags lookup timed out after 8s",
                            artist,
                            limit=15
                        )
                        
                        if artist_tags:
                            log_info(f"Found {len(artist_tags)} top tags for '{artist}' from Last.fm")
                            log_debug(f"Artist tags: {[t.get('name') for t in artist_tags]}")
                            
                            # Store Last.fm tags in database
                            try:
                                tags_json = json.dumps([t.get('name') for t in artist_tags])
                                cursor.execute(
                                    f"UPDATE artists SET lastfm_artist_tags = {placeholder} WHERE name = {placeholder}",
                                    (tags_json, artist)
                                )
                                log_debug(f"Stored {len(artist_tags)} Last.fm tags for '{artist}'")
                            except Exception as e:
                                log_debug(f"Failed to store Last.fm tags for '{artist}': {e}")
                        else:
                            log_debug(f"No top tags found for '{artist}' from Last.fm")
                except Exception as e:
                    log_debug(f"Last.fm artist tags lookup failed for {artist}: {e}")
            except Exception as e:
                log_debug(f"Similar artists and tags lookup failed for {artist}: {e}")
            
            # Fetch missing releases from MusicBrainz and update database
            try:
                if HAVE_MUSICBRAINZ:
                    log_debug(f"Checking for missing releases for '{artist}' on MusicBrainz")
                    
                    # Get existing albums for this artist
                    cursor.execute(f"SELECT DISTINCT album FROM tracks WHERE artist = {placeholder}", (artist,))
                    existing_albums = [row[0] for row in cursor.fetchall()]
                    existing_norm = set()
                    for a in existing_albums:
                        if a:
                            # Normalize: lowercase, remove special chars, remove bonus/remaster/etc
                            normalized = unicodedata.normalize("NFKD", a)
                            normalized = "".join(c for c in normalized if not unicodedata.combining(c))
                            normalized = normalized.lower()
                            normalized = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", normalized)
                            normalized = re.sub(r"(?i)\b(remaster(?:ed)?\s*\d{0,4}|remaster|deluxe|live|mono|stereo|edit|mix|version|bonus track)\b", " ", normalized)
                            normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
                            normalized = " ".join(normalized.split())
                            existing_norm.add(normalized)
                    
                    # Get artist MBID for more accurate lookup
                    cursor.execute(f"SELECT MAX(musicbrainz_artist_id) FROM tracks WHERE artist = {placeholder}", (artist,))
                    result = cursor.fetchone()
                    artist_mbid = result[0] if result and result[0] else None
                    
                    # Fetch MusicBrainz releases using the client
                    mb_client = _get_timeout_safe_musicbrainz_client()
                    if not mb_client:
                        raise Exception("MusicBrainz client not available")
                    
                    # Build query
                    if artist_mbid:
                        query = f'arid:"{artist_mbid}" AND (primarytype:album OR primarytype:ep OR primarytype:single)'
                    else:
                        query = f'artist:"{artist}" AND (primarytype:album OR primarytype:ep OR primarytype:single)'
                    
                    # Search for release groups
                    import requests
                    headers = {"User-Agent": "sptnr/1.0"}
                    url = "https://musicbrainz.org/ws/2/release-group"
                    params = {"fmt": "json", "limit": 100, "query": query}
                    
                    response = requests.get(url, headers=headers, params=params, timeout=10)
                    response.raise_for_status()
                    data = response.json()
                    
                    release_groups = data.get("release-groups", []) or []
                    
                    missing_count = 0
                    updated_count = 0
                    
                    for rg in release_groups:
                        rg_id = rg.get("id", "")
                        rg_title = rg.get("title", "")
                        
                        # Normalize title
                        norm_title = unicodedata.normalize("NFKD", rg_title)
                        norm_title = "".join(c for c in norm_title if not unicodedata.combining(c))
                        norm_title = norm_title.lower()
                        norm_title = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", norm_title)
                        norm_title = re.sub(r"(?i)\b(remaster(?:ed)?\s*\d{0,4}|remaster|deluxe|live|mono|stereo|edit|mix|version|bonus track)\b", " ", norm_title)
                        norm_title = re.sub(r"[^a-z0-9]+", " ", norm_title)
                        norm_title = " ".join(norm_title.split())
                        
                        cover_art_url = f"https://coverartarchive.org/release-group/{rg_id}/front-500" if rg_id else ""
                        
                        # If album exists in library, skip to next
                        if norm_title and norm_title in existing_norm:
                            continue
                        
                        if not norm_title:
                            continue
                        
                        # Categorize by type
                        secondary = [s.lower() for s in rg.get("secondary-types") or []]
                        primary_type = (rg.get("primary-type") or "").lower()
                        category = "Album"
                        if "compilation" in secondary:
                            category = "Compilation"
                        elif primary_type == "ep":
                            category = "EP"
                        elif primary_type == "single" or "single" in secondary:
                            category = "Single"
                        
                        # Insert missing release
                        cursor.execute(f"""
                            INSERT OR REPLACE INTO missing_releases 
                            (artist, artist_mbid, release_id, title, primary_type, first_release_date, cover_art_url, category, last_checked)
                            VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, CURRENT_TIMESTAMP)
                        """, (artist, artist_mbid, rg_id, rg_title, 
                              rg.get("primary-type", ""), rg.get("first-release-date", ""), 
                              cover_art_url, category))
                        missing_count += 1
                    
                    conn.commit()
                    
                    if missing_count > 0:
                        log_info(f"MusicBrainz: Found {missing_count} missing releases for '{artist}'")
                    else:
                        log_debug(f"No missing releases found for '{artist}'")
                    
                    # Rate limit: wait 1 second after MusicBrainz API call
                    time.sleep(1.0)
                        
            except Exception as e:
                log_debug(f"Missing releases lookup failed for {artist}: {e}")
            
            album_num = 0
            total_albums = len(albums)
            single_detection_albums_processed = 0
            
            # Calculate milestones for single detection progress tracking
            single_detection_milestone_25 = int(total_albums * 0.25) if total_albums > 0 else 0
            single_detection_milestone_50 = int(total_albums * 0.50) if total_albums > 0 else 0
            single_detection_milestone_75 = int(total_albums * 0.75) if total_albums > 0 else 0
            single_detection_milestones_logged = set()
            
            for album, album_tracks in albums.items():
                if _stop_requested():
                    log_info(f"Stop requested via progress file; exiting during artist '{artist}'")
                    return False

                album_num += 1
                album_scanned = 0  # Initialize before popularity section (may be skipped in singles_only)
                skip_popularity_for_album = False
                mb_writer_client = None
                # Defensive defaults so downstream singles-detection context always has album type values.
                # This prevents NameError regressions if stale variable names are referenced.
                album_type_from_field = 'album'
                pre_detected_album_type = 'album'

                # PostgreSQL safeguard: if a previous album hit SQL error state,
                # clear the failed transaction so this album can still run fully.
                if is_pg and hasattr(conn, "get_transaction_status"):
                    try:
                        from psycopg2 import extensions as _pg_ext
                        tx_status = conn.get_transaction_status()
                        if tx_status == _pg_ext.TRANSACTION_STATUS_INERROR:
                            conn.rollback()
                            log_info(
                                f"Recovered aborted PostgreSQL transaction before album scan: '{artist} - {album}'"
                            )
                    except Exception as e:
                        log_debug(f"Failed PostgreSQL transaction-state check for '{artist} - {album}': {e}")
                
                # Fetch and store album tags from Last.fm
                try:
                    lastfm_config = get_lastfm_config(config)
                    if lastfm_config.get("enabled") and lastfm_config.get("api_key"):
                        api_key = lastfm_config.get("api_key")
                        # Skip if placeholder key
                        if api_key not in ["your_lastfm_api_key", "YOUR_API_KEY", "<your_api_key>", ""]:
                            from api_clients.lastfm import LastFmClient
                            lastfm_client = LastFmClient(api_key)
                        
                        album_tags = _run_with_timeout(
                            lastfm_client.get_album_top_tags,
                            8,
                            "Last.fm album tags lookup timed out after 8s",
                            artist, album,
                            limit=15
                        )
                        
                        if album_tags:
                            log_info(f"Found {len(album_tags)} top tags for '{album}' by '{artist}' from Last.fm")
                            log_debug(f"Album tags: {[t.get('name') for t in album_tags]}")
                        else:
                            log_debug(f"No top tags found for '{album}' by '{artist}' from Last.fm")
                except Exception as e:
                    log_debug(f"Last.fm album tags lookup failed for '{album}' by '{artist}': {e}")
                
                # In singles_only mode, skip popularity scanning and go directly to singles detection
                if singles_only:
                    log_unified(f'Singles Detection - Scanning Album {album} ({album_num}/{len(albums)})')
                    log_info(f'Singles-only mode: Skipping popularity scan, going directly to singles detection for "{artist} - {album}"')
                else:
                    # Check if album was already scanned (unless force rescan is enabled)
                    if not (FORCE_RESCAN or force) and was_album_scanned(artist, album, 'popularity', album_skip_days):
                        log_unified(f'Popularity Scan - Skipping album "{album}" (scanned within last {album_skip_days} days)')
                        log_info(f'Album "{artist} - {album}" was already scanned within {album_skip_days} days')
                        skipped_count += 1
                        skip_popularity_for_album = True
                    
                    log_unified(f'Popularity Scan - Scanning Album {album} ({album_num}/{len(albums)})')
                    log_info(f'Starting popularity scan for album: "{artist} - {album}"')
                
                # ALBUM TYPE DETECTION - Do this once per album at the start
                # Detect album type from MusicBrainz/auto-detection and apply to all tracks
                log_debug(f'Starting album type detection for "{artist} - {album}"')
                
                # Get current album type from first track (if any)
                current_album_type = album_tracks[0].get('spotify_album_type', '') if album_tracks else ''
                detected_album_type = None
                type_detection_source = None
                
                # Auto-detect Various Artists → Album (Compilation)
                if artist.lower() == 'various artists':
                    detected_album_type = 'album+compilation'
                    type_detection_source = 'auto-detected (Various Artists)'
                    log_info(f'Auto-detected compilation album: "{album}" (artist: Various Artists)')
                
                # Auto-detect Soundtrack in album name → Album (Soundtrack)
                elif 'soundtrack' in album.lower():
                    detected_album_type = 'album+soundtrack'
                    type_detection_source = 'auto-detected (Soundtrack in name)'
                    log_info(f'Auto-detected soundtrack album: "{album}"')
                
                # Otherwise, fetch from MusicBrainz with Spotify fallback
                else:
                    try:
                        from api_clients.musicbrainz import get_album_type_with_fallback
                        detected_album_type, type_detection_source = get_album_type_with_fallback(
                            artist, album, current_album_type, enabled=HAVE_MUSICBRAINZ, track_count=len(album_tracks)
                        )
                        log_debug(f'MusicBrainz album type: "{detected_album_type}" (source: {type_detection_source})')
                    except Exception as e:
                        log_debug(f'Failed to fetch album type from MusicBrainz: {e}')
                        detected_album_type = current_album_type or 'album'
                        type_detection_source = 'fallback (Spotify or default)'
                
                # Update ALL tracks in this album with the detected type
                if detected_album_type and detected_album_type != current_album_type:
                    tracks_updated = 0
                    for track in album_tracks:
                        track_id = track["id"]
                        cursor.execute(f"""
                            UPDATE tracks 
                            SET spotify_album_type = {placeholder}
                            WHERE id = {placeholder}
                        """, (detected_album_type, track_id))
                        tracks_updated += 1
                        # Update the track dict for use in rest of scan
                        track["spotify_album_type"] = detected_album_type
                    
                    if tracks_updated > 0:
                        conn.commit()
                        log_info(f'Updated {tracks_updated} track(s) with album type "{detected_album_type}" (source: {type_detection_source})')
                else:
                    log_debug(f'Album type unchanged: "{detected_album_type or current_album_type}"')
                
                # Use the detected type for rest of scan
                album_type_from_field = detected_album_type or current_album_type or 'album'
                pre_detected_album_type = album_type_from_field

                # Override misclassified EPs: if labeled as 'ep' but has >6 tracks, it's likely a full album
                # MusicBrainz sometimes incorrectly classifies live albums or special releases as EPs
                # Example: Metallica - S&M (21 tracks) was wrongly classified as EP
                if album_type_from_field and 'ep' in album_type_from_field.lower() and 'single' not in album_type_from_field.lower():
                    track_count = len(album_tracks)
                    if track_count > 6:  # Standard EP threshold is 3-6 tracks
                        # Convert EP to Album, but keep any secondary types (e.g., "ep (live)" -> "album+live")
                        old_type = album_type_from_field
                        if '(' in album_type_from_field and ')' in album_type_from_field:
                            # Extract secondary type like "(live)" and convert to album+live format
                            match = re.search(r'\(([^)]+)\)', album_type_from_field)
                            if match:
                                secondary_type = match.group(1)
                                album_type_from_field = f"album+{secondary_type}"
                            else:
                                album_type_from_field = "album"
                        else:
                            album_type_from_field = "album"
                        log_info(f'EP override: "{artist} - {album}" has {track_count} tracks (>6), reclassified from "{old_type}" to "{album_type_from_field}"')

                # ALBUM-LEVEL DISCOGS GENRE FETCH
                # For homogeneous album types (Single, EP, Album), fetch Discogs genres once at album level
                # For compilations/soundtracks/live albums, genres will be fetched per-track (different per track)
                album_discogs_genres = None
                album_discogs_genres_json = None
                
                # Determine if this is a "homogeneous" album type (all tracks share genres)
                is_homogeneous_album = True
                album_type_lower = album_type_from_field.lower()
                
                # Check for heterogeneous types (compilation, soundtrack, live, remix, spoken word)
                heterogeneous_markers = ['+compilation', '+soundtrack', '+live', '+remix', '+spokenword']
                if any(marker in album_type_lower for marker in heterogeneous_markers):
                    is_homogeneous_album = False
                    log_debug(f'Heterogeneous album type detected ("{album_type_from_field}"), will fetch Discogs genres per-track')
                
                # Fetch album-level Discogs genres for homogeneous albums
                if is_homogeneous_album and HAVE_DISCOGS and discogs_token:
                    try:
                        # Get Discogs release ID from first track (same for all tracks in album)
                        first_track_discogs_id = row_get(album_tracks[0], 'discogs_release_id') if album_tracks else None
                        
                        if first_track_discogs_id:
                            from api_clients.discogs import DiscogsClient
                            discogs_client_album = DiscogsClient(discogs_token)
                            
                            log_debug(f'Fetching album-level Discogs genres (release ID: {first_track_discogs_id})')
                            album_discogs_genres = discogs_client_album.get_release_genres_by_id(first_track_discogs_id)
                            
                            if album_discogs_genres:
                                # Convert to JSON for storage
                                if isinstance(album_discogs_genres, list) and album_discogs_genres:
                                    if isinstance(album_discogs_genres[0], str):
                                        album_discogs_genres = [{"name": g} for g in album_discogs_genres]
                                    album_discogs_genres_json = json.dumps(album_discogs_genres)
                                    log_info(f'Fetched {len(album_discogs_genres)} Discogs genre(s) at album level for "{album}" (type: {album_type_from_field})')
                                    log_debug(f'Album-level Discogs genres: {album_discogs_genres}')
                            else:
                                log_debug(f'No Discogs genres found at album level for release ID {first_track_discogs_id}')
                        else:
                            log_debug(f'No Discogs release ID available for album-level genre fetch')
                    except Exception as e:
                        log_debug(f'Failed to fetch album-level Discogs genres: {e}')
                        log_info(f'Will fall back to per-track Discogs genre fetching for this album')
                elif is_homogeneous_album:
                    log_debug(f'Discogs not configured or disabled, skipping album-level genre fetch')
                
                # Detect if this is a live/unplugged album
                # Check album type field first (now freshly updated), fall back to name pattern detection
                is_live_album = '+live' in album_type_from_field or is_live_or_alternate_album(album)
                
                if is_live_album:
                    log_info(f'Detected live/unplugged album: "{album}"')
                    log_info(f'Track lookups will include album context to avoid matching studio versions')
                    log_debug(f'Live album detection: album="{album}", album_type="{album_type_from_field}"')
                    
                    # Update all track titles in this album to add (Live) suffix if not already present
                    live_tracks_updated = 0
                    for track in album_tracks:
                        track_id = track["id"]
                        original_title = track["title"]
                        
                        # Only add (Live) suffix if it's not already there
                        if not re.search(r'\(live\)', original_title, re.IGNORECASE):
                            new_title = f"{original_title} (Live)"
                            cursor.execute(f"""
                                UPDATE tracks 
                                SET title = {placeholder}
                                WHERE id = {placeholder}
                            """, (new_title, track_id))
                            live_tracks_updated += 1
                            log_debug(f'Updated track title: "{original_title}" → "{new_title}"')
                            # Update the track dict for use in rest of scan
                            track["title"] = new_title
                    
                    if live_tracks_updated > 0:
                        conn.commit()
                        log_info(f'Updated {live_tracks_updated} track title(s) to add (Live) suffix in album "{album}"')
                
                # Detect alternate takes for this album (tracks with parentheses matching base tracks)
                album_tracks_list = list(album_tracks)
                alternate_takes_map = detect_alternate_takes(album_tracks_list)
                
                # Save alternate take mappings to database
                if alternate_takes_map:
                    for alt_track_id, base_track_id in alternate_takes_map.items():
                        cursor.execute(f"""
                            UPDATE tracks 
                            SET alternate_take = 1, base_track_id = {placeholder}
                            WHERE id = {placeholder}
                        """, (base_track_id, alt_track_id))
                    conn.commit()
                    log_info(f'Detected {len(alternate_takes_map)} alternate take(s) in album')
                    log_debug(f'Alternate takes map: {alternate_takes_map}')
                
                # Detect if this album is a compilation ONLY if we're scanning a compilation artist
                # (e.g., Various Artists, Soundtracks, etc.)
                # This avoids running compilation detection on regular artist popularity scans
                is_scanning_compilation_artist = artist.lower() in ('various artists', 'various', 'compilation', 'soundtrack', 'various artists -')
                
                # Determine album type for optimized scanning strategy
                album_type = "regular"  # Default: regular artist album
                is_compilation = False
                
                if is_scanning_compilation_artist:
                    # Get album metadata from first track to access album_artist and spotify_album_type
                    sample_track = album_tracks_list[0] if album_tracks_list else {}
                    album_artist = row_get(sample_track, 'album_artist')
                    spotify_album_type = row_get(sample_track, 'spotify_album_type')
                    
                    is_compilation = detect_compilation_album(artist, album, album_tracks_list, album_artist, spotify_album_type)
                    if is_compilation:
                        # Update all tracks in this album to mark as compilation
                        cursor.execute(f"""
                            UPDATE tracks 
                            SET is_compilation = 1
                            WHERE COALESCE(NULLIF(album_artist, ''), artist) = {placeholder} AND album = {placeholder}
                        """, (artist, album))
                        conn.commit()
                        log_info(f'Marked album as compilation: "{artist} - {album}"')
                        log_debug(f'Compilation detected for album: album_artist="{album_artist}", spotify_type="{spotify_album_type}"')
                        album_type = "various_artists"
                
                # Check if this is a greatest hits album (even for regular artists)
                if album_type == "regular":
                    is_greatest_hits = detect_greatest_hits_album(album, artist, conn, album_tracks_list)
                    if is_greatest_hits:
                        album_type = "greatest_hits"
                        log_info(f'Album type: Greatest Hits - "{artist} - {album}"')
                        log_debug(f'Greatest hits album detected, will run single detection on all tracks')
                
                log_debug(f'Album type determined: {album_type} for "{artist} - {album}"')
                
                # Cache Spotify search results for singles detection reuse
                # Initialize unconditionally for both singles_only and normal mode
                spotify_results_cache = {}
                
                # Fetch and cache album art using fallback strategy for this album
                album_art_url = None
                if not singles_only:
                    # Reuse the already-loaded/validated token and avoid shadowing it.
                    album_art_discogs_token = discogs_token
                    
                    # Try to fetch and save album art using fallback chain
                    # (MusicBrainz -> AudioDB -> Discogs)
                    if fetch_and_save_album_art_with_fallback(artist, album, conn, cursor, album_art_discogs_token):
                        log_info(f'[ALBUM_ART] Album art successfully downloaded and saved for {artist} - {album}')
                    else:
                        log_debug(f'[ALBUM_ART] Failed to obtain album art from any source for {artist} - {album}')
                
                # Batch-fetch ListenBrainz popularity data for all tracks with MBIDs
                # This is more efficient than per-track calls and respects rate limits
                album_listenbrainz_data = {}  # Map of track_id -> {"total_listen_count": int, "total_user_count": int}
                if not singles_only:
                    try:
                        # Collect all MBIDs from this album's tracks
                        mbids_to_fetch = []
                        mbid_to_track_id = {}  # Map MBID -> track_id for results
                        
                        for track in album_tracks:
                            track_mbid = row_get(track, 'mbid')
                            if track_mbid:
                                mbids_to_fetch.append(track_mbid)
                                mbid_to_track_id[track_mbid] = track["id"]
                        
                        if mbids_to_fetch:
                            log_debug(f'Batch-fetching ListenBrainz popularity for {len(mbids_to_fetch)} recording(s) in album "{album}"')
                            
                            # Import batch function
                            from api_clients.audiodb_and_listenbrainz import get_recording_popularity_batch
                            from api_clients.musicbrainz import _USER_AGENT as MB_USER_AGENT
                            
                            # Call batch API with MusicBrainz user agent for consistency
                            lb_results = get_recording_popularity_batch(mbids_to_fetch, user_agent=MB_USER_AGENT)
                            
                            # Process results and store by track_id
                            for mbid, data in lb_results.items():
                                track_id = mbid_to_track_id.get(mbid)
                                if track_id and data:
                                    # Only store if we have valid listen count
                                    if data.get('total_listen_count') is not None and data.get('total_listen_count') > 0:
                                        album_listenbrainz_data[track_id] = {
                                            "total_listen_count": data.get('total_listen_count', 0),
                                            "total_user_count": data.get('total_user_count', 0)
                                        }
                                        log_debug(f'ListenBrainz data for track {track_id}: {data.get("total_listen_count")} listens, {data.get("total_user_count")} users')
                            
                            log_info(f'Fetched ListenBrainz popularity for {len(album_listenbrainz_data)} track(s) with MBID data')
                            
                    except Exception as e:
                        log_debug(f'ListenBrainz batch fetch failed for album "{album}": {e}')
                        log_info(f'Continuing without ListenBrainz data for this album')
                
                # Collect Last.fm data for all album tracks for z-score normalization
                # This enables us to calculate z-scores relative to the album
                album_lastfm_data = {}  # Map of track_id -> {"listeners": int, "playcount": int}
                if not singles_only:
                    log_info(f'Pre-fetching Last.fm data for all tracks in album "{album}" for z-score normalization')
                    for track in album_tracks:
                        track_id = track["id"]
                        title = track["title"]
                        track_artist = track["artist"]
                        
                        # Skip if already have cached data
                        cached_lastfm = row_get(track, 'lastfm_track_playcount', 0)
                        if cached_lastfm > 0:
                            album_lastfm_data[track_id] = {
                                "listeners": cached_lastfm,  # Note: now stores listeners not playcount
                                "playcount": 0  # Will be fetched if needed
                            }
                            log_debug(f'Using cached Last.fm listeners for {title}: {cached_lastfm}')
                            continue
                        
                        # Fetch from Last.fm API if not cached
                        try:
                            rate_limiter = get_rate_limiter()
                            can_proceed, reason = rate_limiter.check_lastfm_limit()
                            if not can_proceed:
                                # Rate limit hit - wait before proceeding
                                log_debug(f'Rate limit hit for {title}: {reason}, waiting...')
                                if rate_limiter.wait_if_needed_lastfm(max_wait_seconds=2.0):
                                    can_proceed = True
                                else:
                                    # Still at limit after waiting, skip this one
                                    log_debug(f'Rate limit still active for {title} after wait, skipping Last.fm prefetch')
                            
                            if can_proceed:
                                lastfm_info = _run_with_timeout(
                                    get_lastfm_track_info,
                                    API_CALL_TIMEOUT,
                                    f"Last.fm lookup timed out after {API_CALL_TIMEOUT}s",
                                    track_artist, strip_cover_attribution(title)
                                )
                                rate_limiter.record_lastfm_request()
                                
                                if lastfm_info:
                                    listeners = lastfm_info.get("listeners", 0)
                                    playcount = lastfm_info.get("track_play", 0)
                                    album_lastfm_data[track_id] = {
                                        "listeners": listeners,
                                        "playcount": playcount
                                    }
                                    log_debug(f'Fetched Last.fm data for {title}: listeners={listeners}, playcount={playcount}')
                                else:
                                    album_lastfm_data[track_id] = {"listeners": 0, "playcount": 0}
                            else:
                                # Still rate limited after wait - add placeholder entry
                                # This ensures z-score has complete track count even with partial data
                                album_lastfm_data[track_id] = {"listeners": 0, "playcount": 0}
                        except Exception as e:
                            log_debug(f'Failed to fetch Last.fm data for {title}: {e}')
                            album_lastfm_data[track_id] = {"listeners": 0, "playcount": 0}
                    
                    # Log pre-fetch results
                    fetched_listeners = [data["listeners"] for data in album_lastfm_data.values() if data["listeners"] > 0]
                    fetched_tracks = len([data for data in album_lastfm_data.values() if data["listeners"] > 0])
                    zero_tracks = len([data for data in album_lastfm_data.values() if data["listeners"] == 0])
                    log_info(f'Pre-fetch complete for album "{album}": {fetched_tracks} tracks with listener data, {zero_tracks} with zero/unavailable data')
                    if fetched_listeners:
                        log_debug(f'Album listener stats: min={min(fetched_listeners)}, max={max(fetched_listeners)}, avg={sum(fetched_listeners)/len(fetched_listeners):.0f}')
                    
                    # Fetch Last.fm tags, ListenBrainz genres, and Discogs genres for all tracks
                    # This is done batch-style per album to be efficient
                    album_tags_data = {}  # Map of track_id -> {"lastfm_tags": [...], "listenbrainz_genres": [...], "discogs_genres": [...]}
                    
                    # Initialize clients outside the try block to prevent silent failures
                    lastfm_client = None
                    discogs_client = None
                    
                    try:
                        # Get Last.fm client for tag lookups
                        lastfm_config = get_lastfm_config(config)
                        if lastfm_config.get("enabled") and lastfm_config.get("api_key"):
                            api_key = lastfm_config.get("api_key")
                            # Check if API key is still a placeholder
                            if api_key in ["your_lastfm_api_key", "YOUR_API_KEY", "<your_api_key>", ""]:
                                log_info(f"⚠️ Last.fm API key not configured (placeholder value detected)")
                                log_info(f"   Set a real API key in config.yaml under api_integrations.lastfm.api_key")
                            else:
                                from api_clients.lastfm import LastFmClient
                                lastfm_client = LastFmClient(api_key)
                                log_info(f"✓ Last.fm client initialized for tag fetching (API key configured)")
                        else:
                            if not lastfm_config.get("enabled"):
                                log_info(f"⚠️ Last.fm is disabled in config.yaml")
                            else:
                                log_info(f"⚠️ Last.fm API key missing from config.yaml")
                        
                        # Get Discogs client for genre lookups
                        discogs_config = config.get("api_integrations", {}).get("discogs", {})
                        if discogs_config.get("enabled") and discogs_config.get("token"):
                            from api_clients.discogs import DiscogsClient
                            discogs_client = DiscogsClient(discogs_config.get("token"))
                            log_debug(f"Discogs client initialized for batch genre fetching")
                        else:
                            log_debug(f"Discogs client not configured or disabled")
                            
                    except Exception as e:
                        log_debug(f"Error initializing API clients for batch fetch: {e}")
                        log_info(f"Continuing with partial API capabilities for album '{album}'")
                    
                    # Always attempt the batch fetch loop, even if clients failed to initialize
                    try:
                        if lastfm_client or discogs_client:
                            log_info(f'Fetching tags/genres for {len(album_tracks)} track(s) in album "{album}"')
                        else:
                            log_debug(f'No API clients available for batch tag fetch, will use per-track fallback if needed')
                        
                        for track in album_tracks:
                            track_id = track["id"]
                            title = track["title"]
                            track_artist = track["artist"]
                            track_mbid = row_get(track, 'mbid')
                            discogs_release_id = row_get(track, 'discogs_release_id')
                            
                            # Detect cover songs and normalize title for API lookups
                            is_cover_song_tags, normalized_title_tags = detect_cover_and_normalize_title(title)
                            api_lookup_title_tags = normalized_title_tags
                            
                            track_tags = {"lastfm_tags": [], "listenbrainz_genres": [], "discogs_genres": []}
                            
                            # Fetch Last.fm tags
                            if lastfm_client:
                                try:
                                    rate_limiter = get_rate_limiter()
                                    can_proceed, reason = rate_limiter.check_lastfm_limit()
                                    if not can_proceed:
                                        # Rate limit hit - wait before proceeding
                                        log_debug(f'Rate limit hit for Last.fm tags ({title}): {reason}, waiting...')
                                        if rate_limiter.wait_if_needed_lastfm(max_wait_seconds=2.0):
                                            can_proceed = True
                                        else:
                                            log_debug(f'Rate limit still active for Last.fm tags ({title}) after wait, skipping')
                                    
                                    if can_proceed:
                                        lastfm_tags = _run_with_timeout(
                                            lastfm_client.get_track_tags,
                                            5,
                                            f"Last.fm tags lookup timed out",
                                            track_artist, api_lookup_title_tags, limit=10
                                        )
                                        if lastfm_tags:
                                            track_tags["lastfm_tags"] = lastfm_tags
                                            log_debug(f'Fetched {len(lastfm_tags)} Last.fm tags for "{title}"')
                                        else:
                                            log_debug(f'[TAGS] No Last.fm tags returned for "{title}" by "{track_artist}"')
                                        rate_limiter.record_lastfm_request()
                                except Exception as e:
                                    log_debug(f'Failed to fetch Last.fm tags for "{title}": {e}')
                            else:
                                log_debug(f'[TAGS] Last.fm client not available for "{title}"')
                            
                            # Fetch ListenBrainz genres (if MBID available)
                            if track_mbid:
                                try:
                                    from api_clients.audiodb_and_listenbrainz import ListenBrainzUserClient
                                    lb_client = ListenBrainzUserClient("")
                                    lb_genres = _run_with_timeout(
                                        lb_client.get_recording_tags,
                                        5,
                                        f"ListenBrainz genres lookup timed out",
                                        track_mbid
                                    )
                                    if lb_genres:
                                        # Format ListenBrainz tags to match expected structure
                                        track_tags["listenbrainz_genres"] = [{"name": t.get("tag", t.get("name", "")), "count": t.get("count", 0)} for t in lb_genres if t.get("tag") or t.get("name")]
                                        log_debug(f'Fetched {len(track_tags["listenbrainz_genres"])} ListenBrainz genres for "{title}"')
                                except Exception as e:
                                    log_debug(f'Failed to fetch ListenBrainz genres for "{title}": {e}')
                            
                            # Fetch Discogs genres
                            # Use album-level data if available (homogeneous albums), otherwise fetch per-track
                            if album_discogs_genres:
                                # Use pre-fetched album-level genres for homogeneous albums
                                track_tags["discogs_genres"] = album_discogs_genres
                                log_debug(f'Using album-level Discogs genres for "{title}" ({len(album_discogs_genres)} genres)')
                            elif discogs_client:
                                # Fetch per-track for heterogeneous albums (compilation, soundtrack, live, etc.)
                                try:
                                    discogs_genres = None
                                    if discogs_release_id:
                                        # Use release ID if available
                                        discogs_genres = _run_with_timeout(
                                            discogs_client.get_release_genres_by_id,
                                            5,
                                            f"Discogs genres by ID lookup timed out",
                                            discogs_release_id
                                        )
                                    else:
                                        # Fall back to search by title/artist
                                        discogs_genres =_run_with_timeout(
                                            discogs_client.get_genres,
                                            5,
                                            f"Discogs genres search lookup timed out",
                                            title, track_artist
                                        )
                                        if isinstance(discogs_genres, list) and discogs_genres and isinstance(discogs_genres[0], str):
                                            # Convert string list to dict format
                                            discogs_genres = [{"name": g} for g in discogs_genres]
                                    
                                    if discogs_genres:
                                        track_tags["discogs_genres"] = discogs_genres
                                        log_debug(f'Fetched {len(discogs_genres)} Discogs genres per-track for "{title}" (heterogeneous album)')
                                except Exception as e:
                                    log_debug(f'Failed to fetch Discogs genres for "{title}": {e}')
                            
                            # Store all tags for this track (including empty lists if nothing was fetched)
                            album_tags_data[track_id] = track_tags
                        
                        if album_tags_data:
                            log_info(f'Tag/genre fetch complete for album "{album}": {len(album_tags_data)} track(s) processed')
                        
                    except Exception as e:
                        log_debug(f'Error during tag/genre batch fetch for album "{album}": {e}')
                        log_info(f'Continuing with per-track fallback for tag/genre data for this album')

                
                # In singles_only mode, skip all popularity scoring
                if not singles_only and not skip_popularity_for_album:
                    # Batch updates for this album (commit once at end instead of per-track)
                    track_updates = []
                    writer_updates = []
                    
                    # Track progress within album
                    total_tracks = len(album_tracks)
                    tracks_processed = 0
                    # Pre-calculate milestone track counts for efficient checking
                    milestone_25 = int(total_tracks * 0.25)
                    milestone_50 = int(total_tracks * 0.50)
                    milestone_75 = int(total_tracks * 0.75)
                    milestones_logged = set()
                
                else:
                    # Single-only mode: skip popularity processing, will do singles detection only
                    if skip_popularity_for_album:
                        log_info(f"Popularity already scanned for album '{album}'; running singles detection only")
                    else:
                        log_info(f"Singles-only mode for album '{album}': all popularity processing skipped")
                    track_updates = []
                    writer_updates = []
                
                if not singles_only and not skip_popularity_for_album:
                    for track in album_tracks:
                        track_id = track["id"]
                        title = track["title"]
                        track_artist = track["artist"]

                        # Backfill missing writer credits from MusicBrainz during popularity scan.
                        # This runs before popularity cache short-circuit so metadata still improves on cached scans.
                        if HAVE_MUSICBRAINZ and _writer_is_empty(row_get(track, 'writer')):
                            try:
                                if mb_writer_client is None:
                                    mb_writer_client = MusicBrainzClient()

                                mb_writer_names = _run_with_timeout(
                                    mb_writer_client.get_composers_for_track,
                                    API_CALL_TIMEOUT,
                                    f"MusicBrainz writer lookup timed out after {API_CALL_TIMEOUT}s",
                                    strip_cover_attribution(title),
                                    track_artist,
                                    row_get(track, 'mbid') or None,
                                )

                                if mb_writer_names:
                                    deduped_writers = []
                                    for name in mb_writer_names:
                                        cleaned = str(name).strip()
                                        if cleaned and cleaned not in deduped_writers:
                                            deduped_writers.append(cleaned)

                                    if deduped_writers:
                                        writer_json = json.dumps(deduped_writers)
                                        writer_updates.append((writer_json, track_id))
                                        track['writer'] = writer_json
                                        log_info(f'MusicBrainz writer backfill: "{title}" -> {len(deduped_writers)} credit(s)')
                                    else:
                                        log_debug(f'MusicBrainz writer lookup returned only empty values for "{title}"')
                                else:
                                    log_debug(f'No MusicBrainz writer credits found for "{title}"')
                            except TimeoutError as e:
                                log_debug(f'MusicBrainz writer lookup timed out for "{title}": {e}')
                            except Exception as e:
                                log_debug(f'Failed MusicBrainz writer backfill for "{title}": {e}')
                        
                        # Skip popularity scoring for tracks already detected as singles
                        # Singles have their own prominence rating, no need for popularity scoring
                        if row_get(track, 'is_single', 0):
                            log_debug(f'Skipping popularity scoring for single: "{title}" (already marked as is_single=1)')
                            continue
                        
                        # Detect cover songs and normalize title for API lookups
                        is_cover_song, normalized_title = detect_cover_and_normalize_title(title)
                        if is_cover_song:
                            log_debug(f'Cover song detected: "{title}" -> normalized to "{normalized_title}" for API lookups')
                        
                        # Use normalized title for API searches to improve match accuracy
                        api_lookup_title = normalized_title

                        log_info(f'Processing track: "{title}" (Track ID: {track_id})')
                        log_debug(f'Track details - id: {track_id}, title: {title}, album: {album}, artist: {track_artist}')

                    # Check if we can use the complete cached popularity_score
                    # This avoids all API calls if the final score is still valid
                        use_full_cache = False
                        if not (FORCE_RESCAN or force):
                            if should_use_cached_score(track, 'popularity_score', 'last_spotify_lookup'):
                                cached_popularity = row_get(track, 'popularity_score', 0)
                                if cached_popularity > 0:
                                    # Use fully cached score - skip all API lookups
                                    use_full_cache = True
                                    log_info(f'Using complete cached popularity score for: {title} (score: {cached_popularity:.1f})')
                                    log_debug(f'Full score cache hit - skipping all API calls for track {track_id}')
                                
                                    # Get cached component scores
                                    cached_spotify_score = row_get(track, 'spotify_score', 0)
                                    cached_lastfm_ratio = row_get(track, 'lastfm_ratio', 0)
                                
                                    # Add to batch update with cached scores (genres remain unchanged when using cache)
                                    track_updates.append((cached_popularity, cached_spotify_score, cached_lastfm_ratio, None, None, None, None, album_art_url, track_id))
                                    scanned_count += 1
                                    album_scanned += 1
                                    tracks_processed += 1
                                
                                    # Check milestones
                                    if tracks_processed == milestone_25 and 25 not in milestones_logged:
                                        log_unified(f"Popularity Scan - 25% completed - {tracks_processed}/{total_tracks} songs")
                                        log_debug(f"Progress milestone - 25% completed for album {album}")
                                        milestones_logged.add(25)
                                    elif tracks_processed == milestone_50 and 50 not in milestones_logged:
                                        log_unified(f"Popularity Scan - 50% completed - {tracks_processed}/{total_tracks} songs")
                                        log_debug(f"Progress milestone - 50% completed for album {album}")
                                        milestones_logged.add(50)
                                    elif tracks_processed == milestone_75 and 75 not in milestones_logged:
                                        log_unified(f"Popularity Scan - 75% completed - {tracks_processed}/{total_tracks} songs")
                                        log_debug(f"Progress milestone - 75% completed for album {album}")
                                        milestones_logged.add(75)
                                
                                    continue  # Skip to next track
                    
                        # If not using full cache, proceed with individual API lookups

                        # Skip Spotify lookup for obvious non-album tracks (live, remix, etc.)
                        # This prevents the scan from hanging on albums with many bonus/live tracks
                        # Strip cover attributions first so "Song (Live Cover)" doesn't get "Cover" matched
                        base_title = strip_cover_attribution(title)
                        skip_spotify_lookup = any(k in base_title.lower() for k in IGNORE_SINGLE_KEYWORDS)
                        if skip_spotify_lookup:
                            log_info(f'Skipping Spotify lookup for: {title} (keyword filter: live/remix/etc.)')
                            log_debug(f'Track "{title}" matched keyword filter for exclusion')
                    
                        # Try to get popularity from Spotify (using cached data or API)
                        spotify_score = 0
                        spotify_search_results = None
                        spotify_release_date = None
                        lastfm_info = {}  # Initialize for genre extraction later
                    
                        # Check if we can use cached Spotify popularity score
                        if not skip_spotify_lookup and not (FORCE_RESCAN or force):
                            if should_use_cached_score(track, 'spotify_popularity', 'last_spotify_lookup'):
                                spotify_score = row_get(track, 'spotify_popularity', 0)
                                skip_spotify_lookup = True
                                log_info(f'Using cached Spotify popularity for: {title} (score: {spotify_score})')
                                log_debug(f'Cached Spotify data reused for track {track_id}')
                    
                        try:
                            if "Spotify" in enabled_apis and SPOTIFY_WEIGHT > 0 and spotify_artist_id and not skip_spotify_lookup:
                                # Check rate limit before making API call
                                rate_limiter = get_rate_limiter()
                                can_proceed, reason = rate_limiter.check_spotify_limit()
                                if not can_proceed:
                                    log_debug(f'Spotify rate limit check failed: {reason}')
                                    # Try to wait if reasonable
                                    if not rate_limiter.wait_if_needed_spotify(max_wait_seconds=5.0):
                                        log_info(f'Skipping Spotify lookup for {title} due to rate limits')
                                        skip_spotify_lookup = True
                            
                                if not skip_spotify_lookup:
                                    log_info(f'Searching Spotify for track: {title} by {track_artist}')
                                    # Normalize album name to remove version suffixes for better matching
                                    # This helps match albums like "Helix (2021 version)" with "Helix"
                                    normalized_album = normalize_album(album) if album else None
                                    log_debug(f'Spotify search params - title: {api_lookup_title}, artist: {track_artist}, album: {album} (normalized: {normalized_album})')
                                    # For popularity scoring, we pass album for better matching accuracy
                                    # For live/unplugged albums, this is especially important to avoid matching studio versions
                                    spotify_search_results = _run_with_timeout(
                                        search_spotify_track,
                                        API_CALL_TIMEOUT,
                                        f"Spotify track search timed out after {API_CALL_TIMEOUT}s",
                                        api_lookup_title, track_artist, normalized_album
                                    )
                                    # Record API request for rate limiting
                                    rate_limiter.record_spotify_request()
                                    log_debug(f'Spotify API request recorded for rate limiting')
                                
                                    # Cache results for singles detection reuse (using title as key)
                                    spotify_results_cache[title] = spotify_search_results
                                    log_debug(f'Cached Spotify results for track: {title}')
                            
                                # Update last_spotify_lookup timestamp
                                current_timestamp = datetime.now().isoformat()
                                try:
                                    cursor.execute(f"""
                                        UPDATE tracks 
                                        SET last_spotify_lookup = {placeholder}
                                        WHERE id = {placeholder}
                                    """, (current_timestamp, track_id))
                                    log_debug(f'Updated last_spotify_lookup for track {track_id}')
                                except Exception as e:
                                    # Don't let timestamp update fail the entire scan
                                    # This can happen with PostgreSQL transaction state issues
                                    log_debug(f'Warning: Failed to update last_spotify_lookup for track {track_id}: {e}')
                                    # Continue anyway - popularity data is more important than timestamp
                            
                                log_info(f'Spotify search completed. Results count: {len(spotify_search_results) if spotify_search_results else 0}')
                                if spotify_search_results and isinstance(spotify_search_results, list) and len(spotify_search_results) > 0:
                                    log_debug(f'Processing {len(spotify_search_results)} Spotify search results')
                                    # Use strict matching if enabled, otherwise use standard highest popularity
                                    if strict_spotify_matching:
                                        from helpers import select_best_spotify_match_strict
                                        # Get track metadata for strict matching
                                        track_duration_ms = None
                                        track_isrc = None
                                        track_duration = row_get(track, "duration", None)
                                        if track_duration:
                                            # Duration is stored in seconds, convert to milliseconds
                                            track_duration_ms = int(track_duration * 1000)
                                        track_isrc = row_get(track, "isrc", None)
                                    
                                        log_debug(f'Strict matching - duration_ms: {track_duration_ms}, isrc: {track_isrc}')
                                        best_match = select_best_spotify_match_strict(
                                            spotify_search_results,
                                            title,
                                            track_duration_ms,
                                            track_isrc,
                                            duration_tolerance_sec
                                        )
                                        if best_match:
                                            log_info(f'Strict match found for: {title}')
                                            log_debug(f'Best match track_id: {best_match.get("id")}, popularity: {best_match.get("popularity")}')
                                        else:
                                            log_info(f'No strict match found for: {title} (trying standard matching)')
                                            # Fallback to standard matching if no strict match
                                            best_match = max(spotify_search_results, key=lambda r: r.get('popularity', 0))
                                            log_debug(f'Fallback to standard match - id: {best_match.get("id")}, popularity: {best_match.get("popularity")}')
                                    else:
                                        # Standard matching: highest popularity
                                        best_match = max(spotify_search_results, key=lambda r: r.get('popularity', 0))
                                        # Log only essential fields to reduce log bloat
                                        log_debug(f'Standard matching - best match: {best_match.get("name")}, popularity: {best_match.get("popularity")}')
                                
                                    if best_match:
                                        spotify_score = best_match.get("popularity", 0)
                                        spotify_track_id = best_match.get("id")
                                        # Extract release date from Spotify result for age scoring
                                        spotify_release_date = best_match.get("album", {}).get("release_date")
                                        log_info(f'Spotify popularity score: {spotify_score}')
                                        log_debug(f'Spotify track ID: {spotify_track_id}')
                                        log_debug(f'Spotify release date: {spotify_release_date}')
                                    else:
                                        spotify_score = 0
                                        spotify_track_id = None
                                        spotify_release_date = None
                                        log_info(f'No Spotify match found for: {title}')
                                
                                    # Skip comprehensive metadata fetch during popularity scan (can be fetched on-demand later)
                                    # This was causing 90s timeouts per track which severely slowed down scans
                                    log_debug(f"Skipping comprehensive metadata fetch for {title} (can be fetched on-demand later)")
                                else:
                                    log_info(f'No Spotify results found for: {title}')
                                    log_debug(f"[ALBUM_ART] No Spotify results available, cannot extract album art for {title}")
                            else:
                                log_info(f'No Spotify artist ID available')
                        except TimeoutError as e:
                            log_info(f"Spotify lookup timed out for {artist} - {title}")
                            log_debug(f"Timeout error: {e}")
                        except KeyboardInterrupt:
                            # Allow user to interrupt the scan
                            raise
                        except Exception as e:
                            log_info(f"Spotify lookup failed for {artist} - {title}: {e}")
                            log_debug(f"Spotify error details: {type(e).__name__}: {str(e)}")
                            import traceback
                            log_debug(f"Exception traceback: {traceback.format_exc()}")

                        # Try to get popularity from Last.fm (using cached data or API)
                        lastfm_score = 0
                        skip_lastfm_lookup = skip_spotify_lookup  # Use same filter for Last.fm as Spotify
                    
                        # Check if we can use cached Last.fm listeners
                        if not skip_lastfm_lookup and not (FORCE_RESCAN or force):
                            if should_use_cached_score(track, 'lastfm_track_playcount', 'last_spotify_lookup'):
                                cached_listeners = row_get(track, 'lastfm_track_playcount', 0)
                                if cached_listeners > 0:
                                    # Log raw cached Last.fm listener count before calculation
                                    log_debug(f'Last.fm raw cached data for "{title}": listeners={cached_listeners}')
                                    
                                    # Use z-score calculation for cached data too
                                    album_listeners_list = [data["listeners"] for data in album_lastfm_data.values() if data["listeners"] > 0]
                                    album_playcounts_list = [data["playcount"] for data in album_lastfm_data.values() if data["playcount"] > 0]
                                    
                                    if album_listeners_list and album_playcounts_list:
                                        from popularity_helpers import calculate_lastfm_zscore_popularity
                                        lastfm_score = calculate_lastfm_zscore_popularity(
                                            cached_listeners, 0,  # We don't have cached playcount for old data
                                            album_listeners_list,
                                            album_playcounts_list
                                        )
                                        log_info(f'Using cached Last.fm listeners with z-score for: {title} (listeners: {cached_listeners}, score: {lastfm_score:.1f})')
                                    else:
                                        # Fallback to simple logarithmic scoring if not enough album data
                                        lastfm_score = calculate_lastfm_popularity_score(cached_listeners)
                                        log_info(f'Using cached Last.fm listeners for: {title} (count: {cached_listeners}, score: {lastfm_score:.1f}, fallback mode)')
                                    
                                    skip_lastfm_lookup = True
                                    log_debug(f'Cached Last.fm data reused for track {track_id}')
                    
                        if not skip_lastfm_lookup:  # Fetch from API if not cached
                            try:
                                # Check rate limit before making API call
                                rate_limiter = get_rate_limiter()
                                can_proceed, reason = rate_limiter.check_lastfm_limit()
                                if not can_proceed:
                                    log_debug(f'Last.fm rate limit check failed: {reason}')
                                    # Try to wait if reasonable
                                    if rate_limiter.wait_if_needed_lastfm(max_wait_seconds=2.0):
                                        log_debug(f"Waited for Last.fm rate limit, retrying {title}")
                                        can_proceed = True  # Successfully waited, can proceed now
                                    else:
                                        log_info(f'Last.fm rate limit hit for {title}: {reason}, skipping lookup')
                            
                                # Perform lookup if we can proceed (either initially or after waiting)
                                if can_proceed:
                                    log_info(f'Getting Last.fm info for: {title} by {track_artist}')
                                    log_debug(f'Last.fm lookup params - artist: {track_artist}, title: {strip_cover_attribution(title)}')
                                    lastfm_info = _run_with_timeout(
                                        get_lastfm_track_info,
                                        API_CALL_TIMEOUT,
                                        f"Last.fm lookup timed out after {API_CALL_TIMEOUT}s",
                                        track_artist, strip_cover_attribution(title)
                                    )
                                    # Record API request for rate limiting
                                    rate_limiter.record_lastfm_request()
                                    log_debug(f'Last.fm API request recorded for rate limiting')
                                    
                                    log_debug(f'Last.fm API response - listeners: {lastfm_info.get("listeners")}, playcount: {lastfm_info.get("track_play", 0)}')
                                    if lastfm_info and lastfm_info.get("listeners"):
                                        listeners = lastfm_info.get("listeners")
                                        playcount = lastfm_info.get("track_play", 0)
                                        
                                        # Log raw Last.fm listener count before calculation
                                        log_debug(f'Last.fm raw data for "{title}": listeners={listeners}, playcount={playcount}')
                                        
                                        # Store in album_lastfm_data for z-score calculation
                                        album_lastfm_data[track_id] = {
                                            "listeners": listeners,
                                            "playcount": playcount
                                        }
                                        
                                        # Collect all album listener and playcount data for z-score
                                        album_listeners_list = [data["listeners"] for data in album_lastfm_data.values() if data["listeners"] > 0]
                                        album_playcounts_list = [data["playcount"] for data in album_lastfm_data.values() if data["playcount"] > 0]
                                        
                                        # Calculate z-score based popularity
                                        if album_listeners_list and album_playcounts_list:
                                            from popularity_helpers import calculate_lastfm_zscore_popularity
                                            lastfm_score = calculate_lastfm_zscore_popularity(
                                                listeners, playcount,
                                                album_listeners_list,
                                                album_playcounts_list
                                            )
                                            log_info(f'Last.fm z-score popularity: {lastfm_score:.1f} (listeners={listeners}, playcount={playcount}, album_tracks={len(album_listeners_list)})')
                                            log_debug(f'Last.fm z-score data - listeners: {listeners}, playcount: {playcount}, album_median_listeners: {median(album_listeners_list) if album_listeners_list else 0:.0f}')
                                        else:
                                            # Fallback to simple logarithmic scoring if not enough album data
                                            lastfm_score = calculate_lastfm_popularity_score(listeners)
                                            log_info(f'Last.fm listeners (fallback): {listeners} (score: {lastfm_score:.1f})')
                                            log_debug(f'Not enough album data for z-score, using fallback logarithmic scoring')
                                    else:
                                        log_info(f'No Last.fm listeners data found for: {title}')
                            except TimeoutError as e:
                                log_info(f"Last.fm lookup timed out for {artist} - {title}")
                                log_debug(f"Timeout error: {e}")
                            except KeyboardInterrupt:
                                # Allow user to interrupt the scan
                                raise
                            except Exception as e:
                                log_info(f"Last.fm lookup failed for {artist} - {title}: {e}")
                                log_debug(f"Last.fm error details: {type(e).__name__}: {str(e)}")

                        # Try to get ListenBrainz score if mbid is available
                        # Calculate age score if release date is available
                        age_score = 0
                        if spotify_release_date:
                            try:
                                log_debug(f'Calculating age score for release date: {spotify_release_date}')
                                # Apply age decay to Spotify score if available, otherwise use a base score of 1
                                base_score = spotify_score if spotify_score > 0 else 1
                                age_score, days_since = score_by_age(base_score, spotify_release_date)
                                log_debug(f'Age score calculated: {age_score:.1f} (release date: {spotify_release_date}, days since: {days_since})')
                            except Exception as e:
                                log_debug(f"Age score calculation failed: {e}")
                        else:
                            log_debug(f'No release date available for age scoring: {title}')

                        # Collect genre sources from available APIs
                        spotify_genres_json = None
                        lastfm_tags_json = None
                        listenbrainz_genres_json = None
                        discogs_genres_json = None
                        musicbrainz_genres_json = None
                        
                        # Extract Spotify genres from artist metadata (saved by fetch_comprehensive_metadata)
                        try:
                            spotify_artist_genres = row_get(track, 'spotify_artist_genres')
                            if spotify_artist_genres:
                                spotify_genres_json = spotify_artist_genres
                                log_debug(f'Spotify genres available for: {title}')
                        except Exception as e:
                            log_debug(f'Failed to extract Spotify genres: {e}')
                        
                        # Use batch-fetched tag data if available (more efficient than per-track fetches)
                        if track_id in album_tags_data:
                            tags_dict = album_tags_data[track_id]
                            if tags_dict.get("lastfm_tags"):
                                lastfm_tags_json = json.dumps(tags_dict["lastfm_tags"])
                                log_debug(f'Using batch-fetched Last.fm tags for: {title}')
                            if tags_dict.get("discogs_genres"):
                                discogs_genres_json = json.dumps(tags_dict["discogs_genres"])
                                log_debug(f'Using batch-fetched Discogs genres for: {title}')
                        
                        # If batch fetch didn't provide Last.fm tags, try extracting from lastfm_info
                        if not lastfm_tags_json:
                            try:
                                if lastfm_info and lastfm_info.get("toptags"):
                                    tags_list = lastfm_info.get("toptags", {}).get("tag", [])
                                    if tags_list and isinstance(tags_list, list):
                                        tag_names = [tag.get("name") for tag in tags_list if isinstance(tag, dict) and tag.get("name")]
                                        if tag_names:
                                            lastfm_tags_json = json.dumps(tag_names)
                                            log_debug(f'Last.fm tags extracted from API for: {title} - {len(tag_names)} tags')
                            except Exception as e:
                                log_debug(f'Failed to extract Last.fm tags from API: {e}')
                        
                        # Extract Discogs genres (if not already fetched in batch)
                        if not discogs_genres_json and HAVE_DISCOGS and discogs_token:
                            try:
                                discogs_release_id = row_get(track, 'discogs_release_id')
                                if discogs_release_id:
                                    log_debug(f'Fetching Discogs genres for release ID: {discogs_release_id}')
                                    discogs_client = DiscogsClient(token=discogs_token)
                                    discogs_genres = discogs_client.get_genres(title, artist)
                                    if discogs_genres:
                                        discogs_genres_json = json.dumps(discogs_genres)
                                        log_debug(f'Discogs genres extracted for: {title} - {len(discogs_genres)} genres')
                            except Exception as e:
                                log_debug(f'Failed to extract Discogs genres: {e}')
                        
                        # Extract MusicBrainz genres (from recording metadata)
                        if HAVE_MUSICBRAINZ:
                            try:
                                track_mbid = row_get(track, 'mbid')
                                if track_mbid:
                                    log_debug(f'Fetching MusicBrainz genres for MBID: {track_mbid}')
                                    mb_client = MusicBrainzClient()
                                    mb_genres = mb_client.get_genres(title, artist)
                                    if mb_genres:
                                        musicbrainz_genres_json = json.dumps(mb_genres)
                                        log_debug(f'MusicBrainz genres extracted for: {title} - {len(mb_genres)} genres')
                            except Exception as e:
                                log_debug(f'Failed to extract MusicBrainz genres: {e}')

                        # Calculate weighted popularity score
                        # Include 4 sources: Spotify, Last.fm, ListenBrainz, Age
                        # Only include sources that have data (score > 0)
                        scores = []
                        weights = []
                        
                        # Calculate dynamic weights based on artist catalogue context
                        # This boosts Last.fm weight for tracks that are outliers in the artist's catalogue
                        dynamic_spotify_weight = SPOTIFY_WEIGHT
                        dynamic_lastfm_weight = LASTFM_WEIGHT
                        dynamic_listenbrainz_weight = LISTENBRAINZ_WEIGHT
                        
                        if artist_lastfm_context and artist_lastfm_context.get('track_count', 0) > 0 and listeners > 0:
                            dynamic_spotify_weight, dynamic_lastfm_weight = get_dynamic_weights(
                                spotify_score, lastfm_score,
                                artist_lastfm_context,
                                listeners,
                                SPOTIFY_WEIGHT, LASTFM_WEIGHT
                            )
                            if dynamic_spotify_weight != SPOTIFY_WEIGHT or dynamic_lastfm_weight != LASTFM_WEIGHT:
                                log_info(f"Dynamic weight adjustment for artist context: Spotify {SPOTIFY_WEIGHT:.2f}→{dynamic_spotify_weight:.2f}, Last.fm {LASTFM_WEIGHT:.2f}→{dynamic_lastfm_weight:.2f}")
                        
                        if spotify_score > 0:
                            scores.append(spotify_score)
                            weights.append(dynamic_spotify_weight)
                            log_debug(f'Including Spotify score: {spotify_score} (weight: {dynamic_spotify_weight:.2f})')
                    
                        if lastfm_score > 0:
                            scores.append(lastfm_score)
                            weights.append(dynamic_lastfm_weight)
                            log_debug(f'Including Last.fm score: {lastfm_score} (weight: {dynamic_lastfm_weight:.2f})')
                        
                        # NEW: Include ListenBrainz score if available
                        listenbrainz_score = 0
                        if track_id in album_listenbrainz_data:
                            lb_data = album_listenbrainz_data[track_id]
                            lb_listen_count = lb_data.get('total_listen_count', 0)
                            lb_user_count = lb_data.get('total_user_count', 0)
                            
                            if lb_listen_count > 0:
                                # Calculate ListenBrainz score using logarithmic scaling (similar to LastFm)
                                # log10(100) = 2.0 → 25 points  
                                # log10(1000) = 3.0 → 37.5 points
                                # log10(10000) = 4.0 → 50 points
                                listenbrainz_score = min(100.0, max(0.0, 12.5 * math.log10(lb_listen_count)))
                                
                                scores.append(listenbrainz_score)
                                weights.append(dynamic_listenbrainz_weight)
                                log_debug(f'Including ListenBrainz score: {listenbrainz_score:.1f} (listens: {lb_listen_count}, users: {lb_user_count}, weight: {dynamic_listenbrainz_weight:.2f})')
                    
                        if age_score > 0:
                            scores.append(age_score)
                            weights.append(AGE_WEIGHT)
                            log_debug(f'Including age score: {age_score} (weight: {AGE_WEIGHT})')
                    
                        # Calculate weighted average
                        if scores and weights:
                            total_weight = sum(weights)
                            popularity_score = sum(s * w for s, w in zip(scores, weights)) / total_weight
                            
                            # Use weighted popularity score directly (restored to original working method)
                            track_updates.append((popularity_score, spotify_score, lastfm_score, spotify_genres_json, lastfm_tags_json, listenbrainz_genres_json, discogs_genres_json, musicbrainz_genres_json, album_art_url, track_id))
                            scanned_count += 1
                            album_scanned += 1
                            log_info(f'Track scanned successfully: "{title}" (weighted: {popularity_score:.1f})')
                            log_debug(f'Weighted popularity calculation - spotify: {spotify_score:.1f}, lastfm: {lastfm_score:.1f}, listenbrainz: {listenbrainz_score:.1f}, age: {age_score:.1f}, weighted: {popularity_score:.1f}')
                        else:
                            log_info(f"No popularity score found for {artist} - {title}")
                            log_debug(f'No data sources available for scoring')
                    
                        # Track progress and show percentage milestones
                        tracks_processed += 1
                        # Efficient milestone checking using pre-calculated values
                        if tracks_processed == milestone_25 and 25 not in milestones_logged:
                            log_unified(f"Popularity Scan - 25% completed - {tracks_processed}/{total_tracks} songs")
                            log_debug(f"Progress milestone - 25% completed for album {album}")
                            milestones_logged.add(25)
                        elif tracks_processed == milestone_50 and 50 not in milestones_logged:
                            log_unified(f"Popularity Scan - 50% completed - {tracks_processed}/{total_tracks} songs")
                            log_debug(f"Progress milestone - 50% completed for album {album}")
                            milestones_logged.add(50)
                        elif tracks_processed == milestone_75 and 75 not in milestones_logged:
                            log_unified(f"Popularity Scan - 75% completed - {tracks_processed}/{total_tracks} songs")
                            log_debug(f"Progress milestone - 75% completed for album {album}")
                            milestones_logged.add(75)

# Batch update all popularity scores and genre sources for this album in one commit (skipped in singles_only mode)
                if writer_updates and not singles_only:
                    try:
                        cursor.executemany(
                            f"UPDATE tracks SET writer = {placeholder} WHERE id = {placeholder}",
                            writer_updates
                        )
                        log_debug(f"Batch prepared {len(writer_updates)} writer credit update(s) for album '{album}'")
                    except Exception as e:
                        log_debug(f"Warning: Failed to batch update writer credits: {e}")
                        # For PostgreSQL: try to rollback and get fresh connection
                        try:
                            conn.rollback()
                        except:
                            pass

                if track_updates and not singles_only:
                    # Merge tags from album_tags_data into track_updates BEFORE committing
                    # This ensures Last.fm tags and other genre data are saved
                    updated_track_updates = []
                    for update_tuple in track_updates:
                        # Unpack: (popularity_score, spotify_score, lastfm_ratio, spotify_genres, lastfm_tags, listenbrainz_genres, discogs_genres, musicbrainz_genres, album_art_url, track_id)
                        popularity_score, spotify_score, lastfm_ratio, spotify_genres, lastfm_tags, listenbrainz_genres, discogs_genres, musicbrainz_genres, album_art_url, track_id = update_tuple
                        
                        # Check if we have tags for this track in album_tags_data
                        if track_id in album_tags_data:
                            tags_data = album_tags_data[track_id]
                            # Merge tags - prefer freshly fetched data over existing
                            if tags_data.get("lastfm_tags"):
                                lastfm_tags = json.dumps(tags_data["lastfm_tags"])
                                log_debug(f"Using Last.fm tags for track {track_id}: {len(tags_data['lastfm_tags'])} tags")
                            if tags_data.get("listenbrainz_genres"):
                                listenbrainz_genres = json.dumps(tags_data["listenbrainz_genres"])
                                log_debug(f"Using ListenBrainz genres for track {track_id}: {len(tags_data['listenbrainz_genres'])} genres")
                            if tags_data.get("discogs_genres"):
                                discogs_genres = json.dumps(tags_data["discogs_genres"])
                                log_debug(f"Using Discogs genres for track {track_id}: {len(tags_data['discogs_genres'])} genres")
                        
                        # Add "Cover" genre if this is a cover song
                        if is_cover_song:
                            # Add Cover to musicbrainz_genres (most appropriate field for special tags)
                            try:
                                mb_genres_list = json.loads(musicbrainz_genres) if musicbrainz_genres and musicbrainz_genres != 'null' else []
                                if "Cover" not in mb_genres_list:
                                    mb_genres_list.insert(0, "Cover")  # Add at beginning for visibility
                                    musicbrainz_genres = json.dumps(mb_genres_list)
                                    log_debug(f'Added "Cover" genre to track: {title}')
                            except (json.JSONDecodeError, TypeError):
                                musicbrainz_genres = json.dumps(["Cover"])
                                log_debug(f'Initialized genres with "Cover" for track: {title}')
                        
                        # Append merged tuple
                        updated_track_updates.append((popularity_score, spotify_score, lastfm_ratio, spotify_genres, lastfm_tags, listenbrainz_genres, discogs_genres, musicbrainz_genres, album_art_url, track_id))
                    
                    try:
                        cursor.executemany(
                            f"UPDATE tracks SET popularity_score = {placeholder}, spotify_score = {placeholder}, lastfm_ratio = {placeholder}, spotify_genres = {placeholder}, lastfm_tags = {placeholder}, listenbrainz_genres = {placeholder}, discogs_genres = {placeholder}, musicbrainz_genres = {placeholder}, cover_art_url = {placeholder} WHERE id = {placeholder}",
                            updated_track_updates
                        )
                        conn.commit()
                    except Exception as e:
                        # PostgreSQL may abort transaction if previous updates failed
                        log_debug(f"Error batch updating popularity scores: {e}")
                        try:
                            conn.rollback()
                            log_debug(f"Rolled back failed transaction")
                        except:
                            pass
                        # Try to get fresh connection and continue
                        try:
                            from app import get_db
                            conn = get_db()
                            cursor = conn.cursor()
                            log_debug(f"Got fresh database connection after transaction failure")
                        except Exception as conn_error:
                            log_debug(f"Failed to get fresh connection: {conn_error}")
                            raise  # Re-raise if we can't recover
                    log_debug(f"Batch committed {len(updated_track_updates)} popularity scores and genre sources for album '{album}' with merged tag data")
                    if writer_updates:
                        log_debug(f"Committed {len(writer_updates)} writer credit update(s) for album '{album}'")
                    if album_art_url:
                        log_info(f"[ALBUM_ART] Album art URL cached for {album}: {album_art_url}")
                    else:
                        log_debug(f"[ALBUM_ART] Album art will be fetched on-demand from Navidrome or Apple Music sources")
                elif writer_updates and not singles_only:
                    conn.commit()
                    log_debug(f"Committed {len(writer_updates)} writer credit update(s) for album '{album}'")
                
                if not singles_only:
                    log_unified(f'Popularity Scan - Popularity Scanning for {album} Complete')
                    log_info(f'Album "{artist} - {album}" scanned. Popularity applied to {album_scanned} tracks')
                    
                    # --- Standout & Star Rating Assignment ---
                    #
                    # This section applies album- and artist-normalized standout detection and star rating assignment.
                    #
                    # Album standout: z >= config or top N in album (using median+MAD)
                    # Artist standout: z >= config and in top X% of artist catalog (using median+MAD)
                    # 5★: Both album and artist standout, top 10% of artist
                    # 4★: Album standout and artist z >= 1.0 or top 20%
                    # 3★: Album standout only or above album median
                    # 2★: Not standout, but above album median
                    # 1★: Below album median or excluded from stats
                    try:
                        from statistics import median as stat_median_standout
                        from popularity_helpers import get_top_standout_tracks_with_gap
                        MIN_SPREAD = 10.0  # Prevent flat-album noise amplification
                        log_info(f'Analyzing standout/star ratings for artist: {artist}')
                        cursor.execute(f"""
                            SELECT id, title, album, popularity_score, lastfm_track_playcount FROM tracks
                            WHERE COALESCE(NULLIF(album_artist, ''), artist) = {placeholder} AND is_single = 0 AND album NOT IN (
                                SELECT DISTINCT album FROM tracks WHERE COALESCE(NULLIF(album_artist, ''), artist) = {placeholder} AND album_context_live = 1
                            ) AND album NOT IN (
                                SELECT DISTINCT album FROM tracks WHERE COALESCE(NULLIF(album_artist, ''), artist) = {placeholder} AND discogs_format_descriptions LIKE '%live%'
                            )
                        """, (artist, artist, artist))
                        artist_tracks = cursor.fetchall()
                        if not artist_tracks:
                            log_debug(f'No non-single tracks found for {artist}, skipping standout analysis')
                        else:
                            # Gather all popularity scores for artist
                            artist_scores = [row_get(t, 'popularity_score', 0) for t in artist_tracks if row_get(t, 'popularity_score', 0) > 0]
                            if len(artist_scores) < 2:
                                log_debug(f'Insufficient tracks with scores for {artist} ({len(artist_scores)} found), skipping standout analysis')
                            else:
                                # Use median + MAD for artist-level statistics
                                artist_median = stat_median_standout(artist_scores)
                                artist_absolute_devs = [abs(s - artist_median) for s in artist_scores]
                                artist_mad = stat_median_standout(artist_absolute_devs)
                                artist_mad_scaled = artist_mad * 1.4826  # Scale to be comparable to stddev
                                artist_spread = max(artist_mad_scaled, MIN_SPREAD)  # Apply floor
                                
                                sorted_artist_scores = sorted(artist_scores, reverse=True)
                                def artist_percentile(score):
                                    return (sorted_artist_scores.index(score) + 1) / len(sorted_artist_scores)
                                
                                # Pre-compute standout clusters for each album to handle multiple singles with similar popularity
                                album_standout_clusters = {}
                                unique_albums = set(row_get(t, 'album', '') for t in artist_tracks)
                                for album_name in unique_albums:
                                    if album_name:
                                        cluster = get_top_standout_tracks_with_gap(artist, album_name, conn, gap_threshold=0.5, verbose=False)
                                        if cluster:
                                            album_standout_clusters[album_name] = cluster
                                            log_debug(f"Album '{album_name}' has {len(cluster)} tracks in standout cluster")
                                
                                for track in artist_tracks:
                                    track_id = row_get(track, 'id')
                                    track_title = row_get(track, 'title')
                                    track_album = row_get(track, 'album', '')
                                    score = row_get(track, 'popularity_score', 0)
                                    if score <= 0 or artist_spread == 0:
                                        cursor.execute(f"""
                                            UPDATE tracks SET is_standout_track = 0, artist_z_score = 0, stars = 1
                                            WHERE id = {placeholder}
                                        """, (track_id,))
                                        continue
                                    # Album-level stats using median + MAD
                                    cursor.execute(f"""
                                        SELECT popularity_score FROM tracks WHERE COALESCE(NULLIF(album_artist, ''), artist) = {placeholder} AND album = {placeholder} AND popularity_score > 0
                                    """, (artist, track_album))
                                    track_album_scores = [row[0] for row in cursor.fetchall()]
                                    track_album_median = stat_median_standout(track_album_scores) if track_album_scores else 0
                                    track_album_abs_devs = [abs(s - track_album_median) for s in track_album_scores]
                                    track_album_mad = stat_median_standout(track_album_abs_devs) if len(track_album_scores) > 1 else 0
                                    track_album_mad_scaled = track_album_mad * 1.4826
                                    track_album_spread = max(track_album_mad_scaled, MIN_SPREAD)
                                    album_z = (score - track_album_median) / track_album_spread if track_album_spread > 0 else 0
                                    
                                    # Check if track is in the standout cluster for this album
                                    # Uses gap-based clustering to handle multiple singles with similar high popularity
                                    is_in_standout_cluster = track_id in album_standout_clusters.get(track_album, set())
                                    
                                    # Require BOTH medium z-score (>= 0.8) AND being in standout cluster
                                    # This ensures tracks are statistically significant AND part of top-performing group
                                    # Handles both single standouts and clusters of multiple singles
                                    is_album_standout = (album_z >= STANDOUT_CONFIG['album_zscore_threshold'] and is_in_standout_cluster)
                                    # Artist-level stats using median + MAD
                                    artist_z = (score - artist_median) / artist_spread if artist_spread > 0 else 0
                                    is_artist_standout = (artist_z >= STANDOUT_CONFIG['artist_zscore_threshold'] and artist_percentile(score) <= STANDOUT_CONFIG['artist_top_percentile'])
                                    # Star rating assignment
                                    # 5★ requires album z >= 1.0 (high confidence) + artist standout
                                    if is_album_standout and is_artist_standout and album_z >= STANDOUT_CONFIG['star_5']['album_z']:
                                        star = 5
                                    elif is_album_standout and (artist_z >= STANDOUT_CONFIG['star_4']['artist_z'] or artist_percentile(score) <= STANDOUT_CONFIG['star_4']['artist_pct']):
                                        star = 4
                                    elif is_album_standout:
                                        star = 3
                                    elif track_album_median and score > track_album_median:
                                        star = 2
                                    else:
                                        star = 1
                                    cursor.execute(f"""
                                        UPDATE tracks SET is_standout_track = {placeholder}, artist_z_score = {placeholder}, stars = {placeholder}
                                        WHERE id = {placeholder}
                                    """, (1 if is_album_standout or is_artist_standout else 0, artist_z, star, track_id))
                                    log_debug(f"Track: {track_title} | Score: {score:.1f} | Album_z: {album_z:.2f} | Artist_z: {artist_z:.2f} | Album_standout: {is_album_standout} | Artist_standout: {is_artist_standout} | Star: {star}")
                                conn.commit()
                    except Exception as e:
                        log_debug(f'Standout/star rating analysis failed for {artist}: {e}')
                    # --- End standout/star rating section ---

                # BULK TAG LOOKUP: Fetch tags from ListenBrainz bulk-tag-lookup API
                # This enriches tracks with community-aggregated MusicBrainz tags/genres
                # Run BEFORE single detection to ensure all metadata is available
                try:
                    if HAVE_MUSICBRAINZ:  # Only proceed if MusicBrainz support available
                        # Collect recording MBIDs from all tracks that have them
                        mbid_list = []
                        track_mbid_map = {}  # Map MBID -> track_id for updating results
                        
                        for track in album_tracks:
                            track_mbid = track.get('mbid')
                            if track_mbid and track_mbid.strip():  # Only include non-empty MBIDs
                                mbid_list.append(track_mbid)
                                track_mbid_map[track_mbid] = track.get('id')
                        
                        if mbid_list:
                            log_debug(f"Bulk tag lookup: Collected {len(mbid_list)} MBIDs from {album} tracks")
                            
                            # Batch MBIDs in groups of 50 (conservative limit for URL length)
                            BATCH_SIZE = 50
                            all_results = {}
                            
                            for batch_start in range(0, len(mbid_list), BATCH_SIZE):
                                batch_mbids = mbid_list[batch_start:batch_start + BATCH_SIZE]
                                mbid_query = ",".join(batch_mbids)  # Comma-separated for API
                                
                                try:
                                    # Call ListenBrainz bulk-tag-lookup API
                                    lb_url = "https://labs.api.listenbrainz.org/bulk-tag-lookup/json"
                                    params = {"recording_mbid": mbid_query}
                                    
                                    res = session.get(lb_url, params=params, timeout=(5, 15))
                                    res.raise_for_status()
                                    batch_results = res.json()
                                    
                                    # Merge batch results
                                    if batch_results:
                                        all_results.update(batch_results)
                                    
                                    log_debug(f"Bulk tag lookup batch {batch_start}-{batch_start + len(batch_mbids)}: Fetched tags for {len(batch_results)} recording(s)")
                                except Exception as batch_error:
                                    log_debug(f"Bulk tag lookup API call failed for batch {batch_start}-{batch_start + len(batch_mbids)}: {batch_error}")
                                    # Continue with next batch on error
                                    continue
                            
                            # Process and store results
                            if all_results:
                                tags_updated = 0
                                for mbid, tag_data in all_results.items():
                                    track_id = track_mbid_map.get(mbid)
                                    if not track_id:
                                        continue
                                    
                                    # Extract tags from result (format depends on API response structure)
                                    # ListenBrainz returns tag data that includes popularity/count info
                                    tags = []
                                    if isinstance(tag_data, dict):
                                        # If it's a dict, try to get tags from various possible keys
                                        if "tags" in tag_data:
                                            tags = tag_data.get("tags", [])
                                            if isinstance(tags, dict):
                                                tags = list(tags.keys()) if tags else []
                                        elif "tag" in tag_data:
                                            tags = tag_data.get("tag", [])
                                    elif isinstance(tag_data, list):
                                        # If it's already a list, use it directly
                                        tags = tag_data
                                    
                                    if tags:
                                        # Convert tags to JSON format matching existing genre columns
                                        tags_json = json.dumps(tags)
                                        
                                        # Merge with existing musicbrainz_genres if present
                                        cursor.execute(
                                            f"SELECT musicbrainz_genres FROM tracks WHERE id = {placeholder}",
                                            (track_id,)
                                        )
                                        existing = cursor.fetchone()
                                        existing_tags = []
                                        
                                        if existing and existing[0]:
                                            try:
                                                existing_tags = json.loads(existing[0])
                                                if not isinstance(existing_tags, list):
                                                    existing_tags = [existing_tags]
                                            except (json.JSONDecodeError, TypeError):
                                                existing_tags = []
                                        
                                        # Merge and deduplicate tags
                                        merged_tags = list(dict.fromkeys(existing_tags + tags))  # Preserves order, removes duplicates
                                        merged_json = json.dumps(merged_tags)
                                        
                                        # Update track with merged tags
                                        cursor.execute(
                                            f"UPDATE tracks SET musicbrainz_genres = {placeholder} WHERE id = {placeholder}",
                                            (merged_json, track_id)
                                        )
                                        tags_updated += 1
                                
                                if tags_updated > 0:
                                    conn.commit()
                                    log_info(f"Bulk tag lookup: Updated {tags_updated} tracks with ListenBrainz tags for \"{artist} - {album}\"")
                                    log_debug(f"Bulk tag lookup: Merged tags with existing musicbrainz_genres for {tags_updated} track(s)")
                        else:
                            log_debug(f"Bulk tag lookup: No MBIDs found for album tracks, skipping")
                    else:
                        log_debug(f"Bulk tag lookup: MusicBrainz not available, skipping ListenBrainz tags")
                except Exception as e:
                    log_debug(f"Bulk tag lookup failed for \"{artist} - {album}\": {e}")
                    # Continue with single detection even if bulk tag lookup fails
                # --- End bulk tag lookup section ---

                # CRITICAL FIX: Close the connection BEFORE single detection to prevent lock contention
                # The original cursor from line ~1949 holds a READ lock on the database.
                # When detect_single_for_track() creates NEW connections to WRITE, those need to acquire
                # a WRITE lock, which SQLite cannot grant while a connection with a READ lock is open.
                # This causes "Database is locked" errors in store_single_detection_result().
                # Solution: Close the entire connection (not just cursor) to fully release all locks.
                # We'll reopen it after single detection completes.
                try:
                    if cursor is not None:
                        try:
                            cursor.close()
                        except:
                            pass  # Cursor might already be closed, that's OK
                    cursor = None
                    
                    if conn is not None:
                        try:
                            conn.close()
                        except:
                            pass  # Connection might have issues, that's OK
                    conn = None
                    log_debug(f"Closed connection before single detection to prevent lock contention")
                except Exception as e:
                    log_debug(f"Warning: Failed to close connection before single detection: {e}")
                    cursor = None
                    conn = None

                # Perform singles detection for album tracks
                log_info(f'Starting singles detection for "{artist} - {album}"')
                
                # Use album type already detected at the start of scan (no need to re-fetch from Music Brainz)
                # The album_type_from_field was set at scan start with MusicBrainz lookup + auto-detection
                spotify_album_type = row_get(album_tracks[0] if album_tracks else {}, 'spotify_album_type', '')
                
                # Use the type that was detected and stored at the start of the scan
                album_type = album_type_from_field or spotify_album_type or 'album'
                type_source = "populated-at-scan-start"
                
                is_compilation = is_compilation_type(album_type)
                log_debug(f'Album context: {len(album_tracks)} total tracks, compilation={is_compilation}, album_type={album_type} (source: {type_source})')
                singles_detected = 0
                
                # Log which sources are available for single detection
                discogs_client_available = bool(discogs_token and _get_timeout_safe_discogs_client(discogs_token))
                sources_available = []
                sources_available.append("Spotify")
                if HAVE_MUSICBRAINZ:
                    sources_available.append("MusicBrainz")
                if discogs_client_available:
                    sources_available.append("Discogs")
                if discogs_client_available:
                    sources_available.append("Discogs Video")
                log_info(f'Available sources for single detection: {[", ".join(sources_available)]}')
                log_debug(f'Source details: Spotify=enabled, MB={HAVE_MUSICBRAINZ}, Discogs={discogs_client_available}, Video={discogs_client_available}')
                
                # Calculate artist-level popularity statistics BEFORE single detection
                # Reason: We need to determine if this album is underperforming vs the artist's catalog
                # so that z-score single detection can be conditionally disabled for underperforming albums
                # while still using metadata-based detection (Discogs, Spotify, MusicBrainz).
                artist_stats = calculate_artist_popularity_stats(artist, conn)
                
                # Log artist statistics for reference in single detection decisions
                log_debug(f'Artist stats - track_count: {artist_stats["track_count"]}, mean: {artist_stats["avg_popularity"]:.1f}, median: {artist_stats["median_popularity"]:.1f}, stddev: {artist_stats["stddev_popularity"]:.1f}')
                
                # Add top 10% threshold from Last.fm context (from earlier pre-fetch)
                # This allows star rating to use dual criteria: global top 10% + album outlier
                artist_stats['top_10_percentile_threshold'] = artist_lastfm_context.get('top_10_percentile_threshold', 0)
                artist_stats['total_artist_tracks'] = artist_lastfm_context.get('total_tracks', 0)
                
                artist_median = artist_stats['median_popularity'] if artist_stats['track_count'] > 0 else 0.0
                
                # Calculate album median to check for underperformance
                # This enables conditional z-score detection: disabled for underperforming albums,
                # except when a track is a standout across the entire artist catalogue.
                # NOTE: Filters out live/remix/alternate versions to ensure album median reflects
                # the core album and is not skewed by bonus tracks.
                album_is_underperforming = False
                if artist_stats['track_count'] > MIN_TRACKS_FOR_ARTIST_COMPARISON:
                    # Use tracks already loaded in album_tracks instead of querying by artist
                    # This works for both regular artists and compilation albums
                    album_pops = []
                    for track in album_tracks:
                        popularity_score = row_get(track, 'popularity_score', 0)
                        title = row_get(track, 'title', '')
                        album_name = row_get(track, 'album', '')
                        
                        # Exclude live/remix/alternate versions from album median calculation
                        if popularity_score > 0 and not should_exclude_track_from_stats(title, album_name):
                            album_pops.append(popularity_score)
                    
                    if album_pops and artist_median > 0:
                        album_median = median(album_pops)
                        # Consider album underperforming if median is < UNDERPERFORMING_THRESHOLD of artist median
                        if album_median < (artist_median * UNDERPERFORMING_THRESHOLD):
                            album_is_underperforming = True
                            log_info(f"Album is underperforming: median={album_median:.1f} vs artist median={artist_median:.1f}")
                            log_info(f"Z-score single detection will be disabled except for artist-level standouts")
                            log_debug(f"Underperforming album detected - album_median: {album_median}, artist_median: {artist_median}, threshold: {UNDERPERFORMING_THRESHOLD}")
                
                if artist_stats['track_count'] > 0:
                    log_info(f"Artist-level stats: avg={artist_stats['avg_popularity']:.1f}, median={artist_median:.1f}")
                    log_debug(f"Artist statistics - track_count: {artist_stats['track_count']}, avg: {artist_stats['avg_popularity']}, median: {artist_median}, stddev: {artist_stats.get('stddev_popularity', 0)}")
                
                # Capture user-set singles before running automated detection
                # User-marked singles (is_single=1 with no/empty sources) should be preserved
                user_set_singles = set()
                for track in album_tracks:
                    track_id = track["id"]
                    is_single = row_get(track, "is_single", 0)
                    single_sources_json = row_get(track, "single_sources", "[]")
                    
                    # Track is user-set if is_single=1 but has no automated sources
                    try:
                        sources = json.loads(single_sources_json) if single_sources_json else []
                        if is_single == 1 and (not sources or len(sources) == 0):
                            user_set_singles.add(track_id)
                            log_info(f"Preserving user-set single: {row_get(track, 'title', 'Unknown')}")
                            log_debug(f"User-set single detected - track_id: {track_id}, title: {row_get(track, 'title', 'Unknown')}")
                    except (json.JSONDecodeError, TypeError):
                        if is_single == 1:
                            user_set_singles.add(track_id)
                            log_info(f"Preserving user-set single (malformed sources): {row_get(track, 'title', 'Unknown')}")
                
                # Batch updates for singles detection
                singles_updates = []
                
                # Get album track count for context-based confidence adjustment
                album_track_count = len(album_tracks)
                
                # Calculate z-scores for all tracks to filter single detection
                # This enables performance optimization by only scanning tracks likely to be singles
                track_zscores = {}  # Map of track_id -> z-score
                top_cluster_tracks = set()  # Track IDs in top z-score cluster (instant 5★)
                album_median_popularity = 0.0  # Track album median for single detection filtering
                
                if album_type == "regular" and album_track_count > 1:
                    # Calculate z-scores for regular albums to filter single detection
                    album_pops_for_zscore = []
                    track_ids_for_zscore = []
                    
                    for track in album_tracks:
                        track_id = track["id"]
                        popularity_score = row_get(track, 'popularity_score', 0)
                        title = row_get(track, 'title', '')
                        album_name = row_get(track, 'album', '')
                        
                        # Exclude live/remix/alternate from z-score calculation
                        if popularity_score > 0 and not should_exclude_track_from_stats(title, album_name):
                            album_pops_for_zscore.append(popularity_score)
                            track_ids_for_zscore.append(track_id)
                    
                    if len(album_pops_for_zscore) > 1:
                        from statistics import mean as stat_mean, stdev as stat_stdev, median as stat_median
                        pop_mean = stat_mean(album_pops_for_zscore)
                        pop_stddev = stat_stdev(album_pops_for_zscore) if len(album_pops_for_zscore) > 1 else 0
                        album_median_popularity = stat_median(album_pops_for_zscore)
                        
                        if pop_stddev > 0:
                            # Calculate z-scores
                            for i, track_id in enumerate(track_ids_for_zscore):
                                z_score = (album_pops_for_zscore[i] - pop_mean) / pop_stddev
                                track_zscores[track_id] = z_score
                            
                            # Identify top cluster using simple z-score based approach
                            # Top cluster = tracks with z-score > 1.0 in the album
                            try:
                                if len(album_pops_for_zscore) > 1:
                                    # Use the already-calculated z-scores from earlier
                                    for track_id, z_score in track_zscores.items():
                                        if z_score > 1.0:
                                            top_cluster_tracks.add(track_id)
                                    
                                    log_debug(f"Top cluster detection (z > 1.0): {len(top_cluster_tracks)} track(s)")
                                    if top_cluster_tracks:
                                        log_info(f"Top z-score cluster identified: {len(top_cluster_tracks)} track(s) get instant 5★")
                                        log_debug(f"Top cluster track IDs: {top_cluster_tracks}")
                                    else:
                                        log_info(f"No tracks identified in top z-score cluster (z > 1.0) for album '{album}'")
                            except Exception as e:
                                log_info(f"Top cluster detection failed: {e}")
                                import traceback
                                log_debug(f"Traceback: {traceback.format_exc()}")
                
                log_debug(f"Album type: {album_type}, will filter single detection accordingly")
                
                # Track progress within singles detection phase
                singles_processed = 0
                # Pre-calculate milestone track counts for efficient checking
                singles_milestone_25 = int(album_track_count * 0.25)
                singles_milestone_50 = int(album_track_count * 0.50)
                singles_milestone_75 = int(album_track_count * 0.75)
                singles_milestones_logged = set()

                # Adaptive greatest-hits mode during single scan:
                # If all processed tracks are being detected as singles, switch to full detection
                # for the rest of the album (including tracks with negative z-scores).
                album_type_lower = (album_type or "").strip().lower()
                force_full_single_detection = (
                    album_type_lower in ("greatest_hits", "various_artists") or
                    is_compilation_type(album_type)
                )
                gh_tracks_processed = 0
                gh_tracks_detected_single = 0

                # Also honor prior state: if every track is already marked single, keep full detection enabled.
                pre_marked_singles = sum(1 for t in album_tracks if row_get(t, "is_single", 0) == 1)
                if album_track_count >= 5 and pre_marked_singles == album_track_count:
                    force_full_single_detection = True
                    if (album_type or "").strip().lower() == "regular":
                        album_type = "greatest_hits"
                    log_info(f'Greatest hits adaptive mode enabled from existing state: "{artist} - {album}" ({pre_marked_singles}/{album_track_count} tracks already marked single)')
                
                for track in album_tracks:
                    track_id = track["id"]
                    title = track["title"]
                    
                    log_debug(f"Processing single detection for track: {title} (ID: {track_id})")
                    
                    # Check single detection cache before running detection
                    single_manual_override = row_get(track, "single_manual_override", 0)
                    single_detection_last_updated = row_get(track, "single_detection_last_updated", None)
                    
                    # Skip re-detection if manually set by user
                    if single_manual_override:
                        log_debug(f"Single detection skipped (user override): {title}")
                        singles_processed += 1
                        continue
                    
                    # Check cache age unless force scanning
                    if not (FORCE_RESCAN or force) and single_detection_last_updated:
                        try:
                            last_run = datetime.fromisoformat(single_detection_last_updated)
                            age_hours = (datetime.now() - last_run).total_seconds() / 3600
                            
                            # Use confidence-based cache TTL
                            current_confidence = row_get(track, "single_confidence", "low")
                            if current_confidence == "high":
                                cache_ttl = 168  # 7 days
                            elif current_confidence == "medium":
                                cache_ttl = 72   # 3 days
                            else:
                                cache_ttl = 24   # 1 day for low confidence
                            
                            if age_hours < cache_ttl:
                                log_debug(f"Single detection cached: {title} (age: {age_hours:.1f}h, TTL: {cache_ttl}h, confidence: {current_confidence})")
                                singles_processed += 1
                                continue
                        except Exception as e:
                            log_debug(f"Failed to parse single detection timestamp: {e}")
                    
                    # Filter single detection based on album type and z-scores
                    # This dramatically speeds up scanning by skipping tracks unlikely to be singles
                    skip_single_detection = False
                    
                    album_type_norm = (album_type or "").strip().lower()
                    is_regular_album = album_type_norm in ("regular", "album", "lp", "studio")
                    
                    # Check if this is a greatest hits or compilation album
                    album_lower = album.lower()
                    greatest_hits_patterns = [
                        'greatest hits', 'best of', 'the best', 'collection', 'anthology',
                        'essentials', ' hits', 'singles', 'the very best', 'gold', 'platinum',
                        'ultimate collection', 'complete', 'definitive', 'various artists'
                    ]
                    is_greatest_hits_or_compilation = (
                        force_full_single_detection or
                        is_compilation or 
                        any(pattern in album_lower for pattern in greatest_hits_patterns)
                    )

                    # Filter single detection by z-score for regular albums
                    # Skip detection for tracks below album average (negative z-score) unless it's a special collection
                    if not is_greatest_hits_or_compilation:
                        # Get pre-calculated z-score (already computed above at line 4595)
                        track_zscore = track_zscores.get(track_id, 0.0)
                        
                        # For regular albums, skip single detection if z-score is negative (below album average)
                        # Rationale: Below-average tracks are unlikely to be real singles
                        # Exception: For compilations/greatest hits, run detection on all tracks (different popularity patterns)
                        if track_zscore < 0.0:
                            skip_single_detection = True
                            log_debug(f"Skipping single detection for '{title}' (z-score: {track_zscore:.2f} < 0.0 - below album average)")
                    else:
                        # Greatest hits/compilation/various artists: Run detection on all tracks
                        # These collections have different popularity patterns, so average tracks can still be genuine singles
                        log_debug(f"Greatest hits/compilation/various artists detected - running single detection on all tracks")
                    
                    # Skip single detection if filtered out
                    if skip_single_detection:
                        singles_processed += 1
                        continue
                    
                    # Get additional fields for advanced detection
                    track_isrc = row_get(track, "isrc", None)
                    track_duration = row_get(track, "duration", None)
                    track_album_type = row_get(track, "spotify_album_type", None)
                    
                    # Get the popularity score for this track (may have been calculated earlier)
                    # Open a fresh short-lived connection (conn was closed before this loop to release locks)
                    track_popularity = 0.0
                    try:
                        temp_conn = get_db_connection()
                        temp_cursor = temp_conn.cursor()
                        temp_is_pg = is_postgres_connection(temp_conn)
                        temp_placeholder = "%s" if temp_is_pg else "?"
                        temp_cursor.execute(f"SELECT popularity_score FROM tracks WHERE id = {temp_placeholder}", (track_id,))
                        pop_row = temp_cursor.fetchone()
                        if pop_row and pop_row['popularity_score']:
                            track_popularity = pop_row['popularity_score']
                        temp_cursor.close()
                        temp_conn.close()
                    except Exception as e:
                        log_debug(f"Could not fetch popularity for {track_id}: {e}")
                        track_popularity = 0.0
                    
                    log_debug(f"Single detection params - track: {title}, isrc: {track_isrc}, duration: {track_duration}, popularity: {track_popularity}, album_type: {track_album_type}")
                    
                    # Skip single detection for zero-popularity tracks (unless in compilation/greatest hits)
                    # Rationale: Tracks with 0 popularity are unlikely to be real singles, wastes API calls
                    # Exception: Always check compilations since featured tracks have different patterns
                    if track_popularity == 0 and not is_greatest_hits_or_compilation:
                        log_debug(f"Skipping single detection for '{title}' (popularity: 0 - not a compilation/greatest hits)")
                        singles_processed += 1
                        continue
                    
                    # Use canonical album grouping artist for single-detection context.
                    # This prevents featured tracks from being treated as isolated 1-track artist catalogs.
                    track_artist = artist
                    detection_result = detect_single_for_track(
                        title=title,
                        artist=track_artist,
                        album_track_count=album_track_count,
                        spotify_results_cache=spotify_results_cache,
                        verbose=verbose,  # Pass function parameter, not module constant
                        discogs_token=discogs_token,  # Pass already-loaded token
                        # Advanced detection parameters
                        track_id=track_id,
                        album=album,
                        isrc=track_isrc,
                        duration=track_duration,
                        popularity=track_popularity,
                        album_type=album_type,
                        use_advanced_detection=True,
                        zscore_threshold=0.20,
                        # Conditional z-score detection parameters
                        album_is_underperforming=album_is_underperforming,
                        artist_median_popularity=artist_median
                    )
                    
                    single_sources = detection_result["sources"]
                    single_confidence = detection_result["confidence"]
                    is_single = detection_result["is_single"]
                    
                    log_debug(f"Single detection result - is_single: {is_single}, confidence: {single_confidence}, sources: {single_sources}")
                    
                    # Update single detection timestamp after running detection
                    # Use a fresh short-lived connection (conn was closed before this loop to release locks)
                    try:
                        timestamp_conn = get_db_connection()
                        timestamp_is_pg = is_postgres_connection(timestamp_conn)
                        timestamp_placeholder = "%s" if timestamp_is_pg else "?"
                        timestamp_cursor = timestamp_conn.cursor()
                        timestamp_cursor.execute(f"""
                            UPDATE tracks 
                            SET single_detection_last_updated = {timestamp_placeholder}
                            WHERE id = {timestamp_placeholder}
                        """, (datetime.now().isoformat(), track_id))
                        timestamp_conn.commit()
                        timestamp_cursor.close()
                        timestamp_conn.close()
                    except Exception as e:
                        log_debug(f"Could not update detection timestamp for {track_id}: {e}")
                    
                    # Preserve user-set singles: if track was user-marked and detection found nothing, keep it marked
                    if track_id in user_set_singles and not is_single:
                        is_single = True
                        single_confidence = "user"  # Mark as user-set
                        # Keep sources empty to indicate user-set
                        log_info(f"Preserving user-set single flag for: {title}")
                        log_debug(f"User-set single preserved - track_id: {track_id}, auto_detection: False")

                    # Adaptive greatest-hits promotion during this same single scan:
                    # once every processed track is being detected as single, continue scanning all remaining tracks.
                    gh_tracks_processed += 1
                    if is_single:
                        gh_tracks_detected_single += 1
                    if (
                        not force_full_single_detection
                        and gh_tracks_processed >= 3
                        and gh_tracks_detected_single == gh_tracks_processed
                    ):
                        force_full_single_detection = True
                        if (album_type or "").strip().lower() == "regular":
                            album_type = "greatest_hits"
                        log_info(
                            f'Greatest hits adaptive mode enabled during single scan: "{artist} - {album}" '
                            f'({gh_tracks_detected_single}/{gh_tracks_processed} processed tracks detected as single)'
                        )
                        log_debug("Adaptive greatest hits mode now bypasses negative z-score skip for remaining tracks")
                    
                    # Queue single detection results for batch update
                    if is_single or single_sources:
                        # Deduplicate single_sources to prevent duplicate entries in JSON
                        # Preserves order while removing duplicates
                        unique_sources = []
                        seen = set()
                        for source in single_sources:
                            if source not in seen:
                                unique_sources.append(source)
                                seen.add(source)
                        
                        # Automatically set stars to 5 for detected singles
                        stars_for_single = 5 if is_single else None
                        singles_updates.append((
                            bool(is_single),
                            single_confidence,
                            json.dumps(unique_sources),  # Use deduplicated sources
                            stars_for_single,
                            track_id
                        ))
                        if is_single:
                            singles_detected += 1
                            if unique_sources:
                                source_str = ", ".join(unique_sources)
                                log_info(f"Single detected: {title} ({single_confidence} confidence, sources: {source_str})")
                            else:
                                log_info(f"Single detected: {title} (user-set)")
                            log_debug(f"Single detection confirmed - track_id: {track_id}, confidence: {single_confidence}, sources: {unique_sources}")
                    
                    # Track progress in singles detection
                    singles_processed += 1
                    # Efficient milestone checking using pre-calculated values
                    if singles_processed == singles_milestone_25 and 25 not in singles_milestones_logged:
                        log_unified(f"Single Detection - 25% completed - {singles_processed}/{album_track_count} tracks")
                        log_debug(f"Progress milestone - 25% completed for singles detection in album {album}")
                        singles_milestones_logged.add(25)
                    elif singles_processed == singles_milestone_50 and 50 not in singles_milestones_logged:
                        log_unified(f"Single Detection - 50% completed - {singles_processed}/{album_track_count} tracks")
                        log_debug(f"Progress milestone - 50% completed for singles detection in album {album}")
                        singles_milestones_logged.add(50)
                    elif singles_processed == singles_milestone_75 and 75 not in singles_milestones_logged:
                        log_unified(f"Single Detection - 75% completed - {singles_processed}/{album_track_count} tracks")
                        log_debug(f"Progress milestone - 75% completed for singles detection in album {album}")
                        singles_milestones_logged.add(75)
                
                # REOPEN CONNECTION: Single detection completed, need to reconnect for batch updates
                # We closed the connection before single detection to prevent lock contention.
                # Now we need to reopen it to perform batch updates of the singles detection results.
                try:
                    if conn is None:
                        conn = get_db_connection()
                        log_debug(f"Reopened connection after singles detection for batch updates")
                    if cursor is not None and not cursor._closed:
                        cursor.close()
                    cursor = conn.cursor()
                    log_debug(f"Reset cursor after single detection loop for batch updates")
                except Exception as e:
                    log_debug(f"Warning: Failed to reset cursor after singles: {e}")
                    # Continue anyway, the connection will still work
                
                # Batch update all singles detection results for this album in one commit
                if singles_updates:
                    # Update with conditional stars setting - only set stars if value is provided (detected singles)
                    for is_single, single_confidence, single_sources, stars_value, track_id in singles_updates:
                        if stars_value is not None:
                            # Set both single status and stars for detected singles
                            cursor.execute(
                                f"""UPDATE tracks 
                                SET is_single = {placeholder}, single_confidence = {placeholder}, single_sources = {placeholder}, stars = {placeholder}
                                WHERE id = {placeholder}""",
                                (is_single, single_confidence, single_sources, stars_value, track_id)
                            )
                        else:
                            # Only set single status if no stars update needed
                            cursor.execute(
                                f"""UPDATE tracks 
                                SET is_single = {placeholder}, single_confidence = {placeholder}, single_sources = {placeholder}
                                WHERE id = {placeholder}""",
                                (is_single, single_confidence, single_sources, track_id)
                            )
                    conn.commit()
                    log_debug(f"Batch committed {len(singles_updates)} singles detection results for album '{album}'")
                
                # COVER DETECTION: Detect and mark cover songs based on writer/lyricist uniqueness
                if HAVE_COVER_DETECTOR and not singles_only:
                    try:
                        log_info(f'Starting cover detection for album "{artist} - {album}"')
                        
                        # Get all tracks for this album with metadata
                        # Note: Use COALESCE to handle album_artist grouping like singles detection does
                        cursor.execute(f"""
                            SELECT id, title, artist, writer, mbid 
                            FROM tracks 
                            WHERE COALESCE(NULLIF(album_artist, ''), artist) = {placeholder} AND album = {placeholder}
                            ORDER BY COALESCE(track_number, 0), title
                        """, (artist, album))
                        album_tracks_for_cover = [dict(row) for row in cursor.fetchall()]
                        
                        if album_tracks_for_cover:
                            # Instantiate cover detector with database connection and clients
                            cover_detector = CoverDetector(db_connection=conn, musicbrainz_client=_get_timeout_safe_musicbrainz_client())
                            
                            # Run cover detection for this album
                            covers_detected = cover_detector.detect_covers_for_album(artist, album, album_tracks_for_cover)
                            
                            if covers_detected:
                                log_info(f'Cover detection complete: {len(covers_detected)} cover(s) detected for "{artist} - {album}"')
                                log_debug(f'Cover detection results: {covers_detected}')
                                # Already committed by CoverDetector, no need to commit again
                                covers_found_count = len(covers_detected)
                            else:
                                log_debug(f'No covers detected for album "{artist} - {album}"')
                                covers_found_count = 0
                        else:
                            log_debug(f'No tracks found for cover detection in album "{artist} - {album}"')
                            covers_found_count = 0
                            
                    except Exception as e:
                        log_debug(f'Cover detection failed for album "{artist} - {album}": {e}')
                        import traceback
                        log_debug(f'Cover detection error traceback: {traceback.format_exc()}')
                        covers_found_count = 0
                else:
                    covers_found_count = 0
                    if singles_only:
                        log_debug(f'Skipping cover detection (singles_only mode active)')
                    elif not HAVE_COVER_DETECTOR:
                        log_debug(f'Skipping cover detection (CoverDetector module unavailable)')
                
                # Log summary of singles detection
                high_conf_count = sum(1 for update in singles_updates if update[0] == 1)
                log_info(f'Singles detection complete: {singles_detected} high-confidence single(s) detected for "{artist} - {album}" ({singles_processed} tracks processed)')
                log_debug(f'Singles detection summary - high_conf: {high_conf_count}, total_processed: {singles_processed}, total_updated: {len(singles_updates)}')

                # Auto-detect Greatest Hits: if every track on the album is now marked as a single,
                # treat the album as a greatest hits collection for scan behavior.
                try:
                    # Use conditional query for PostgreSQL vs SQLite
                    is_pg = is_postgres_connection(cursor.connection)
                    if is_pg:
                        query = f"""
                        SELECT COUNT(*) AS total_tracks,
                               SUM(CASE WHEN is_single THEN 1 ELSE 0 END) AS single_tracks
                        FROM tracks
                        WHERE COALESCE(NULLIF(album_artist, ''), artist) = {placeholder}
                          AND album = {placeholder}
                        """
                        cursor.execute(query, (artist, album))
                    else:
                        query = f"""
                        SELECT COUNT(*) AS total_tracks,
                               SUM(CASE WHEN COALESCE(is_single, 0) = 1 THEN 1 ELSE 0 END) AS single_tracks
                        FROM tracks
                        WHERE COALESCE(NULLIF(album_artist, ''), artist) = {placeholder}
                          AND album = {placeholder}
                        """
                        cursor.execute(query, (artist, album))
                    gh_row = cursor.fetchone()
                    total_tracks = row_get(gh_row, "total_tracks", 0) if gh_row else 0
                    single_tracks = row_get(gh_row, "single_tracks", 0) if gh_row else 0

                    if total_tracks >= 5 and single_tracks == total_tracks and album_type not in ("various_artists", "compilation"):
                        album_type = "greatest_hits"
                        log_info(f'Auto-detected Greatest Hits by singles ratio: "{artist} - {album}" ({single_tracks}/{total_tracks} tracks marked single)')
                        log_debug(f'Greatest hits auto-detect applied after singles detection - album_type set to {album_type}')
                except Exception as e:
                    log_debug(f'Could not evaluate singles-ratio greatest hits auto-detect for "{artist} - {album}": {e}')

                # POST-PROCESSING: Detect album-level z-score outliers as medium confidence singles
                # These are tracks that are strong album standouts: zscore >= 2.0 AND popularity >> album mean
                # This works alongside existing single detection to identify standout album tracks
                try:
                    cursor.execute(f"""
                        SELECT id, title, popularity_score, single_confidence, single_sources, is_single
                        FROM tracks 
                        WHERE COALESCE(NULLIF(album_artist, ''), artist) = {placeholder} AND album = {placeholder}
                        ORDER BY popularity_score DESC
                    """, (artist, album))
                    
                    zscore_update_tracks = cursor.fetchall()
                    
                    if zscore_update_tracks:
                        # Calculate album statistics for z-score
                        from statistics import median as stat_median_post
                        album_pops = [t["popularity_score"] for t in zscore_update_tracks if t["popularity_score"]]
                        
                        if album_pops and len(album_pops) > 1:
                            # Use median + MAD for robust z-score calculation
                            MIN_SPREAD = 10.0  # Prevent flat albums from over-amplifying small differences
                            album_pop_median = stat_median_post(album_pops)
                            
                            # Calculate MAD (Median Absolute Deviation)
                            absolute_deviations = [abs(pop - album_pop_median) for pop in album_pops]
                            album_pop_mad = stat_median_post(absolute_deviations)
                            # Scale MAD to be comparable to standard deviation (1.4826 for normal distribution)
                            album_pop_mad_scaled = album_pop_mad * 1.4826
                            # Apply MIN_SPREAD floor to prevent flat-album over-amplification
                            album_pop_spread = max(album_pop_mad_scaled, MIN_SPREAD)
                            
                            zscore_outliers = []
                            for track in zscore_update_tracks:
                                track_id = track["id"]
                                track_title = track["title"]
                                track_pop = track["popularity_score"] or 0
                                track_single_confidence = track["single_confidence"] or ""
                                track_is_single = track["is_single"] or 0
                                track_sources_json = track["single_sources"] or "[]"
                                
                                # Skip high confidence - those are already confirmed
                                if track_single_confidence == "high":
                                    continue
                                
                                # Calculate album z-score using median + MAD
                                if album_pop_spread > 0:
                                    album_zscore =(track_pop - album_pop_median) / album_pop_spread
                                else:
                                    album_zscore = 0
                                
                                # Check if this is a strong album outlier
                                if album_zscore >= 2.0 and track_pop > (album_pop_median * 1.5):
                                    # This is a strong standout - mark as medium confidence unless already marked
                                    if not track_single_confidence or track_single_confidence == "low":
                                        try:
                                            track_sources = json.loads(track_sources_json) if track_sources_json else []
                                        except json.JSONDecodeError:
                                            track_sources = []
                                        
                                        # Add zscore as detection source if not already present
                                        if "album_zscore" not in track_sources:
                                            track_sources.append("album_zscore")
                                        
                                        # Update to medium confidence
                                        zscore_outliers.append((
                                            "medium",
                                            json.dumps(track_sources),
                                            track_id
                                        ))
                                        
                                        log_debug(f"Album z-score detection: {track_title} (zscore={album_zscore:.2f}, pop={track_pop:.1f} vs median={album_pop_median:.1f})")
                            
                            # Batch update z-score outliers
                            if zscore_outliers:
                                for single_confidence, sources, track_id in zscore_outliers:
                                    cursor.execute(f"""
                                        UPDATE tracks 
                                        SET single_confidence = {placeholder}, single_sources = {placeholder}
                                        WHERE id = {placeholder}
                                    """, (single_confidence, sources, track_id))
                                
                                conn.commit()
                                log_info(f"Album z-score detection: {len(zscore_outliers)} medium-confidence track(s) identified for '{artist} - {album}'")
                                log_debug(f"Z-score outliers updated: {len(zscore_outliers)} tracks")
                
                except Exception as e:
                    log_debug(f"Album z-score detection failed for '{album}': {e}")
                    import traceback
                    log_debug(f"Z-score detection error: {traceback.format_exc()}")

                # Calculate star ratings for album tracks
                log_info(f'Calculating star ratings for "{artist} - {album}"')
                log_debug(f'Star rating calculation starting for album: {album}')
                
                # Note: artist_stats was already calculated before single detection to support
                # conditional z-score detection. We only need to update the database here.
                # Just update the artist_stats table with popularity statistics
                if artist_stats['track_count'] > 0:
                    # Update artist_stats table with popularity statistics
                    # Columns: mean_popularity, median_popularity, popularity_stddev, popularity_mad
                    cursor.execute(f"""
                        UPDATE artist_stats 
                        SET mean_popularity = {placeholder}, median_popularity = {placeholder}, popularity_stddev = {placeholder}, popularity_mad = {placeholder}
                        WHERE artist_name = {placeholder}
                    """, (artist_stats['avg_popularity'], artist_stats['median_popularity'], 
                          artist_stats['stddev_popularity'], artist_stats['mad_popularity'], artist))
                    conn.commit()
                    log_debug(f"Updated artist_stats table for {artist} - mean: {artist_stats['avg_popularity']:.1f}, median: {artist_stats['median_popularity']:.1f}, stddev: {artist_stats['stddev_popularity']:.1f}, MAD: {artist_stats['mad_popularity']:.1f}")
                
                # Get all tracks for this album with their popularity scores and single detection
                # Try matching on artist field first, then fall back to album_artist field
                cursor.execute(
                    f"SELECT id, title, popularity_score, is_single, single_confidence, single_sources, lastfm_track_playcount, is_standout_track FROM tracks WHERE COALESCE(NULLIF(album_artist, ''), artist) = {placeholder} AND album = {placeholder} ORDER BY popularity_score DESC",
                    (artist, album)
                )
                album_tracks_with_scores = [dict(row) for row in cursor.fetchall()]
                
                log_debug(f"Retrieved {len(album_tracks_with_scores)} tracks for star rating calculation")
                
                if album_tracks_with_scores and len(album_tracks_with_scores) > 0:
                    log_debug(f"Processing {len(album_tracks_with_scores)} tracks for star ratings and single detection logging")
                    # Calculate star ratings using the same logic as sptnr.py
                    total_tracks = len(album_tracks_with_scores)
                    band_size = math.ceil(total_tracks / 4)
                    
                    # Identify tracks to exclude from statistics (e.g., bonus tracks with parentheses at end)
                    # Pass alternate_takes_map to exclude those tracks as well
                    excluded_indices = should_exclude_from_stats(album_tracks_with_scores, alternate_takes_map)
                    
                    # Calculate statistics for popularity-based confidence system
                    scores = [t["popularity_score"] if t["popularity_score"] else 0 for t in album_tracks_with_scores]
                    # Filter out excluded tracks when calculating statistics
                    # Complexity is O(n) for iteration; set membership testing is O(1)
                    valid_scores = [s for i, s in enumerate(scores) if s > 0 and i not in excluded_indices]
                    
                    # Log exclusions if any
                    if excluded_indices:
                        excluded_titles = [album_tracks_with_scores[i]["title"] for i in excluded_indices]
                        log_info(f"Excluding {len(excluded_indices)} tracks from statistics: {', '.join(excluded_titles)}")
                        log_debug(f"Excluded track indices: {excluded_indices}")
                    
                    # Note: album_is_underperforming was already calculated before single detection
                    # to support conditional z-score detection. It's not needed for star rating calculation.
                    # The underperformance flag was already used during single detection to determine
                    # whether to apply z-score based single detection for each track.
                    
                    # Import with aliases to avoid shadowing issues from local imports elsewhere
                    from statistics import median as stat_median
                    
                    # MIN_SPREAD floor to prevent flat albums from over-amplifying small differences
                    MIN_SPREAD = 10.0
                    
                    if valid_scores:
                        # Use median + MAD for robust z-score calculation
                        popularity_median = stat_median(valid_scores)
                        
                        # Calculate MAD (Median Absolute Deviation)
                        absolute_deviations = [abs(score - popularity_median) for score in valid_scores]
                        popularity_mad = stat_median(absolute_deviations)
                        # Scale MAD to be comparable to standard deviation (1.4826 for normal distribution)
                        popularity_mad_scaled = popularity_mad * 1.4826
                        # Apply MIN_SPREAD floor
                        popularity_spread = max(popularity_mad_scaled, MIN_SPREAD)
                        
                        log_debug(f"Star rating statistics - median: {popularity_median}, MAD: {popularity_mad_scaled:.1f}, spread (with floor): {popularity_spread:.1f}, valid_scores_count: {len(valid_scores)}")
                        
                        # Calculate z-scores for all tracks using median+MAD
                        zscores = []
                        for score in valid_scores:
                            if popularity_spread > 0:
                                zscore = (score - popularity_median) / popularity_spread
                            else:
                                zscore = 0
                            zscores.append(zscore)
                        
                        # Get mean of top 50% z-scores for medium confidence threshold
                        # Use heapq.nlargest for efficiency with large albums
                        if zscores:
                            top_50_count = max(1, len(zscores) // 2)
                            top_50_zscores = heapq.nlargest(top_50_count, zscores)
                            from statistics import mean as stat_mean
                            mean_top50_zscore = stat_mean(top_50_zscores)
                        else:
                            mean_top50_zscore = 0
                        
                        # Load z-score thresholds from config
                        zscore_thresholds = get_zscore_thresholds()
                        high_conf_threshold = popularity_median + zscore_thresholds['high']
                        medium_conf_zscore_threshold = mean_top50_zscore + zscore_thresholds['medium']
                        
                        log_info(f"Album stats: median={popularity_median:.1f}, MAD={popularity_mad_scaled:.1f}, spread={popularity_spread:.1f}")
                        log_debug(f"Confidence thresholds - high: {high_conf_threshold:.1f}, medium_zscore: {medium_conf_zscore_threshold:.2f}")
                    else:
                        popularity_median = DEFAULT_POPULARITY_MEAN
                        popularity_spread = 0
                        zscore_thresholds = get_zscore_thresholds()
                        high_conf_threshold = DEFAULT_POPULARITY_MEAN + zscore_thresholds['high']
                        medium_conf_zscore_threshold = zscore_thresholds['medium']
                        log_debug(f"Using default thresholds - no valid scores found")
                    
                    # Calculate median score for band-based threshold (legacy)
                    median_score = median(scores) if scores else DEFAULT_POPULARITY_MEAN
                    if median_score == 0:
                        median_score = DEFAULT_POPULARITY_MEAN
                    jump_threshold = median_score * 1.7
                    log_debug(f"Band-based thresholds - median: {median_score}, jump_threshold: {jump_threshold}")
                    
                    star_distribution = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
                    # Keep the exact z-score used during this star pass for unified logging.
                    # This avoids mixing artist-level z-scores with album-level z-scores in output.
                    star_calc_zscores = {}
                    
                    # Batch updates for better performance
                    updates = []
                    # Track which medium-confidence tracks should be upgraded to is_single=1
                    single_upgrades = []
                    # Popularity-only 5★ promotions require a strong outlier signal.
                    # Keep this stricter than album standout tagging to avoid over-promotion.
                    popularity_5star_z_threshold = 2.0

                    
                    for i, track_row in enumerate(album_tracks_with_scores):
                        track_id = track_row["id"]
                        title = track_row["title"]
                        popularity_score = track_row["popularity_score"] if track_row["popularity_score"] else 0
                        is_single = track_row["is_single"] if track_row["is_single"] else 0
                        single_confidence = track_row["single_confidence"] if track_row["single_confidence"] else "low"
                        single_sources_json = track_row["single_sources"] if track_row["single_sources"] else "[]"
                        is_standout_track = track_row["is_standout_track"] if track_row["is_standout_track"] is not None else 0
                        
                        # Parse single sources (defensive check for valid string)
                        try:
                            if single_sources_json and isinstance(single_sources_json, str):
                                single_sources = json.loads(single_sources_json)
                            else:
                                single_sources = []
                        except json.JSONDecodeError:
                            single_sources = []
                            log_debug(f"Failed to parse single_sources JSON for track {track_id}")
                        has_iterative_zscore = "iterative_zscore" in single_sources
                        
                        # Check if this track was excluded from statistics
                        # Excluded tracks should not participate in confidence-based star rating upgrades
                        is_excluded_track = i in excluded_indices
                        
                        # Calculate z-score for this track using median + MAD
                        if popularity_spread > 0 and popularity_score > 0:
                            track_zscore = (popularity_score - popularity_median) / popularity_spread
                        else:
                            track_zscore = 0
                        star_calc_zscores[track_id] = track_zscore
                        
                        log_debug(f"Track star rating calc - {title}: popularity={popularity_score}, zscore={track_zscore}, excluded={is_excluded_track}")
                        
                        # Calculate band-based star rating (baseline)
                        band_index = i // band_size
                        stars = max(1, 4 - band_index)
                        
                        # Track whether 5★ assignment came from popularity logic vs single detection
                        # Used to determine if downgrade logic should apply
                        is_popularity_based_5star = False
                        
                        # SIMPLIFIED 5-STAR LOGIC BASED ON Z-SCORE AND CONFIDENCE
                        # Rules:
                        # 1. z-score 0-1: requires 2 medium confidence sources OR 1 high confidence source
                        # 2. z-score > 1: requires 1 medium confidence source OR 1 high confidence source
                        # 3. z-score > 2: may qualify as popularity-only 5★ based on CURRENT run z-score (not persisted status)
                        
                        # Skip confidence-based upgrades for excluded tracks (e.g., bonus tracks with parentheses)
                        # These tracks were excluded from statistics calculation, so their z-scores are not meaningful
                        if not is_excluded_track:
                            # Count only medium-confidence evidence sources.
                            # Do not count internal markers (e.g. iterative_zscore/version_count)
                            # toward the "2 medium sources" rule for z-score < 1 tracks.
                            medium_conf_eligible_sources = {
                                "spotify",
                                "musicbrainz",
                                "musicbrainz_video",
                                "musicbrainz_compilation",
                                "discogs",
                                "discogs_video",
                                "lastfm",
                            }
                            medium_conf_count = (
                                len([s for s in single_sources if s in medium_conf_eligible_sources])
                                if single_sources and single_confidence == "medium"
                                else 0
                            )
                            has_high_confidence = (single_confidence == "high" or single_confidence == "user")
                            
                            # Never trust persisted "popular" confidence for star assignment.
                            # It is historical and can become stale when popularity shifts between scans.
                            if single_confidence == "popular":
                                log_debug(
                                    f"Ignoring persisted popular confidence for star assignment: {title} "
                                    f"(track_id: {track_id})"
                                )
                                single_confidence = "low"

                            # Apply simplified z-score + confidence rules
                            if single_confidence == "user":
                                # User-set singles always get 5 stars
                                stars = 5
                                log_info(f"5-star assignment: {title} (user-set single)")
                                log_debug(f"User-set single - track_id: {track_id}")
                            elif single_confidence == "high":
                                # High confidence always gets 5 stars (z-score agnostic)
                                stars = 5
                                log_info(f"5-star assignment: {title} (high-confidence single, zscore={track_zscore:.2f})")
                                log_debug(f"High confidence single detected - track_id: {track_id}")
                            elif single_confidence == "medium":
                                # Medium confidence: z-score determines requirements
                                if track_zscore > 1.0:
                                    # z-score > 1: only 1 medium source needed
                                    if medium_conf_count >= 1:
                                        stars = 5
                                        if not is_single:
                                            single_upgrades.append(track_id)
                                            log_info(f"5-star assignment: {title} ({medium_conf_count} medium-confidence source + z-score={track_zscore:.2f} > 1.0) - upgraded to single")
                                        else:
                                            log_info(f"5-star assignment: {title} ({medium_conf_count} medium-confidence source + z-score={track_zscore:.2f} > 1.0)")
                                        log_debug(f"Medium confidence with z > 1.0 - track_id: {track_id}, zscore: {track_zscore:.2f}")
                                    else:
                                        # Medium confidence but no sources
                                        stars = 3
                                        log_info(f"3-star assignment: {title} (medium confidence, zscore={track_zscore:.2f})")
                                elif track_zscore >= 0.0:
                                    # z-score 0-1: requires 2 medium sources
                                    if medium_conf_count >= 2:
                                        stars = 5
                                        if not is_single:
                                            single_upgrades.append(track_id)
                                            log_info(f"5-star assignment: {title} ({medium_conf_count} medium-confidence sources + z-score={track_zscore:.2f}) - upgraded to single")
                                        else:
                                            log_info(f"5-star assignment: {title} ({medium_conf_count} medium-confidence sources + z-score={track_zscore:.2f})")
                                        log_debug(f"Medium confidence with 2+ sources - track_id: {track_id}, zscore: {track_zscore:.2f}")
                                    else:
                                        # Only 1 medium source with z-score < 1.0
                                        stars = 3
                                        log_info(f"3-star assignment: {title} ({medium_conf_count} medium-confidence source(s), zscore={track_zscore:.2f} < 1.0)")
                                        log_debug(f"Medium confidence insufficient - track_id: {track_id}, sources: {medium_conf_count}, zscore: {track_zscore:.2f}")
                                else:
                                    # Negative z-score
                                    stars = 3
                                    log_info(f"3-star assignment: {title} (medium confidence, negative zscore={track_zscore:.2f})")

                            # Popularity-only 5★ must be recomputed every scan from current z-score,
                            # never from persisted confidence flags.
                            has_metadata_single_source = any(
                                s in ["musicbrainz", "discogs", "discogs_video", "spotify", "lastfm"]
                                for s in single_sources
                            )
                            if (
                                stars < 5
                                and not has_high_confidence
                                and not has_metadata_single_source
                                and track_zscore >= popularity_5star_z_threshold
                            ):
                                stars = 5
                                is_popularity_based_5star = True
                                log_info(
                                    f"5-star assignment: {title} "
                                    f"(current popularity outlier, z-score={track_zscore:.2f})"
                                )
                                log_debug(
                                    f"Popularity-only 5★ (recomputed) - track_id: {track_id}, "
                                    f"zscore: {track_zscore:.2f}, single_confidence: {single_confidence}"
                                )
                        else:
                            # Track is excluded from statistics
                            log_debug(f"Skipped confidence checks for excluded track: {title} (baseline stars={stars})")
                        
                        # Ensure at least 1 star
                        stars = max(stars, 1)
                        
                        # Collect update for batch processing
                        updates.append((stars, track_id))
                        
                        star_distribution[stars] += 1
                        
                        log_debug(f"Final star rating for {title}: {stars} stars")
                    
                    # Batch update all tracks at once for better performance
                    cursor.executemany(
                        f"""UPDATE tracks SET stars = {placeholder} WHERE id = {placeholder}""",
                        updates
                    )
                    
                    # NEW: Tag 5-star songs that are detected as singles (medium+ confidence)
                    # This ensures that ANY 5-star track detected as a single is properly flagged
                    five_star_singles_to_tag = []
                    for stars, track_id in updates:
                        if stars == 5:  # Only for 5-star tracks
                            # Fetch the single_confidence for this track
                            cursor.execute(
                                f"SELECT single_confidence, is_single FROM tracks WHERE id = {placeholder}",
                                (track_id,)
                            )
                            single_row = cursor.fetchone()
                            if single_row:
                                single_confidence = single_row["single_confidence"] if single_row["single_confidence"] else "low"
                                is_single = single_row["is_single"] if single_row["is_single"] else 0
                                # Tag as single if medium+ confidence and not already tagged
                                if single_confidence in ["medium", "high"] and not is_single:
                                    five_star_singles_to_tag.append(track_id)
                    
                    # Tag all 5-star medium+ confidence singles
                    if five_star_singles_to_tag:
                        single_true_value = True if is_postgres_connection(conn) else 1
                        cursor.executemany(
                            f"""UPDATE tracks SET is_single = {placeholder} WHERE id = {placeholder}""",
                            ((single_true_value, track_id) for track_id in five_star_singles_to_tag)
                        )
                        log_info(f"Tagged {len(five_star_singles_to_tag)} 5-star track(s) as singles (medium+ confidence)")
                        log_debug(f"5-star singles tagged: {five_star_singles_to_tag}")
                    
                    # Upgrade is_single flag for medium confidence tracks with 2+ sources
                    if single_upgrades:
                        single_true_value = True if is_postgres_connection(conn) else 1
                        cursor.executemany(
                            f"""UPDATE tracks SET is_single = {placeholder} WHERE id = {placeholder}""",
                            ((single_true_value, track_id) for track_id in single_upgrades)
                        )
                        log_info(f"Upgraded {len(single_upgrades)} medium-confidence track(s) to single status (2+ sources) without overriding star rating")
                        log_debug(f"Upgraded tracks: {single_upgrades}")
                    
                    conn.commit()
                    log_debug(f"Batch committed {len(updates)} star ratings for album '{album}'")
                    
                    # Sync to Navidrome after batch update
                    for stars, track_id in updates:
                        if sync_track_rating_to_navidrome(track_id, stars):
                            log_debug(f"Synced track {track_id} to Navidrome with {stars} stars")
                        else:
                            log_debug(f"Skipped Navidrome sync for track {track_id}")
                    
                    # Log star distribution
                    dist_str = ", ".join([f"{stars}★: {count}" for stars, count in sorted(star_distribution.items(), reverse=True) if count > 0])
                    log_info(f'Star distribution for "{album}": {dist_str}')
                    log_debug(f'Star distribution details: {star_distribution}')
                    log_debug(f"About to call log_unified for star distribution: {dist_str}")
                    log_unified(f"Star Ratings - Album '{album}' by {artist}: {dist_str}")
                    log_debug(f"Successfully logged star distribution to unified log")
                    
                    # Generate unified log summary for singles and star ratings
                    # Re-fetch tracks with their final star ratings, single detection, and standout info
                    log_debug(f"Logging categorized tracks for album {album}: singles_count may be 0 if all tracks are non-singles")
                    try:
                        cursor.execute(
                            f"""SELECT id, title, artist, stars, is_single, single_confidence, single_sources, 
                                      is_standout_track, artist_z_score
                            FROM tracks 
                            WHERE COALESCE(NULLIF(album_artist, ''), artist) = {placeholder} AND album = {placeholder} 
                            ORDER BY stars DESC, popularity_score DESC""",
                            (artist, album)
                        )
                        final_tracks = cursor.fetchall()
                        
                        # Categorize tracks
                        detected_singles = []        # Detected as singles (has sources) with 5 stars
                        popular_songs = []           # Top cluster 5★ without single detection
                        rest_of_album = []           # All other tracks
                        
                        SOURCE_DISPLAY_NAMES = {
                            "musicbrainz": "MusicBrainz",
                            "discogs": "Discogs",
                            "discogs_video": "Discogs Video",
                            "spotify": "Spotify",
                            "lastfm": "Lastfm",
                            "iterative_zscore": "Popularity_artist_standout"
                        }
                        
                        for track_row in final_tracks:
                            track_id = track_row["id"]
                            track_title = track_row["title"]
                            track_artist = track_row["artist"] if track_row["artist"] else artist  # Fallback to album artist if track artist is None/empty
                            track_stars = track_row["stars"] if track_row["stars"] else 0
                            track_is_single = track_row["is_single"] if track_row["is_single"] else 0
                            track_is_standout = track_row["is_standout_track"] if track_row["is_standout_track"] else 0
                            track_single_confidence = track_row["single_confidence"] if track_row["single_confidence"] else ""
                            track_sources_json = track_row["single_sources"] if track_row["single_sources"] else "[]"
                            # Use the same album-level z-score computed in the star rating pass.
                            # Fallback to artist_z_score only if the map is missing an entry.
                            track_zscore = star_calc_zscores.get(
                                track_id,
                                track_row["artist_z_score"] if track_row["artist_z_score"] else 0
                            )
                            
                            # Parse single sources
                            try:
                                if track_sources_json and isinstance(track_sources_json, str):
                                    track_sources = json.loads(track_sources_json)
                                else:
                                    track_sources = []
                            except json.JSONDecodeError:
                                track_sources = []
                            
                            # Check if this track is in the top cluster
                            is_top_cluster = track_id in top_cluster_tracks
                            
                            # Format sources for display (metadata sources only)
                            formatted_sources = [SOURCE_DISPLAY_NAMES.get(s, s.capitalize()) for s in track_sources]
                            
                            # NOTE: Do NOT add popularity-based indicators to source display
                            # Z-scores and top cluster status are shown separately as reason/notes
                            
                            sources_str = "; ".join(formatted_sources) if formatted_sources else ""
                            
                            # Create star rating string (max 5 stars)
                            stars_str = "★" * min(track_stars, 5)
                            
                            # Build reason string (sources + z-score)
                            reason_parts = []
                            if sources_str:
                                reason_parts.append(sources_str)
                            if track_zscore:
                                reason_parts.append(f"album-z-score: {track_zscore:.2f}")
                            
                            reason_str = " (" + "; ".join(reason_parts) + ")" if reason_parts else ""
                            
                            # Categorize track for display:
                            # - Detected Singles: Has single detection sources (regardless of is_single flag)
                            # - Popular Songs: 5 stars but NO single detection sources (top cluster popular tracks)
                            # - Rest of Album: Everything else
                            
                            has_single_sources = len(track_sources) > 0 and any(s in ["musicbrainz", "discogs", "discogs_video", "spotify", "lastfm"] for s in track_sources)
                            
                            if has_single_sources and track_stars == 5:
                                # Detected single
                                detected_singles.append((track_artist, track_title, stars_str, reason_str))
                            elif track_stars == 5 and not has_single_sources and track_zscore > 2.0:
                                # Popular song (5★ with z-score > 2.0, no single detection)
                                popular_songs.append((track_artist, track_title, stars_str, reason_str))
                            else:
                                # Rest of album
                                rest_of_album.append((track_artist, track_title, stars_str, reason_str))
                        
                        # Log categorized results for this album (output immediately after album scan)
                        total_logged = len(detected_singles) + len(popular_songs) + len(rest_of_album)
                        log_debug(f"Track categorization for {album}: detected_singles={len(detected_singles)}, popular_songs={len(popular_songs)}, rest={len(rest_of_album)}, total={total_logged}")
                        
                        # Output album results immediately after scanning (not after all albums)
                        try:
                            if detected_singles:
                                log_unified(f"Single Detection Scan - ===== {album} - Detected Singles =====")
                                for track_artist, title, stars, reason in detected_singles:
                                    log_unified(f"Single Detection Scan - {stars} {track_artist} - {title}{reason}")
                            
                            if popular_songs:
                                log_unified(f"Single Detection Scan - ===== {album} - Popular Songs (Not Detected as Single) =====")
                                for track_artist, title, stars, reason in popular_songs:
                                    log_unified(f"Single Detection Scan - {stars} {track_artist} - {title}{reason}")
                            
                            if rest_of_album:
                                log_unified(f"Single Detection Scan - ===== {album} - Rest of Album =====")
                                for track_artist, title, stars, reason in rest_of_album:
                                    log_unified(f"Single Detection Scan - {stars} {track_artist} - {title}{reason}")
                            
                            # Track single detection progress (after logging results for this album)
                            single_detection_albums_processed += 1
                            
                            # Check milestones for single detection progress
                            if single_detection_albums_processed == single_detection_milestone_25 and 25 not in single_detection_milestones_logged:
                                log_unified(f"Single Detection Scan - 25% completed - {single_detection_albums_processed}/{total_albums} albums")
                                log_debug(f"Single detection progress milestone - 25% completed for artist '{artist}'")
                                single_detection_milestones_logged.add(25)
                            elif single_detection_albums_processed == single_detection_milestone_50 and 50 not in single_detection_milestones_logged:
                                log_unified(f"Single Detection Scan - 50% completed - {single_detection_albums_processed}/{total_albums} albums")
                                log_debug(f"Single detection progress milestone - 50% completed for artist '{artist}'")
                                single_detection_milestones_logged.add(50)
                            elif single_detection_albums_processed == single_detection_milestone_75 and 75 not in single_detection_milestones_logged:
                                log_unified(f"Single Detection Scan - 75% completed - {single_detection_albums_processed}/{total_albums} albums")
                                log_debug(f"Single detection progress milestone - 75% completed for artist '{artist}'")
                                single_detection_milestones_logged.add(75)
                        except Exception as e:
                            log_debug(f"Exception logging album results for {album}: {type(e).__name__}: {str(e)}")
                        
                    except Exception as e:
                        log_info(f"Error logging categorized tracks for album {album}: {e}")
                        log_debug(f"Exception in track categorization: {type(e).__name__}: {str(e)}")
                        import traceback
                        log_debug(f"Traceback: {traceback.format_exc()}")
                
                # Update last_scanned timestamp for all tracks in this album
                current_timestamp = datetime.now().isoformat()
                cursor.execute(
                    f"""UPDATE tracks 
                    SET last_scanned = {placeholder} 
                    WHERE COALESCE(NULLIF(album_artist, ''), artist) = {placeholder} AND album = {placeholder}""",
                    (current_timestamp, artist, album)
                )
                
                # Ensure changes are committed before logging to scan_history to avoid database lock conflicts
                conn.commit()
                log_debug(f"Committed all changes for album: {album}")
                
                # Log album scan with scan type that matches the active mode.
                scan_history_type = 'singles' if singles_only else 'popularity'
                log_album_scan(artist, album, scan_history_type, album_scanned, 'completed')
                log_debug(
                    f"Logged album scan to scan_history - album: {album}, "
                    f"scan_type: {scan_history_type}, tracks_scanned: {album_scanned}"
                )

            # After all albums processed for this artist, show artist scan completion summary
            # (Individual album details were logged immediately after each album scan)
            try:
                log_unified(f"✅ Scan complete for artist '{artist}'")
                log_debug(f"Artist '{artist}' scan completed. All album details logged above during individual album scans.")
                
            except Exception as e:
                log_info(f"Error completing artist scan summary for {artist}: {e}")
                log_debug(f"Exception in artist scan summary: {type(e).__name__}: {str(e)}")

            # After artist scans, evaluate essential playlist for artist (Case A: 10+ five-star OR Case B: 100+ tracks)
            # Get ALL tracks for this artist (not just 5-star) to properly apply Case A/B logic
            cursor.execute(
                f"""SELECT id, artist, album, title, stars
                FROM tracks 
                WHERE COALESCE(NULLIF(album_artist, ''), artist) = {placeholder}
                ORDER BY stars DESC, popularity_score DESC""",
                (artist,)
            )
            all_artist_tracks = cursor.fetchall()
            log_debug(f"Retrieved {len(all_artist_tracks)} tracks for playlist evaluation for artist: {artist}")
            
            if all_artist_tracks:
                # Convert to list of dicts for create_or_update_playlist_for_artist
                tracks_list = [
                    {
                        "id": t["id"],
                        "artist": t["artist"],
                        "album": t["album"],
                        "title": t["title"],
                        "stars": int(t["stars"]) if t["stars"] else 0
                    }
                    for t in all_artist_tracks
                ]
                
                # Call the actual playlist creation function (applies Case A/B logic)
                # Logging happens inside the function based on whether playlist was actually created
                log_debug(f"Calling playlist creation for artist: {artist} with {len(tracks_list)} tracks")
                create_or_update_playlist_for_artist(artist, tracks_list)

            # After playlist creation, detect cover songs for this artist
            # This batch process runs once per artist after all their tracks/albums are scanned
            # ensuring we have all composer data available before comparing
            log_debug(f"Starting cover detection for artist: {artist}")
            covers_found = detect_covers_for_artist(artist, conn)
            if covers_found > 0:
                conn.commit()  # Commit cover detection results
                log_debug(f"Cover detection results committed - {covers_found} covers detected for {artist}")

            # Update artist progress tracking after completing all albums for this artist
            # Note: Progress is saved once per artist (not per track) to balance granularity
            # with I/O efficiency. Original code saved after every track which could result
            # in thousands of writes for large libraries. Per-artist updates provide adequate
            # progress visibility while reducing file I/O by orders of magnitude.
            # If scan is interrupted, it can resume from the last completed artist.
            processed_artists += 1
            save_popularity_progress(processed_artists, total_artists, current_artist=artist)
            log_debug(f"Progress saved - {processed_artists}/{total_artists} artists processed (current: {artist})")

        log_debug("Committing final changes to database")
        conn.commit()

        log_unified(f"Popularity Scan - Complete: {scanned_count} tracks updated, {skipped_count} albums skipped")
        log_info(f"Popularity scan completed: {scanned_count} tracks updated, {skipped_count} albums skipped (already scanned)")
        log_debug(f"Scan statistics - scanned: {scanned_count}, skipped: {skipped_count}, total_artists: {total_artists}")
        
        # Write final progress state (marks scan as completed)
        try:
            progress_data = {
                "is_running": False,
                "scan_type": "popularity_scan",
                "processed_artists": total_artists,
                "total_artists": total_artists,
                "percent_complete": 100,
                "current_artist": None  # Clear current artist when scan completes
            }
            with open(POPULARITY_PROGRESS_FILE, 'w') as f:
                json.dump(progress_data, f)
            log_debug(f"Final progress state written to {POPULARITY_PROGRESS_FILE}")
        except Exception as e:
            log_info(f"Error writing final progress state: {e}")
            log_debug(f"Progress file error details: {type(e).__name__}: {str(e)}")
            
    except Exception as e:
        log_unified(f"Popularity Scan - Error: {str(e)}")
        log_info(f"Popularity scan failed with error: {str(e)}")
        import traceback
        log_debug(f"Exception traceback: {traceback.format_exc()}")
        raise
    finally:
        if conn:
            conn.close()
            log_debug("Database connection closed")
        log_info("=" * 60)
        log_info(f"Popularity scan session ended at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        log_info("=" * 60)

def _sanitize_playlist_name(name: str) -> str:
    """Sanitize playlist name for filesystem use."""
    return "".join(c for c in name if c.isalnum() or c in ('-', '_', ' ')).strip()


def _delete_nsp_file(playlist_name: str) -> None:
    """Delete an NSP playlist file if it exists."""
    try:
        music_folder = os.environ.get("MUSIC_FOLDER", "/music")
        playlists_dir = os.path.join(music_folder, "Playlists")
        safe_name = _sanitize_playlist_name(playlist_name)
        file_path = os.path.join(playlists_dir, f"{safe_name}.nsp")
        if os.path.exists(file_path):
            os.remove(file_path)
            log_basic(f"ðŸ—‘ï¸ Deleted playlist: {playlist_name}")
    except Exception as e:
        log_basic(f"Failed to delete playlist '{playlist_name}': {e}")


def _create_nsp_file(playlist_name: str, playlist_data: dict) -> bool:
    """Create an NSP playlist file. Returns True on success."""
    try:
        music_folder = os.environ.get("MUSIC_FOLDER", "/music")
        playlists_dir = os.path.join(music_folder, "Playlists")
        os.makedirs(playlists_dir, exist_ok=True)
        
        safe_name = _sanitize_playlist_name(playlist_name)
        file_path = os.path.join(playlists_dir, f"{safe_name}.nsp")
        
        # Overwrite if exists (allow updates)
        with open(file_path, 'w', encoding='utf-8') as f:
            json.dump(playlist_data, f, indent=2, ensure_ascii=False)
        
        log_basic(f"ðŸ“ NSP created/updated: {file_path}")
        return True
    except Exception as e:
        log_basic(f"Failed to create playlist '{playlist_name}': {e}")
        return False


def detect_covers_for_artist(artist_name: str, conn: sqlite3.Connection) -> int:
    """
    Detect cover songs for an artist by comparing composers.
    
    Algorithm:
    1. Get all tracks for the artist with their composers
    2. Find the most common composer (artist's "typical" composer)
    3. For each track with a DIFFERENT composer:
       - Search for other artists with same song title AND same composer
       - If found, mark as cover
    4. Update is_cover and is_cover_reason fields in database
    
    Args:
        artist_name: Artist name to check
        conn: Database connection
        
    Returns:
        Number of covers detected
    """
    try:
        cursor = conn.cursor()
        
        # 1. Get all tracks for this artist with composers
        cursor.execute("""
            SELECT id, title, composer, artist
            FROM tracks
            WHERE artist = %s AND composer IS NOT NULL AND composer != ''
            ORDER BY composer
        """, (artist_name,))
        
        artist_tracks = cursor.fetchall()
        if not artist_tracks:
            return 0  # No tracks with composers
        
        # 2. Find the most common composer (artist's typical composer)
        composer_counts = {}
        for track in artist_tracks:
            composer = track[2]  # composer field
            composer_counts[composer] = composer_counts.get(composer, 0) + 1
        
        if not composer_counts:
            return 0
        
        typical_composer = max(composer_counts.items(), key=lambda x: x[1])[0]
        log_debug(f"Artist '{artist_name}' typical composer: '{typical_composer}' (appears {composer_counts[typical_composer]} times)")
        
        covers_detected = 0
        
        # 3. Check each track with a DIFFERENT composer
        for track in artist_tracks:
            track_id, title, composer, artist = track
            
            # Skip if composer matches typical composer
            if composer == typical_composer:
                continue
            
            # Search for other artists with same title AND same composer
            cursor.execute("""
                SELECT artist FROM tracks
                WHERE title = %s AND composer = %s AND artist != %s AND composer IS NOT NULL
                LIMIT 1
            """, (title, composer, artist_name))
            
            other_artist = cursor.fetchone()
            
            if other_artist:
                # Found another artist with this title + composer combo!
                # Mark as cover
                other_artist_name = other_artist[0]
                reason = f"Cover detected: Original by '{other_artist_name}' (composer: '{composer}')"
                
                cursor.execute(f"""
                    UPDATE tracks
                    SET is_cover = 1, is_cover_reason = {placeholder}
                    WHERE id = {placeholder}
                """, (reason, track_id))
                
                log_debug(f"Cover detected: '{title}' by '{artist_name}' is a cover of original by '{other_artist_name}'")
                covers_detected += 1
            else:
                # No other artist found - clear cover flag if it was previously set
                cursor.execute(f"""
                    UPDATE tracks
                    SET is_cover = 0, is_cover_reason = NULL
                    WHERE id = {placeholder}
                """, (track_id,))
        
        if covers_detected > 0:
            log_info(f"Cover Detection - Detected {covers_detected} covers for '{artist_name}'")
        
        return covers_detected
        
    except Exception as e:
        log_info(f"Error detecting covers for artist '{artist_name}': {e}")
        log_debug(f"Cover detection error details: {type(e).__name__}: {str(e)}")
        return 0


def create_or_update_playlist_for_artist(artist_name: str, tracks: list):
    """
    Create/refresh 'Essential {artist}' smart playlist using Navidrome's 0â€“5 rating scale.

    Logic:
      - Case A: if artist has >= 10 five-star tracks, build a pure 5★ essentials playlist.
      - Case B: if total tracks >= 100, build top 10% essentials sorted by rating.
    
    Args:
        artist_name: Name of the artist
        tracks: List of track dictionaries with id, artist, album, title, stars
    """
    total_tracks = len(tracks)
    five_star_tracks = [t for t in tracks if (t["stars"] or 0) == 5]
    playlist_name = f"Essential {artist_name}"

    # CASE A – 10+ five-star tracks → purely 5★ essentials
    if len(five_star_tracks) >= 10:
        _delete_nsp_file(playlist_name)
        playlist_data = {
            "name": playlist_name,
            "comment": "Auto-generated by SPTNR",
            "all": [{"is": {"artist": artist_name, "rating": 5}}],
            "sort": "random"
        }
        _create_nsp_file(playlist_name, playlist_data)
        log_basic(f"Essential playlist created for '{artist_name}' (5★ essentials)")
        return

    # CASE B – 100+ total tracks → top 10% by rating
    if total_tracks >= 100:
        _delete_nsp_file(playlist_name)
        limit = max(1, math.ceil(total_tracks * 0.10))
        playlist_data = {
            "name": playlist_name,
            "comment": "Auto-generated by SPTNR",
            "all": [{"is": {"artist": artist_name}}],
            "sort": "-rating,random",
            "limit": limit
        }
        _create_nsp_file(playlist_name, playlist_data)
        log_basic(f"Essential playlist created for '{artist_name}' (top 10% by rating)")
        return

    # If artist no longer meets requirements, delete existing playlist if it exists
    log_basic(
        f"No Essential playlist created for '{artist_name}' "
        f"(total={total_tracks}, five★={len(five_star_tracks)})"
    )
    # Clean up old playlist if it exists but requirements are no longer met
    _delete_nsp_file(playlist_name)

def refresh_all_playlists_from_db():
    """
    Refresh all smart playlists for all artists from DB cache (no track rescans).
    This function pulls distinct artists that have cached tracks and updates their playlists.
    """
    log_basic("ðŸ”„ Refreshing smart playlists for all artists from DB cache (no track rescans)...")
    
    # Pull distinct artists that have cached tracks
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT COALESCE(NULLIF(album_artist, ''), artist) AS artist_name FROM tracks")
        artists = [row[0] for row in cursor.fetchall()]
        
        if not artists:
            log_basic("âš ï¸ No cached tracks in DB. Skipping playlist refresh.")
            return
        
        for name in artists:
            cursor.execute(
                f"""SELECT id, artist, album, title, stars
                   FROM tracks
                   WHERE COALESCE(NULLIF(album_artist, ''), artist) = {placeholder}""",
                (name,)
            )
            rows = cursor.fetchall()
            
            if not rows:
                log_basic(f"âš ï¸ No cached tracks found for '{name}', skipping.")
                continue
            
            tracks = [
                {
                    "id": r[0],
                    "artist": r[1],
                    "album": r[2],
                    "title": r[3],
                    "stars": int(r[4]) if r[4] else 0
                }
                for r in rows
            ]
            create_or_update_playlist_for_artist(name, tracks)
            log_basic(f"âœ… Playlist refreshed for '{name}' ({len(tracks)} tracks)")
    except Exception as e:
        log_basic(f"âŒ Error refreshing playlists: {e}")
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Run popularity scan.")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose output")
    parser.add_argument("--force", action="store_true", help="Force re-scan of all albums")
    args = parser.parse_args()
    popularity_scan(verbose=args.verbose, force=args.force)