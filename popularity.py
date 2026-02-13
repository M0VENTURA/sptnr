#!/usr/bin/env python3
"""
Popularity Scanner - Detects track popularity from external sources (Spotify, Last.fm, ListenBrainz).
Calculates popularity scores and updates database.
Note: Singles detection is handled separately by sptnr.py rate_artist() function.
"""

import os
import sqlite3
import logging
import json
import math
import yaml
import atexit
import time
import heapq
import re
import difflib
from contextlib import contextmanager
from datetime import datetime, timedelta
from statistics import median, mean, stdev
from concurrent.futures import ThreadPoolExecutor
import concurrent.futures
from api_clients import session, timeout_safe_session
from helpers import find_matching_spotify_single
from matching_utils import normalize_album

# Import centralized logging
from logging_config import setup_logging, log_unified, log_info, log_debug

# Set up logging for popularity service
setup_logging("popularity")

# Import API clients for single detection at module level
try:
    from api_clients.musicbrainz import MusicBrainzClient, get_artist_country
    HAVE_MUSICBRAINZ = True
except ImportError as e:
    log_debug(f"MusicBrainz client unavailable: {e}")
    HAVE_MUSICBRAINZ = False
    
try:
    from api_clients.discogs import DiscogsClient
    HAVE_DISCOGS = True
    HAVE_DISCOGS_VIDEO = True
except ImportError as e:
    log_debug(f"Discogs client unavailable: {e}")
    HAVE_DISCOGS = False
    HAVE_DISCOGS_VIDEO = False

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
    global _timeout_safe_discogs_clients
    if not HAVE_DISCOGS:
        return None
    if token not in _timeout_safe_discogs_clients:
        _timeout_safe_discogs_clients[token] = DiscogsClient(token, http_session=timeout_safe_session, enabled=True)
    return _timeout_safe_discogs_clients.get(token)

# Module-level logger
logger = logging.getLogger(__name__)

# Keyword filter for non-singles (defined at module level for performance)
# Filters out alternate versions: live, acoustic, orchestral, remixes, demos, etc.
# Note: List is used (not set) since we perform substring matching with 'any(k in title...)'
IGNORE_SINGLE_KEYWORDS = [
    "intro", "outro", "jam",  # intros/outros/jams
    "live", "unplugged",  # live performances
    "remix", "edit", "mix",  # remixes and edits
    "acoustic", "orchestral",  # alternate arrangements
    "demo", "instrumental", "karaoke",  # alternate versions
    "remaster", "remastered"  # remasters
]

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

# Popularity-based confidence system constants
DEFAULT_POPULARITY_MEAN = 10  # Default mean when no valid scores
DEFAULT_HIGH_CONF_OFFSET = 6  # Offset above mean for high confidence (popularity >= mean + 6)
DEFAULT_MEDIUM_CONF_THRESHOLD = -0.3  # Threshold below top 50% mean for medium confidence

# Artist-level popularity comparison constants
UNDERPERFORMING_THRESHOLD = 0.6  # Album median must be >= 60% of artist median to not be underperforming
MIN_TRACKS_FOR_ARTIST_COMPARISON = 10  # Minimum tracks needed for reliable artist-level comparison

# Metadata source display constant
POPULARITY_METADATA_SOURCE_NAME = "Spotify/Last.fm popularity"  # Display name for tracks with popularity data but no single sources


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


def should_exclude_track_from_stats(title: str, album: str = "") -> bool:
    """
    Determine if a track should be excluded from album/artist statistics calculations.
    
    Excludes tracks that are:
    - Live versions
    - Remixes
    - Acoustic/orchestral versions
    - Demos
    - Instrumentals
    - Remasters
    - Other alternate versions
    
    This ensures that album median, mean, stddev calculations reflect the core album tracks
    and are not skewed by bonus/alternate versions.
    
    Args:
        title: Track title to check
        album: Album name to check (optional, for live album detection)
        
    Returns:
        True if track should be excluded from statistics, False otherwise
    """
    # Check title and album name for keywords
    combined_text = f"{title} {album}".lower()
    return any(keyword in combined_text for keyword in IGNORE_SINGLE_KEYWORDS)


def is_live_or_alternate_album(album: str) -> bool:
    """
    Determine if an album is a live, unplugged, or acoustic album.
    
    This helps identify albums where the recorded versions differ from studio versions,
    such as "Alice in Chains - Unplugged in New York" where tracks should not be matched
    with their studio counterparts.
    
    Args:
        album: Album name to check
        
    Returns:
        True if this is a live/unplugged/acoustic album, False otherwise
    """
    if not album:
        return False
    
    album_lower = album.lower()
    
    # Live album indicators
    # Note: 'unplugged' covers 'mtv unplugged', so no need for separate entry
    live_keywords = [
        'live',
        'unplugged',
        'acoustic',
        'live at',
        'live in',
        'concert',
        'live from',
        'in concert',
        'on stage',
        'live tour'  # More specific than just 'tour' to avoid false positives
    ]
    
    return any(keyword in album_lower for keyword in live_keywords)


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
        cursor = conn.cursor()
        cursor.execute("""
            SELECT last_spotify_lookup, popularity_score 
            FROM tracks 
            WHERE id = ?
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
    
    This helps identify underperforming albums/singles within an artist's catalog.
    
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
    """
    try:
        cursor = conn.cursor()
        
        # Try to get album column if it exists, otherwise use empty string
        # This ensures backward compatibility with databases that don't have album column
        try:
            cursor.execute("""
                SELECT popularity_score, title, album
                FROM tracks 
                WHERE artist = ? AND popularity_score > 0
            """, (artist_name,))
            rows = cursor.fetchall()
            has_album_column = True
        except sqlite3.OperationalError as e:
            # Fallback: album column doesn't exist (OperationalError: no such column: album)
            # Only handle the specific "no such column" error
            if "no such column" in str(e).lower():
                cursor.execute("""
                    SELECT popularity_score, title
                    FROM tracks 
                    WHERE artist = ? AND popularity_score > 0
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
                'track_count': 0
            }
        
        return {
            'avg_popularity': mean(scores),
            'median_popularity': median(scores),
            'stddev_popularity': stdev(scores) if len(scores) > 1 else 0,
            'track_count': len(scores)
        }
    except Exception as e:
        log_verbose(f"   Error calculating artist stats: {e}")
        return {
            'avg_popularity': 0,
            'median_popularity': 0,
            'stddev_popularity': 0,
            'track_count': 0
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
    AGE_WEIGHT,
)
from api_rate_limiter import get_rate_limiter

# Import scan history tracker
try:
    from scan_history import log_album_scan, was_album_scanned
except ImportError:
    def log_album_scan(*args, **kwargs):
        pass  # Fallback if scan_history not available
    def was_album_scanned(*args, **kwargs):
        return False  # Fallback if scan_history not available

# --- DEBUG: Test log_unified and print log path ---
if __name__ == "__main__":
    try:
        print("UNIFIED_LOG_PATH:", UNIFIED_LOG_PATH)
        log_unified("TEST ENTRY: log_unified() at script start")
    except Exception as e:
        print("log_unified() test failed:", e)

def get_db_connection():
    """Get database connection with WAL mode and extended timeout for concurrent access"""
    conn = sqlite3.connect(DB_PATH, timeout=120.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout = 120000")  # 120 seconds
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

def save_popularity_progress(processed_artists: int, total_artists: int):
    """Save popularity scan progress to file"""
    try:
        progress_data = {
            "is_running": True,
            "scan_type": "popularity_scan",
            "processed_artists": processed_artists,
            "total_artists": total_artists,
            "percent_complete": int((processed_artists / total_artists * 100)) if total_artists > 0 else 0
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
        
        # Try to get MBID from database
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT beets_album_mbid FROM tracks 
            WHERE artist = ? AND album = ? AND beets_album_mbid IS NOT NULL
            LIMIT 1
        """, (artist, album))
        result = cursor.fetchone()
        conn.close()
        
        album_mbid = result['beets_album_mbid'] if result else None
        
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
    zscore_threshold: float = 0.20,
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
        zscore_threshold: Z-score threshold for singles (default 0.20)
        
    Returns:
        Dict with keys:
            - sources: List of sources that confirmed single (e.g. ['spotify', 'musicbrainz'])
            - confidence: 'high', 'medium', or 'low'
            - is_single: True if confidence is 'high', False otherwise
            - global_popularity: Global popularity across versions (if advanced)
            - zscore: Z-score within album (if advanced)
            - metadata_single: Metadata single status (if advanced)
            - is_compilation: Compilation status (if advanced)
    """
    # Use enhanced detection algorithm per problem statement if enabled
    # This implements the exact 8-stage algorithm with pre-filter, Discogs primary, etc.
    if use_advanced_detection and track_id and album:
        conn = None
        try:
            from single_detection_enhanced import detect_single_enhanced, store_single_detection_result
            # get_db_connection is already available in this module
            conn = get_db_connection()
            
            # Get Spotify results if cached
            spotify_search_results = None
            if spotify_results_cache is not None:
                spotify_search_results = spotify_results_cache.get(title)
            
            # Get API clients
            discogs_client = None
            if discogs_token and HAVE_DISCOGS:
                discogs_client = _get_timeout_safe_discogs_client(discogs_token)
            
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
            
            # Store result in database
            store_single_detection_result(conn, track_id, result)
            
            # Return in expected format
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
    
    # Ignore obvious non-singles by keywords
    if any(k in title.lower() for k in IGNORE_SINGLE_KEYWORDS):
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
            cursor = conn.cursor()
            
            # Calculate album mean popularity
            cursor.execute("""
                SELECT popularity_score 
                FROM tracks 
                WHERE artist = ? AND album = ? AND popularity_score > 0
            """, (artist, album))
            album_popularities = [row[0] for row in cursor.fetchall()]
            
            if album_popularities:
                from statistics import mean as stat_mean
                album_mean = stat_mean(album_popularities)
                
                if popularity < album_mean:
                    if verbose:
                        log_verbose(f"   âŠ— Skipping single: {title} (popularity {popularity:.1f} < album mean {album_mean:.1f})")
                    conn.close()
                    return {
                        "sources": [],
                        "confidence": "low",
                        "is_single": False
                    }
            
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
        except TimeoutError as e:
            log_info(f"   â± MusicBrainz single check timed out for {title}: {e}")
        except Exception as e:
            log_info(f"   âš  MusicBrainz single check failed for {title}: {e}")
    else:
        log_info(f"   â“˜ MusicBrainz client not available")
    
    # Third check: Discogs single detection
    if HAVE_DISCOGS and discogs_token:
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
        if not HAVE_DISCOGS:
            log_info(f"   â“˜ Discogs client not available")
            log_debug(f"   Discogs: Client not available (module import failed)")
        elif not discogs_token:
            log_info(f"   â“˜ Discogs token not configured")
            log_debug(f"   Discogs: Token not configured in config.yaml")
    
    # Fourth check: Discogs video detection
    if HAVE_DISCOGS_VIDEO and discogs_token:
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
        if not HAVE_DISCOGS_VIDEO:
            log_info(f"   â“˜ Discogs video client not available")
            log_debug(f"   Discogs: Video client not available")
        elif not discogs_token:
            log_info(f"   â“˜ Discogs token not configured for video detection")
            log_debug(f"   Discogs: Token not configured for video detection")
    
    # Calculate confidence based on sources per problem statement
    # High confidence: Discogs single format OR (discogs_video + any other source)
    # Medium confidence: Spotify, MusicBrainz, Last.fm single, or discogs_video alone
    # Low confidence: No sources
    has_discogs_single = "discogs" in single_sources
    has_discogs_video = "discogs_video" in single_sources
    has_other_sources = any(s in single_sources for s in ["spotify", "musicbrainz", "lastfm"])
    
    # Video detection requires a second method to approve it (Spotify, Discogs single, or MusicBrainz)
    if has_discogs_single or (has_discogs_video and has_other_sources):
        single_confidence = "high"
    elif has_other_sources or has_discogs_video:
        single_confidence = "medium"
    else:
        single_confidence = "low"
    
    # Album context rule: downgrade medium â†’ low if album has >3 tracks
    if single_confidence == "medium" and album_track_count > 3:
        single_confidence = "low"
        if verbose:
            log_verbose(f"   â“˜ Downgraded {title} confidence to low (album has {album_track_count} tracks)")
    
    # is_single = True only for high confidence singles (5* singles)
    is_single = single_confidence == "high"
    
    return {
        "sources": single_sources,
        "confidence": single_confidence,
        "is_single": is_single
    }


def get_artist_lastfm_context(artist_name: str, conn: sqlite3.Connection) -> dict:
    """
    Pre-fetch Last.fm listener data for all tracks by an artist to enable dynamic weight adjustment.
    
    Uses artist-level statistics to identify tracks that are outliers in the artist's catalogue,
    allowing intelligent weight redistribution during popularity scoring.
    
    Falls back to Last.fm's top tracks API if database has limited catalogue coverage.
    Also fetches artist info to determine top 10% threshold.
    
    Example:
        - Colossus by Borknagar: 43,991 Last.fm listeners
        - Borknagar has ~150 total tracks → Top 10% = top 15 tracks
        - Colossus is in top 15 globally AND outlier on album → 5 stars
    
    Args:
        artist_name: Name of the artist
        conn: Database connection
        
    Returns:
        Dict with keys:
        - mean: Average Last.fm listeners across artist's tracks
        - stdev: Standard deviation of Last.fm listeners
        - min: Minimum listener count
        - max: Maximum listener count
        - track_count: Number of tracks analyzed (for stats)
        - total_tracks: Total artist tracks on Last.fm (from API)
        - top_10_percentile_threshold: Listener count for top 10% of artist tracks
        - track_zscores: Dict mapping track_id → z-score for database tracks
        - source: 'database' or 'lastfm_api' indicating stats data source
    """
    try:
        cursor = conn.cursor()
        
        # Get all tracks by artist with Last.fm listener data
        # Exclude live/remix/alternate versions to avoid skewing stats
        cursor.execute("""
            SELECT id, title, album, lastfm_track_playcount
            FROM tracks
            WHERE artist = ? AND lastfm_track_playcount > 0 AND is_single = 0 
                AND album NOT IN (
                    SELECT DISTINCT album FROM tracks WHERE artist = ? AND album_context_live = 1
                )
                AND album NOT IN (
                    SELECT DISTINCT album FROM tracks WHERE artist = ? AND discogs_format_descriptions LIKE '%live%'
                )
        """, (artist_name, artist_name, artist_name))
        
        tracks = cursor.fetchall()
        listeners_list = [row[3] for row in tracks if row[3] > 0]
        
        # Fetch artist info to get total track count and top 10% threshold
        total_tracks = 0
        top_10_percentile_threshold = 0
        try:
            import requests
            from config_loader import load_config
            
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
                    
                    if listeners_list:
                        log_debug(f"Added {len(api_listeners_list)} top tracks from Last.fm API for {artist_name}")
        except Exception as e:
            log_debug(f"Failed to fetch Last.fm artist info for {artist_name}: {e}")
        
        if not listeners_list or len(listeners_list) < 2:
            return {
                'mean': 0,
                'stdev': 0,
                'min': 0,
                'max': 0,
                'track_count': len(listeners_list) if listeners_list else 0,
                'total_tracks': total_tracks,
                'top_10_percentile_threshold': top_10_percentile_threshold,
                'track_zscores': {},
                'source': 'error'
            }
        
        # Calculate artist-level statistics
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
            'source': 'database_plus_api' if listeners_list else 'error'
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
            'source': 'error'
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
    singles_only: bool = False
):
    """
    Detect track popularity from external sources.
    
    Args:
        verbose: Enable verbose logging
        resume_from: Artist name to resume from (for interrupted scans)
        artist_filter: Only scan tracks for this specific artist
        album_filter: Only scan tracks for this specific album (requires artist_filter)
        skip_header: Skip logging the header (useful when called from unified_scan)
        force: Force re-scan of albums even if they were already scanned
        filter_missing: Only scan artists/albums with missing popularity data
        singles_only: Only rescan singles detection, skip popularity scoring
    """
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
            # Check both artist and album_artist fields to handle compilation albums
            # (e.g., Various Artists albums where album_artist = "Various Artists" but artist = track artist)
            sql_conditions.append("(artist = ? OR album_artist = ?)")
            sql_params.extend([artist_filter, artist_filter])
        
        if album_filter and artist_filter:
            sql_conditions.append("album = ?")
            sql_params.append(album_filter)
        
        sql = f"""
            SELECT id, artist, title, album, isrc, duration, spotify_album_type, track_number, mbid, year,
                   spotify_popularity, lastfm_track_playcount, last_spotify_lookup, popularity_score, album_artist
            FROM tracks
            {('WHERE ' + ' AND '.join(sql_conditions)) if sql_conditions else ''}
            ORDER BY artist, album, title
        """
        
        log_debug(f"Executing SQL: {sql.strip()} with params: {sql_params}")
        cursor.execute(sql, sql_params)

        tracks = cursor.fetchall()
        log_info(f"Found {len(tracks)} tracks to scan for popularity")
        log_debug(f"Fetched {len(tracks)} tracks from database")

        if not tracks:
            log_info("No tracks found for popularity scan. Exiting.")
            return

        # Group tracks by album_artist and album
        # For compilation albums, album_artist is "Various Artists", so all tracks group together
        # Each track still uses its individual track["artist"] field for API lookups
        from collections import defaultdict
        artist_album_tracks = defaultdict(lambda: defaultdict(list))
        
        for track in tracks:
            # Get album_artist, fall back to artist if not set
            album_artist = track.get('album_artist', '') if isinstance(track, dict) else (
                track['album_artist'] if hasattr(track, '__getitem__') and 'album_artist' in track.keys() else ''
            )
            
            # Use album_artist for grouping (or fall back to artist)
            grouping_artist = album_artist if album_artist else track["artist"]
            album_name = track["album"]
            
            artist_album_tracks[grouping_artist][album_name].append(track)
            
            log_debug(f"Track grouping: grouping_artist={grouping_artist}, album_artist={album_artist}, track_artist={track['artist']}, title={track['title']}")

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
        # Check if Spotify is available (we always try to configure it)
        enabled_apis.append("Spotify")
        # Check if Last.fm and ListenBrainz are configured
        try:
            config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f)
            if config.get("api_integrations", {}).get("lastfm", {}).get("api_key"):
                enabled_apis.append("Last.FM")
            if config.get("api_integrations", {}).get("listenbrainz", {}).get("token"):
                enabled_apis.append("ListenBrainz")
        except (FileNotFoundError, yaml.YAMLError, KeyError, AttributeError) as e:
            log_debug(f"Could not load API configuration: {e}")
        
        if enabled_apis:
            log_unified(f"Popularity Scan - Scanning {', '.join(enabled_apis)} for Metadata")
            log_debug(f"Enabled APIs: {enabled_apis}")
        
        for artist, albums in artist_album_tracks.items():
            # Skip until resume match
            if not resume_hit:
                if artist.lower() == resume_from.lower():
                    resume_hit = True
                    log_info(f"Resuming from: {artist}")
                elif resume_from.lower() in artist.lower():
                    resume_hit = True
                    log_info(f"Fuzzy resume match: {resume_from} â†’ {artist}")
                else:
                    log_debug(f"Skipping {artist} (before resume point)")
                    continue
            
            log_unified(f"Popularity Scan - Scanning Artist {artist} ({len(albums)} album(s))")
            log_debug(f"Processing artist/album group: {artist} with {len(albums)} albums")
            
            # Pre-fetch artist's Last.fm context for dynamic weight adjustment
            # This allows us to boost Last.fm weight for tracks that are outliers in the artist's catalogue
            artist_lastfm_context = get_artist_lastfm_context(artist, conn)
            if artist_lastfm_context['track_count'] > 0:
                log_info(f"Artist Last.fm context: {artist_lastfm_context['track_count']} tracks, mean={artist_lastfm_context['mean']:.0f} listeners, stdev={artist_lastfm_context['stdev']:.0f}")
                log_debug(f"Artist catalogue range: {artist_lastfm_context['min']:.0f} - {artist_lastfm_context['max']:.0f} listeners")
            else:
                log_debug(f"No Last.fm listener data available for artist {artist} - will use base weights")
            
            # Get Spotify artist ID once per artist (before album loop)
            # Skip for compilation albums (Various Artists, Compilation, Soundtrack)
            spotify_artist_id = None
            is_compilation_group = artist.lower() in ('various artists', 'various', 'compilation', 'soundtrack')
            
            if not is_compilation_group:
                # Lookup Spotify artist ID for non-compilation artists
                try:
                    # First, try to get cached artist ID from database
                    cursor.execute("""
                        SELECT spotify_artist_id 
                        FROM tracks 
                        WHERE artist = ? AND spotify_artist_id IS NOT NULL 
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
                        
                        # Fetch and update artist country from MusicBrainz during popularity scan
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
                                    cursor.execute("""
                                        INSERT INTO artists (id, name, country) 
                                        VALUES (?, ?, ?)
                                        ON CONFLICT(id) DO UPDATE SET country = excluded.country
                                    """, (artist, artist, artist_country))
                                    
                                    # Update tracks table with artist country
                                    cursor.execute("UPDATE tracks SET artist_country = ? WHERE COALESCE(album_artist, artist) = ?", 
                                                 (artist_country, artist))
                                    conn.commit()
                                    log_debug(f'Updated artist country in database: {artist} -> {artist_country}')
                                else:
                                    log_debug(f'No country information found for artist: {artist}')
                        except TimeoutError as e:
                            log_debug(f"Artist country lookup timed out for {artist}: {e}")
                        except Exception as e:
                            log_debug(f"Artist country lookup failed for {artist}: {e}")
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
            
            album_num = 0
            for album, album_tracks in albums.items():
                album_num += 1
                album_scanned = 0  # Initialize before popularity section (may be skipped in singles_only)
                
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
                        continue
                    
                    log_unified(f'Popularity Scan - Scanning Album {album} ({album_num}/{len(albums)})')
                    log_info(f'Starting popularity scan for album: "{artist} - {album}"')
                
                # Detect if this is a live/unplugged album
                is_live_album = is_live_or_alternate_album(album)
                if is_live_album:
                    log_info(f'Detected live/unplugged album: "{album}"')
                    log_info(f'Track lookups will include album context to avoid matching studio versions')
                    log_debug(f'Live album detection: album="{album}"')
                
                # Detect alternate takes for this album (tracks with parentheses matching base tracks)
                album_tracks_list = list(album_tracks)
                alternate_takes_map = detect_alternate_takes(album_tracks_list)
                
                # Save alternate take mappings to database
                if alternate_takes_map:
                    for alt_track_id, base_track_id in alternate_takes_map.items():
                        cursor.execute("""
                            UPDATE tracks 
                            SET alternate_take = 1, base_track_id = ?
                            WHERE id = ?
                        """, (base_track_id, alt_track_id))
                    conn.commit()
                    log_info(f'Detected {len(alternate_takes_map)} alternate take(s) in album')
                    log_debug(f'Alternate takes map: {alternate_takes_map}')
                
                # Cache Spotify search results for singles detection reuse
                # Initialize unconditionally for both singles_only and normal mode
                spotify_results_cache = {}
                
                # Fetch and cache album art URL from MusicBrainz for this album
                album_art_url = None
                if not singles_only:
                    album_art_url = fetch_album_art_url_from_musicbrainz(artist, album)
                    if album_art_url:
                        log_info(f'Fetched album art URL for {artist} - {album}: {album_art_url}')
                    else:
                        log_debug(f'No album art URL found for {artist} - {album}')
                
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
                                    track_artist, title
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
                
                # In singles_only mode, skip all popularity scoring
                if not singles_only:
                    # Batch updates for this album (commit once at end instead of per-track)
                    track_updates = []
                    
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
                    log_info(f"Singles-only mode for album '{album}': all popularity processing skipped")
                    track_updates = []
                
                if not singles_only:
                    for track in album_tracks:
                        track_id = track["id"]
                        title = track["title"]
                        track_artist = track["artist"]

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
                        skip_spotify_lookup = any(k in title.lower() for k in IGNORE_SINGLE_KEYWORDS)
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
                            if spotify_artist_id and not skip_spotify_lookup:
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
                                    log_info(f'Searching Spotify for track: {title} by {artist}')
                                    # Normalize album name to remove version suffixes for better matching
                                    # This helps match albums like "Helix (2021 version)" with "Helix"
                                    normalized_album = normalize_album(album) if album else None
                                    log_debug(f'Spotify search params - title: {title}, artist: {artist}, album: {album} (normalized: {normalized_album})')
                                    # For popularity scoring, we pass album for better matching accuracy
                                    # For live/unplugged albums, this is especially important to avoid matching studio versions
                                    spotify_search_results = _run_with_timeout(
                                        search_spotify_track,
                                        API_CALL_TIMEOUT,
                                        f"Spotify track search timed out after {API_CALL_TIMEOUT}s",
                                        title, artist, normalized_album
                                    )
                                    # Record API request for rate limiting
                                    rate_limiter.record_spotify_request()
                                    log_debug(f'Spotify API request recorded for rate limiting')
                                
                                    # Cache results for singles detection reuse (using title as key)
                                    spotify_results_cache[title] = spotify_search_results
                                    log_debug(f'Cached Spotify results for track: {title}')
                            
                                # Update last_spotify_lookup timestamp
                                current_timestamp = datetime.now().isoformat()
                                cursor.execute("""
                                    UPDATE tracks 
                                    SET last_spotify_lookup = ?
                                    WHERE id = ?
                                """, (current_timestamp, track_id))
                                log_debug(f'Updated last_spotify_lookup for track {track_id}')
                            
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
                                    log_info(f'Getting Last.fm info for: {title} by {artist}')
                                    log_debug(f'Last.fm lookup params - artist: {artist}, title: {title}')
                                    lastfm_info = _run_with_timeout(
                                        get_lastfm_track_info,
                                        API_CALL_TIMEOUT,
                                        f"Last.fm lookup timed out after {API_CALL_TIMEOUT}s",
                                        artist, title
                                    )
                                    # Record API request for rate limiting
                                    rate_limiter.record_lastfm_request()
                                    log_debug(f'Last.fm API request recorded for rate limiting')
                                    
                                    log_debug(f'Last.fm API response - listeners: {lastfm_info.get("listeners")}, playcount: {lastfm_info.get("track_play", 0)}')
                                    if lastfm_info and lastfm_info.get("listeners"):
                                        listeners = lastfm_info.get("listeners")
                                        playcount = lastfm_info.get("track_play", 0)
                                        
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
                                            log_debug(f'Last.fm z-score data - listeners: {listeners}, playcount: {playcount}, album_mean_listeners: {sum(album_listeners_list)/len(album_listeners_list) if album_listeners_list else 0:.0f}')
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
                        
                        # Extract Last.fm tags from API response
                        try:
                            if lastfm_info and lastfm_info.get("toptags"):
                                tags_list = lastfm_info.get("toptags", {}).get("tag", [])
                                if tags_list and isinstance(tags_list, list):
                                    tag_names = [tag.get("name") for tag in tags_list if isinstance(tag, dict) and tag.get("name")]
                                    if tag_names:
                                        lastfm_tags_json = json.dumps(tag_names)
                                        log_debug(f'Last.fm tags extracted for: {title} - {len(tag_names)} tags')
                        except Exception as e:
                            log_debug(f'Failed to extract Last.fm tags: {e}')
                        
                        # Extract Discogs genres (requires Discogs release ID or search)
                        if HAVE_DISCOGS and discogs_token:
                            try:
                                discogs_release_id = row_get(track, 'discogs_release_id')
                                if discogs_release_id:
                                    # Try to get genres directly from release
                                    log_debug(f'Fetching Discogs genres for release ID: {discogs_release_id}')
                                    discogs_client = DiscogsClient(token=discogs_token)
                                    # Search for release by ID to get genres
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
                        # Only include sources that have data (score > 0)
                        scores = []
                        weights = []
                        
                        # Calculate dynamic weights based on artist catalogue context
                        # This boosts Last.fm weight for tracks that are outliers in the artist's catalogue
                        dynamic_spotify_weight = SPOTIFY_WEIGHT
                        dynamic_lastfm_weight = LASTFM_WEIGHT
                        
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
                    
                        if age_score > 0:
                            scores.append(age_score)
                            weights.append(AGE_WEIGHT)
                            log_debug(f'Including age score: {age_score} (weight: {AGE_WEIGHT})')
                    
                        # Calculate weighted average
                        if scores and weights:
                            total_weight = sum(weights)
                            popularity_score = sum(s * w for s, w in zip(scores, weights)) / total_weight
                            track_updates.append((popularity_score, spotify_score, lastfm_score, spotify_genres_json, lastfm_tags_json, discogs_genres_json, musicbrainz_genres_json, album_art_url, track_id))
                            scanned_count += 1
                            album_scanned += 1
                            log_info(f'Track scanned successfully: "{title}" (score: {popularity_score:.1f})')
                            log_debug(f'Weighted popularity calculation - spotify: {spotify_score}, lastfm: {lastfm_score}, age: {age_score}, final: {popularity_score:.1f}')
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
                if track_updates and not singles_only:
                    cursor.executemany(
                        "UPDATE tracks SET popularity_score = ?, spotify_score = ?, lastfm_ratio = ?, spotify_genres = ?, lastfm_tags = ?, discogs_genres = ?, musicbrainz_genres = ?, cover_art_url = ? WHERE id = ?",
                        track_updates
                    )
                    conn.commit()
                    log_debug(f"Batch committed {len(track_updates)} popularity scores and genre sources for album '{album}'")
                    if album_art_url:
                        log_info(f"[ALBUM_ART] Album art URL cached for {album}: {album_art_url}")
                    else:
                        log_debug(f"[ALBUM_ART] Album art will be fetched on-demand from Navidrome or Apple Music sources")
                
                if not singles_only:
                    log_unified(f'Popularity Scan - Popularity Scanning for {album} Complete')
                    log_info(f'Album "{artist} - {album}" scanned. Popularity applied to {album_scanned} tracks')
                    
                    # After album complete, calculate standout tracks for artist
                    # Standout = z-score >= 2.0 (2+ stdev above artist mean) + >= 1000 listeners
                    if album_scanned > 0:
                        try:
                            log_info(f'Analyzing standout tracks for artist: {artist}')
                            cursor.execute("""
                                SELECT id, title, lastfm_track_playcount FROM tracks
                                WHERE artist = ? AND is_single = 0 AND album NOT IN (
                                    SELECT DISTINCT album FROM tracks WHERE artist = ? AND album_context_live = 1
                                ) AND album NOT IN (
                                    SELECT DISTINCT album FROM tracks WHERE artist = ? AND discogs_format_descriptions LIKE '%live%'
                                )
                            """, (artist, artist, artist))
                            artist_tracks = cursor.fetchall()
                            
                            if artist_tracks:
                                listeners_list = [row_get(t, 'lastfm_track_playcount', 0) for t in artist_tracks if row_get(t, 'lastfm_track_playcount', 0) > 0]
                                
                                if len(listeners_list) >= 2:
                                    artist_mean = mean(listeners_list)
                                    artist_stdev = stdev(listeners_list) if len(listeners_list) > 1 else 0
                                    
                                    log_debug(f'Artist {artist} listener stats: mean={artist_mean:.0f}, stdev={artist_stdev:.0f}')
                                    
                                    standup_count = 0
                                    for track in artist_tracks:
                                        track_id = row_get(track, 'id')
                                        track_title = row_get(track, 'title')
                                        listeners = row_get(track, 'lastfm_track_playcount', 0)
                                        
                                        if listeners > 0 and artist_stdev > 0:
                                            track_zscore = (listeners - artist_mean) / artist_stdev
                                            
                                            # Mark as standout if z-score >= 2.0 AND listeners >= 1000
                                            is_standout = 1 if (track_zscore >= 2.0 and listeners >= 1000) else 0
                                            
                                            if is_standout:
                                                cursor.execute("""
                                                    UPDATE tracks SET is_standout_track = ?, artist_z_score = ?
                                                    WHERE id = ?
                                                """, (is_standout, track_zscore, track_id))
                                                standup_count += 1
                                                log_info(f'Marked as standout: {track_title} (z-score: {track_zscore:.2f}, listeners: {listeners})')
                                            else:
                                                cursor.execute("""
                                                    UPDATE tracks SET is_standout_track = 0, artist_z_score = ?
                                                    WHERE id = ?
                                                """, (track_zscore, track_id))
                                        else:
                                            cursor.execute("""
                                                UPDATE tracks SET is_standout_track = 0
                                                WHERE id = ?
                                            """, (track_id,))
                                    
                                    conn.commit()
                                    if standup_count > 0:
                                        log_unified(f'Popularity Scan - Identified {standup_count} standout tracks for {artist}')
                        
                        except Exception as e:
                            log_debug(f'Standout track analysis failed for {artist}: {e}')
                
                # End of popularity scanning section (skipped in singles_only mode)

                # Perform singles detection for album tracks
                log_info(f'Starting singles detection for "{artist} - {album}"')
                # Get album type from MusicBrainz with Spotify fallback
                from api_clients.musicbrainz import get_album_type_with_fallback
                spotify_album_type = row_get(album_tracks[0] if album_tracks else {}, 'spotify_album_type', '')
                album_type, type_source = get_album_type_with_fallback(artist, album, spotify_album_type, enabled=HAVE_MUSICBRAINZ)
                is_compilation = album_type.lower() == 'compilation' if album_type else False
                log_debug(f'Album context: {len(album_tracks)} total tracks, compilation={is_compilation}, album_type={album_type} (source: {type_source})')
                singles_detected = 0
                
                # Log which sources are available for single detection
                sources_available = []
                sources_available.append("Spotify")
                if HAVE_MUSICBRAINZ:
                    sources_available.append("MusicBrainz")
                if HAVE_DISCOGS and discogs_token:
                    sources_available.append("Discogs")
                if HAVE_DISCOGS_VIDEO and discogs_token:
                    sources_available.append("Discogs Video")
                log_info(f'Available sources for single detection: {[", ".join(sources_available)]}')
                log_debug(f'Source details: Spotify=enabled, MB={HAVE_MUSICBRAINZ}, Discogs={HAVE_DISCOGS and bool(discogs_token)}, Video={HAVE_DISCOGS_VIDEO and bool(discogs_token)}')
                
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
                
                # Track progress within singles detection phase
                singles_processed = 0
                # Pre-calculate milestone track counts for efficient checking
                singles_milestone_25 = int(album_track_count * 0.25)
                singles_milestone_50 = int(album_track_count * 0.50)
                singles_milestone_75 = int(album_track_count * 0.75)
                singles_milestones_logged = set()
                
                for track in album_tracks:
                    track_id = track["id"]
                    title = track["title"]
                    
                    log_debug(f"Processing single detection for track: {title} (ID: {track_id})")
                    
                    # Get additional fields for advanced detection
                    track_isrc = row_get(track, "isrc", None)
                    track_duration = row_get(track, "duration", None)
                    track_album_type = row_get(track, "spotify_album_type", None)
                    
                    # Get the popularity score for this track (may have been calculated earlier)
                    track_popularity = 0.0
                    cursor.execute("SELECT popularity_score FROM tracks WHERE id = ?", (track_id,))
                    pop_row = cursor.fetchone()
                    if pop_row and pop_row[0]:
                        track_popularity = pop_row[0]
                    
                    log_debug(f"Single detection params - track: {title}, isrc: {track_isrc}, duration: {track_duration}, popularity: {track_popularity}, album_type: {track_album_type}")
                    
                    # Use the centralized single detection function with advanced parameters
                    # Use track's individual artist, not the grouping artist
                    track_artist = track["artist"]
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
                        album_type=track_album_type,
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
                    
                    # Preserve user-set singles: if track was user-marked and detection found nothing, keep it marked
                    if track_id in user_set_singles and not is_single:
                        is_single = True
                        single_confidence = "user"  # Mark as user-set
                        # Keep sources empty to indicate user-set
                        log_info(f"Preserving user-set single flag for: {title}")
                        log_debug(f"User-set single preserved - track_id: {track_id}, auto_detection: False")
                    
                    # Queue single detection results for batch update
                    if is_single or single_sources:
                        singles_updates.append((
                            1 if is_single else 0,
                            single_confidence,
                            json.dumps(single_sources),
                            track_id
                        ))
                        if is_single:
                            singles_detected += 1
                            if single_sources:
                                source_str = ", ".join(single_sources)
                                log_info(f"Single detected: {title} ({single_confidence} confidence, sources: {source_str})")
                            else:
                                log_info(f"Single detected: {title} (user-set)")
                            log_debug(f"Single detection confirmed - track_id: {track_id}, confidence: {single_confidence}, sources: {single_sources}")
                    
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
                
                # Batch update all singles detection results for this album in one commit
                if singles_updates:
                    cursor.executemany(
                        """UPDATE tracks 
                        SET is_single = ?, single_confidence = ?, single_sources = ?
                        WHERE id = ?""",
                        singles_updates
                    )
                    conn.commit()
                    log_debug(f"Batch committed {len(singles_updates)} singles detection results for album '{album}'")
                
                # Log summary of singles detection
                high_conf_count = sum(1 for update in singles_updates if update[0] == 1)
                log_info(f'Singles detection complete: {singles_detected} high-confidence single(s) detected for "{artist} - {album}" ({len(singles_updates)} tracks checked)')
                log_debug(f'Singles detection summary - high_conf: {high_conf_count}, total_checked: {len(singles_updates)}')

                # Calculate star ratings for album tracks
                log_info(f'Calculating star ratings for "{artist} - {album}"')
                log_debug(f'Star rating calculation starting for album: {album}')
                
                # Note: artist_stats was already calculated before single detection to support
                # conditional z-score detection. We only need to update the database here.
                # Just update the artist_stats table with popularity statistics
                if artist_stats['track_count'] > 0:
                    # Update artist_stats table with popularity statistics
                    cursor.execute("""
                        UPDATE artist_stats 
                        SET avg_popularity = ?, median_popularity = ?, popularity_stddev = ?
                        WHERE artist_name = ?
                    """, (artist_stats['avg_popularity'], artist_stats['median_popularity'], 
                          artist_stats['stddev_popularity'], artist))
                    conn.commit()
                    log_debug(f"Updated artist_stats table for {artist}")
                
                # Get all tracks for this album with their popularity scores and single detection
                # Try matching on artist field first, then fall back to album_artist field
                cursor.execute(
                    "SELECT id, title, popularity_score, is_single, single_confidence, single_sources, lastfm_track_playcount, is_standout_track FROM tracks WHERE artist = ? AND album = ? ORDER BY popularity_score DESC",
                    (artist, album)
                )
                album_tracks_with_scores = cursor.fetchall()
                
                # If no results from first query, fall back to matching on album_artist
                if not album_tracks_with_scores:
                    log_debug(f"No tracks found with artist field match for artist '{artist}', falling back to album_artist field match")
                    cursor.execute(
                        "SELECT id, title, popularity_score, is_single, single_confidence, single_sources, lastfm_track_playcount, is_standout_track FROM tracks WHERE album_artist = ? AND album = ? ORDER BY popularity_score DESC",
                        (artist, album)
                    )
                    album_tracks_with_scores = cursor.fetchall()
                
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
                    
                    if valid_scores:
                        popularity_mean = mean(valid_scores)
                        popularity_stddev = stdev(valid_scores) if len(valid_scores) > 1 else 0
                        log_debug(f"Star rating statistics - mean: {popularity_mean}, stddev: {popularity_stddev}, valid_scores_count: {len(valid_scores)}")
                        # Calculate z-scores for all tracks
                        zscores = []
                        for score in valid_scores:
                            if popularity_stddev > 0:
                                zscore = (score - popularity_mean) / popularity_stddev
                            else:
                                zscore = 0
                            zscores.append(zscore)
                        
                        # Get mean of top 50% z-scores for medium confidence threshold
                        # Use heapq.nlargest for efficiency with large albums
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
                        
                        log_info(f"Album stats: mean={popularity_mean:.1f}, stddev={popularity_stddev:.1f}")
                        log_debug(f"Confidence thresholds - high: {high_conf_threshold:.1f}, medium_zscore: {medium_conf_zscore_threshold:.2f}")
                    else:
                        popularity_mean = DEFAULT_POPULARITY_MEAN
                        popularity_stddev = 0
                        high_conf_threshold = DEFAULT_POPULARITY_MEAN + DEFAULT_HIGH_CONF_OFFSET
                        medium_conf_zscore_threshold = DEFAULT_MEDIUM_CONF_THRESHOLD
                        log_debug(f"Using default thresholds - no valid scores found")
                    
                    # Calculate median score for band-based threshold (legacy)
                    median_score = median(scores) if scores else DEFAULT_POPULARITY_MEAN
                    if median_score == 0:
                        median_score = DEFAULT_POPULARITY_MEAN
                    jump_threshold = median_score * 1.7
                    log_debug(f"Band-based thresholds - median: {median_score}, jump_threshold: {jump_threshold}")
                    
                    star_distribution = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
                    
                    # Batch updates for better performance
                    updates = []
                    # Track which medium-confidence tracks should be upgraded to is_single=1
                    single_upgrades = []
                    
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
                        
                        # Check if this track was excluded from statistics
                        # Excluded tracks should not participate in confidence-based star rating upgrades
                        is_excluded_track = i in excluded_indices
                        
                        # Calculate z-score for this track
                        if popularity_stddev > 0 and popularity_score > 0:
                            track_zscore = (popularity_score - popularity_mean) / popularity_stddev
                        else:
                            track_zscore = 0
                        
                        log_debug(f"Track star rating calc - {title}: popularity={popularity_score}, zscore={track_zscore}, excluded={is_excluded_track}")
                        
                        # Calculate band-based star rating (baseline)
                        band_index = i // band_size
                        stars = max(1, 4 - band_index)
                        
                        # NEW: 5-STAR LOGIC WITH DUAL ARTIST + ALBUM CONTEXT
                        # A track becomes 5★ if ANY of these conditions are met:
                        # 1. Artist-level standout (zscore >= 2.0) + top 10% of artist tracks globally
                        # 2. High popularity + strong z-score (zscore >= 2.0 AND popularity significantly above album mean)
                        # 3. User-set single
                        # 4. High-confidence single detection
                        # 5. Medium-confidence with 2+ sources
                        #
                        # Artist context: Dual criteria for maximum relevance
                        # - Top 10% globally: Track is in artist's most-streamed catalogue (global significance)
                        # - Album outlier: Track stands out from its album peers (local significance)
                        # Both needed = meaningful standout content
                        #
                        # Popularity context: Strong outlier within album (z-score >= 2.0) with high absolute popularity
                        # - Z-score >= 2.0: Top ~2% of tracks in album by popularity
                        # - Popularity significantly above album mean: Standard deviation of popularity indicates separation
                        
                        # Skip confidence-based upgrades for excluded tracks (e.g., bonus tracks with parentheses)
                        # These tracks were excluded from statistics calculation, so their z-scores are not meaningful
                        if not is_excluded_track:
                            # Apply new 5-star rule
                            # FIRST: Dual artist context check
                            # Track must be BOTH: top 10% globally AND album outlier (zscore >= 2.0)
                            track_lastfm_listeners = row_get(track_row, "lastfm_track_playcount", 0) or 0
                            artist_context_threshold = artist_stats.get('top_10_percentile_threshold', 0) or 0
                            is_top_10_global = track_lastfm_listeners >= artist_context_threshold if artist_context_threshold > 0 else False
                            is_album_outlier = track_zscore >= 2.0
                            
                            if is_top_10_global and is_album_outlier:
                                stars = 5
                                log_info(f"5-star assignment: {title} (top 10% global + album outlier, listeners={track_lastfm_listeners}, zscore={track_zscore:.2f})")
                                log_debug(f"Dual criteria met - track_id: {track_id}, listeners: {track_lastfm_listeners}, top_10_threshold: {artist_context_threshold}, zscore: {track_zscore:.2f}")
                            # SECOND: High popularity + strong z-score (alternative 5-star method)
                            # Track with z-score >= 2.0 AND popularity significantly above album mean gets 5 stars
                            elif track_zscore >= 2.0 and popularity_score > popularity_mean * 1.5:
                                stars = 5
                                log_info(f"5-star assignment: {title} (high popularity + strong outlier, pop={popularity_score:.1f} vs album_mean={popularity_mean:.1f}, zscore={track_zscore:.2f})")
                                log_debug(f"High pop+zscore met - track_id: {track_id}, pop: {popularity_score}, album_mean: {popularity_mean}, zscore: {track_zscore:.2f}")
                            # User-set singles always get 5 stars
                            elif single_confidence == "user":
                                stars = 5
                                log_info(f"5-star assignment: {title} (user-set single)")
                                log_debug(f"User-set single - track_id: {track_id}")
                            # High confidence always gets 5 stars
                            elif single_confidence == "high":
                                stars = 5
                                log_info(f"5-star assignment: {title} (high-confidence single)")
                                log_debug(f"High confidence single detected - track_id: {track_id}")
                            # Medium confidence with 2+ sources gets 5 stars
                            elif single_confidence == "medium":
                                # Count the number of medium-confidence sources
                                # Each unique source in single_sources represents a medium-confidence method
                                medium_conf_count = len(single_sources) if single_sources else 0
                                if medium_conf_count >= 2:
                                    stars = 5
                                    # Upgrade is_single flag for medium confidence tracks with 2+ sources
                                    if not is_single:
                                        single_upgrades.append(track_id)
                                        log_info(f"5-star assignment: {title} (has {medium_conf_count} medium-confidence sources) - upgraded to single")
                                    else:
                                        log_info(f"5-star assignment: {title} (has {medium_conf_count} medium-confidence sources)")
                                    log_debug(f"Medium confidence with {medium_conf_count} sources - track_id: {track_id}")
                            # Standout tracks (high Last.fm scrobbles) get 5 stars
                            elif is_standout_track:
                                stars = 5
                                log_info(f"5-star assignment: {title} (standout track - high Last.fm scrobbles)")
                                log_debug(f"Standout track - track_id: {track_id}")
                            
                            # NEW: Artist-level popularity context
                            # Downgrade singles from underperforming albums (only for medium confidence - high confidence singles are independently verified)
                            # High-confidence singles are confirmed by multiple authoritative sources (Spotify, Discogs, MusicBrainz, Last.fm),
                            # so they should always get 5 stars regardless of album performance
                            if album_is_underperforming and single_confidence == "medium":
                                if artist_stats['median_popularity'] > 0:
                                    # Only downgrade if track popularity is also below artist median
                                    if popularity_score < artist_stats['median_popularity']:
                                        # Downgrade by 1 star (but keep at least 3 stars for medium-confidence singles)
                                        original_stars = stars
                                        stars = max(stars - 1, 3)
                                        if stars < original_stars:
                                            log_info(f"Downgraded '{title}': {original_stars}★ -> {stars}★ (underperforming album, pop={popularity_score:.1f} < artist_median={artist_stats['median_popularity']:.1f})")
                                            log_debug(f"Downgrade applied - album_is_underperforming: True, track_pop: {popularity_score}, artist_median: {artist_stats['median_popularity']}, confidence={single_confidence}")
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
                        """UPDATE tracks SET stars = ? WHERE id = ?""",
                        updates
                    )
                    
                    # Upgrade is_single flag for medium confidence tracks with 2+ sources
                    if single_upgrades:
                        cursor.executemany(
                            """UPDATE tracks SET is_single = 1 WHERE id = ?""",
                            ((track_id,) for track_id in single_upgrades)
                        )
                        log_info(f"Upgraded {len(single_upgrades)} medium-confidence track(s) to single status (2+ sources)")
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
                            """SELECT id, title, stars, is_single, single_confidence, single_sources, 
                                      is_standout_track, artist_z_score
                            FROM tracks 
                            WHERE artist = ? AND album = ? 
                            ORDER BY stars DESC, popularity_score DESC""",
                            (artist, album)
                        )
                        final_tracks = cursor.fetchall()
                        
                        # Categorize tracks
                        detected_singles = []      # is_single = 1, 5 stars
                        standout_tracks = []       # is_standout_track = 1, 5 stars
                        possible_singles = []      # Medium confidence, not marked as single
                        rest_of_album = []         # Other tracks
                        
                        SOURCE_DISPLAY_NAMES = {
                            "musicbrainz": "MusicBrainz",
                            "discogs": "Discogs",
                            "discogs_video": "Discogs Video",
                            "spotify": "Spotify"
                        }
                        
                        for track_row in final_tracks:
                            track_title = track_row["title"]
                            track_stars = track_row["stars"] if track_row["stars"] else 0
                            track_is_single = track_row["is_single"] if track_row["is_single"] else 0
                            track_is_standout = track_row["is_standout_track"] if track_row["is_standout_track"] else 0
                            track_single_confidence = track_row["single_confidence"] if track_row["single_confidence"] else ""
                            track_sources_json = track_row["single_sources"] if track_row["single_sources"] else "[]"
                            track_zscore = track_row["artist_z_score"] if track_row["artist_z_score"] else 0
                            
                            # Parse single sources
                            try:
                                if track_sources_json and isinstance(track_sources_json, str):
                                    track_sources = json.loads(track_sources_json)
                                else:
                                    track_sources = []
                            except json.JSONDecodeError:
                                track_sources = []
                            
                            # Format sources for display
                            formatted_sources = [SOURCE_DISPLAY_NAMES.get(s, s.capitalize()) for s in track_sources]
                            sources_str = ", ".join(formatted_sources) if formatted_sources else ""
                            
                            # Create star rating string (max 5 stars)
                            stars_str = "★" * min(track_stars, 5)
                            
                            # Determine detection method(s) for display
                            detection_methods = []
                            if track_is_single and sources_str:
                                detection_methods.append(sources_str)
                            if track_is_standout:
                                detection_methods.append(f"Standout (z-score: {track_zscore:.2f})")
                            if track_single_confidence and not track_is_single:
                                detection_methods.append(f"Possible Single ({track_single_confidence})")
                            
                            method_str = " - " + " | ".join(detection_methods) if detection_methods else ""
                            
                            # Categorize track
                            if track_is_single and track_stars == 5:
                                detected_singles.append((track_title, stars_str, method_str))
                            elif track_is_standout and track_stars == 5:
                                standout_tracks.append((track_title, stars_str, method_str))
                            elif track_single_confidence == "medium" and not track_is_single:
                                possible_singles.append((track_title, stars_str, method_str))
                            else:
                                rest_of_album.append((track_title, stars_str, method_str))
                        
                        # Log categorized results
                        total_logged = len(detected_singles) + len(standout_tracks) + len(possible_singles) + len(rest_of_album)
                        log_debug(f"Track categorization for {album}: detected_singles={len(detected_singles)}, standout={len(standout_tracks)}, possible={len(possible_singles)}, rest={len(rest_of_album)}, total={total_logged}")
                        
                        if detected_singles:
                            log_unified(f"Single Detection Scan - ===== Detected Singles =====")
                            log_debug(f"Logging {len(detected_singles)} detected singles for {album}")
                            for title, stars, method in detected_singles:
                                log_entry = f"Single Detection Scan - {stars:<5} {artist} - {title}{method}"
                                log_debug(f"About to log: {log_entry}")
                                log_unified(log_entry)
                        
                        if standout_tracks:
                            log_unified(f"Single Detection Scan - ===== Standout Tracks =====")
                            for title, stars, method in standout_tracks:
                                log_unified(f"Single Detection Scan - {stars:<5} {artist} - {title}{method}")
                        
                        if possible_singles:
                            log_unified(f"Single Detection Scan - ===== Possible Singles (Medium Confidence) =====")
                            for title, stars, method in possible_singles:
                                log_unified(f"Single Detection Scan - {stars:<5} {artist} - {title}{method}")
                        
                        # Always log rest of album tracks if there are any or if this is the only category
                        if rest_of_album:
                            # Only show header if there were detected singles/standout/possible singles
                            if detected_singles or standout_tracks or possible_singles:
                                log_unified(f"Single Detection Scan - ===== Rest of Album =====")
                            # Log individual rest-of-album tracks
                            for title, stars, method in rest_of_album:
                                log_unified(f"Single Detection Scan - {stars:<5} {artist} - {title}{method}")
                        
                        log_debug(f"Successfully logged all categorized tracks for album {album}")
                        
                    except Exception as e:
                        log_info(f"Error logging categorized tracks for album {album}: {e}")
                        log_debug(f"Exception in track categorization: {type(e).__name__}: {str(e)}")
                        import traceback
                        log_debug(f"Traceback: {traceback.format_exc()}")
                
                # Update last_scanned timestamp for all tracks in this album
                current_timestamp = datetime.now().isoformat()
                cursor.execute(
                    """UPDATE tracks SET last_scanned = ? WHERE artist = ? AND album = ?""",
                    (current_timestamp, artist, album)
                )
                
                # Ensure changes are committed before logging to scan_history to avoid database lock conflicts
                conn.commit()
                log_debug(f"Committed all changes for album: {album}")
                
                # Log album scan
                log_album_scan(artist, album, 'popularity', album_scanned, 'completed')
                log_debug(f"Logged album scan to scan_history - album: {album}, tracks_scanned: {album_scanned}")

            # After artist scans, evaluate essential playlist for artist (Case A: 10+ five-star OR Case B: 100+ tracks)
            # Get ALL tracks for this artist (not just 5-star) to properly apply Case A/B logic
            cursor.execute(
                """SELECT id, artist, album, title, stars
                FROM tracks 
                WHERE artist = ?
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

            # Update artist progress tracking after completing all albums for this artist
            # Note: Progress is saved once per artist (not per track) to balance granularity
            # with I/O efficiency. Original code saved after every track which could result
            # in thousands of writes for large libraries. Per-artist updates provide adequate
            # progress visibility while reducing file I/O by orders of magnitude.
            # If scan is interrupted, it can resume from the last completed artist.
            processed_artists += 1
            save_popularity_progress(processed_artists, total_artists)
            log_debug(f"Progress saved - {processed_artists}/{total_artists} artists processed")

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
                "percent_complete": 100
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
        cursor.execute("SELECT DISTINCT artist FROM tracks")
        artists = [row[0] for row in cursor.fetchall()]
        
        if not artists:
            log_basic("âš ï¸ No cached tracks in DB. Skipping playlist refresh.")
            return
        
        for name in artists:
            cursor.execute("SELECT id, artist, album, title, stars FROM tracks WHERE artist = ?", (name,))
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
