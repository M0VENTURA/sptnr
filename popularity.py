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
from contextlib import contextmanager
from datetime import datetime
from statistics import median
from concurrent.futures import ThreadPoolExecutor
import concurrent.futures
from api_clients import session, timeout_safe_session
from helpers import find_matching_spotify_single

# Import API clients for single detection at module level
try:
    from api_clients.musicbrainz import MusicBrainzClient
    HAVE_MUSICBRAINZ = True
except ImportError as e:
    HAVE_MUSICBRAINZ = False
    logging.debug(f"MusicBrainz client unavailable: {e}")
    
try:
    from api_clients.discogs import DiscogsClient
    HAVE_DISCOGS = True
    HAVE_DISCOGS_VIDEO = True
except ImportError as e:
    HAVE_DISCOGS = False
    HAVE_DISCOGS_VIDEO = False
    logging.debug(f"Discogs client unavailable: {e}")

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


# Dedicated popularity logger (no propagation to root)



# --- Dual Logger Setup: sptnr.log and unified_scan.log ---
import logging
LOG_PATH = os.environ.get("LOG_PATH", "/config/sptnr.log")
UNIFIED_LOG_PATH = os.environ.get("UNIFIED_SCAN_LOG_PATH", "/config/unified_scan.log")
VERBOSE = (
    os.environ.get("SPTNR_VERBOSE_POPULARITY") or os.environ.get("SPTNR_VERBOSE") or "0"
) == "1"
# Force rescan of albums even if they were already scanned
FORCE_RESCAN = os.environ.get("SPTNR_FORCE_RESCAN", "0") == "1"
SERVICE_PREFIX = "popularity_"

class ServicePrefixFormatter(logging.Formatter):
    def __init__(self, prefix, fmt=None):
        super().__init__(fmt or '%(asctime)s [%(levelname)s] %(message)s')
        self.prefix = prefix
    def format(self, record):
        record.msg = f"{self.prefix}{record.msg}"
        return super().format(record)

formatter = ServicePrefixFormatter(SERVICE_PREFIX)
file_handler = logging.FileHandler(LOG_PATH)
file_handler.setFormatter(formatter)
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(formatter)
logging.basicConfig(level=logging.INFO, handlers=[file_handler, stream_handler])

# Dedicated logger for unified_scan.log
unified_logger = logging.getLogger("unified_scan")
unified_file_handler = logging.FileHandler(UNIFIED_LOG_PATH)
# Use a clean formatter without service prefix for unified log
unified_formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
unified_file_handler.setFormatter(unified_formatter)
unified_logger.setLevel(logging.INFO)
# Always add the file handler (even if handlers exist)
unified_logger.addHandler(unified_file_handler)
unified_logger.propagate = False
print("unified_logger handlers:", unified_logger.handlers)

def _flush_handlers(logger_obj):
    """Flush all handlers for a logger to ensure messages are written immediately"""
    try:
        for handler in logger_obj.handlers:
            handler.flush()
    except Exception as e:
        # Log flush errors at debug level to aid troubleshooting
        import logging
        logging.debug(f"Failed to flush log handlers: {e}")

def log_basic(msg):
    logging.info(msg)
    _flush_handlers(logging.getLogger())

def log_unified(msg):
    unified_logger.info(msg)
    _flush_handlers(unified_logger)

def log_verbose(msg):
    if VERBOSE:
        logging.info(msg)
        _flush_handlers(logging.getLogger())




DB_PATH = os.environ.get("DB_PATH", "/database/sptnr.db")
POPULARITY_PROGRESS_FILE = os.environ.get("POPULARITY_PROGRESS_FILE", "/database/popularity_scan_progress.json")
NAVIDROME_PROGRESS_FILE = os.environ.get("NAVIDROME_PROGRESS_FILE", "/database/navidrome_scan_progress.json")
from popularity_helpers import (
    get_spotify_artist_id,
    search_spotify_track,
    get_lastfm_track_info,
    get_listenbrainz_score,
    score_by_age,
    update_artist_id_for_artist,
    SPOTIFY_WEIGHT,
    LASTFM_WEIGHT,
    LISTENBRAINZ_WEIGHT,
    AGE_WEIGHT,
)

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
    conn = sqlite3.connect(DB_PATH, timeout=60.0)
    conn.execute("PRAGMA journal_mode=WAL")
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
    zscore_threshold: float = 0.20
) -> dict:
    """
    Detect if a track is a single using multiple data sources.
    
    This is the canonical single detection logic used by popularity.py.
    Other modules should call this function to ensure consistent behavior.
    
    NEW: Enhanced with advanced single detection logic including:
    - ISRC-based track version matching
    - Title+duration matching (±2 seconds)
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
                verbose=verbose,
                album_type=album_type
            )
            
            # Store result in database
            store_single_detection_result(conn, track_id, result)
            
            conn.close()
            
            # Return in expected format
            return {
                "sources": result['single_sources'],
                "confidence": result['single_confidence'],
                "is_single": result['is_single']
            }
        except ImportError as e:
            if verbose:
                log_unified(f"   ⚠ Enhanced detection module not available: {e}")
            # Fall through to standard detection
        except Exception as e:
            if verbose:
                log_unified(f"   ⚠ Enhanced detection failed, falling back to standard: {e}")
            import traceback
            if verbose:
                log_unified(f"   Error details: {traceback.format_exc()}")
            # Fall through to standard detection
    
    # Ignore obvious non-singles by keywords
    if any(k in title.lower() for k in IGNORE_SINGLE_KEYWORDS):
        if verbose:
            log_verbose(f"   ⊗ Skipping non-single: {title} (keyword filter)")
        return {
            "sources": [],
            "confidence": "low",
            "is_single": False
        }
    
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
                    log_unified(f"   ✓ Loaded Discogs token from config.yaml")
        except Exception as e:
            # Always log config loading errors, not just in verbose mode
            log_unified(f"   ⚠ Could not load Discogs token from config at {config_path}: {e}")
    
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
                log_verbose(f"   ✓ Reusing cached Spotify results for {title}")
        
        if spotify_results and isinstance(spotify_results, list) and len(spotify_results) > 0:
            # Use new sophisticated matching logic
            # Convert duration from seconds to milliseconds if provided
            duration_ms = int(duration * 1000) if duration else None
            
            # Log all releases before filtering if verbose
            if verbose:
                log_verbose(f"   Spotify returned {len(spotify_results)} releases for {title}")
            
            # Use the sophisticated version-aware matching
            matched_release = find_matching_spotify_single(
                spotify_results=spotify_results,
                track_title=title,
                track_duration_ms=duration_ms,
                duration_tolerance_sec=2,
                logger=logger if verbose else None
            )
            
            if matched_release:
                single_sources.append("spotify")
                album_info = matched_release.get("album", {})
                if verbose:
                    log_verbose(f"   ✓ Spotify confirms single: {title}")
                    log_verbose(f"      Matched release: {matched_release.get('name')}")
                    log_verbose(f"      Album: {album_info.get('name')} (type: {album_info.get('album_type')})")
            else:
                if verbose:
                    log_verbose(f"   ⓘ No matching Spotify single found for {title}")
    except TimeoutError as e:
        if verbose:
            log_verbose(f"Spotify single check timed out for {title}: {e}")
    except Exception as e:
        if verbose:
            log_verbose(f"Spotify single check failed for {title}: {e}")
    
    # Second check: MusicBrainz single detection
    if HAVE_MUSICBRAINZ:
        try:
            if verbose:
                log_unified(f"   Checking MusicBrainz for single: {title}")
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
                    if verbose:
                        log_unified(f"   ✓ MusicBrainz confirms single: {title}")
                else:
                    if verbose:
                        log_verbose(f"   ⓘ MusicBrainz does not confirm single: {title}")
        except TimeoutError as e:
            if verbose:
                log_unified(f"   ⏱ MusicBrainz single check timed out for {title}: {e}")
        except Exception as e:
            if verbose:
                log_unified(f"   ⚠ MusicBrainz single check failed for {title}: {e}")
    else:
        if verbose:
            log_verbose(f"   ⓘ MusicBrainz client not available")
    
    # Third check: Discogs single detection
    if HAVE_DISCOGS and discogs_token:
        try:
            # Always log Discogs API calls (not dependent on verbose)
            log_unified(f"   Checking Discogs for single: {title}")
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
                    log_unified(f"   ✓ Discogs confirms single: {title}")
                else:
                    if verbose:
                        log_verbose(f"   ⓘ Discogs does not confirm single: {title}")
        except TimeoutError as e:
            log_unified(f"   ⏱ Discogs single check timed out for {title}: {e}")
        except Exception as e:
            log_unified(f"   ⚠ Discogs single check failed for {title}: {e}")
    else:
        if not HAVE_DISCOGS:
            log_unified(f"   ⓘ Discogs client not available")
        elif not discogs_token:
            log_unified(f"   ⓘ Discogs token not configured")
    
    # Fourth check: Discogs video detection (requires second source for confirmation)
    if HAVE_DISCOGS_VIDEO and discogs_token:
        try:
            # Use timeout-safe client to prevent retries from exceeding timeout
            discogs_client = _get_timeout_safe_discogs_client(discogs_token)
            if discogs_client:
                result = _run_with_timeout(
                    lambda: discogs_client.has_official_video(title, artist),
                    API_CALL_TIMEOUT,
                    f"Discogs video detection timed out after {API_CALL_TIMEOUT}s"
                )
                if result:
                    # Only add if we have at least one other source
                    if len(single_sources) >= 1:
                        single_sources.append("discogs_video")
                        if verbose:
                            log_verbose(f"   ✓ Discogs video confirms single (with other source): {title}")
                    else:
                        if verbose:
                            log_verbose(f"   ⓘ Discogs video detected but needs second source: {title}")
        except TimeoutError as e:
            if verbose:
                log_verbose(f"Discogs video check timed out for {title}: {e}")
        except Exception as e:
            if verbose:
                log_verbose(f"Discogs video check failed for {title}: {e}")
    
    # Calculate confidence based on number of sources (Jan 2nd logic)
    if len(single_sources) >= 2:
        single_confidence = "high"
    elif len(single_sources) == 1:
        single_confidence = "medium"
    else:
        single_confidence = "low"
    
    # Album context rule: downgrade medium → low if album has >3 tracks
    if single_confidence == "medium" and album_track_count > 3:
        single_confidence = "low"
        if verbose:
            log_verbose(f"   ⓘ Downgraded {title} confidence to low (album has {album_track_count} tracks)")
    
    # is_single = True only for high confidence singles (5* singles)
    is_single = single_confidence == "high"
    
    return {
        "sources": single_sources,
        "confidence": single_confidence,
        "is_single": is_single
    }


def popularity_scan(
    verbose: bool = False, 
    resume_from: str = None,
    artist_filter: str = None,
    album_filter: str = None,
    skip_header: bool = False,
    force: bool = False
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
    """
    if not skip_header:
        log_unified("=" * 60)
        log_unified("Popularity Scanner Started")
        log_unified("=" * 60)
        log_unified(f"🟢 Popularity scan started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    # Log scan mode
    if FORCE_RESCAN or force:
        log_unified("⚠ Force rescan mode enabled - will rescan all albums regardless of scan history")
    else:
        log_unified("📋 Normal scan mode - will skip albums that were already scanned")

    # Log filter mode
    if artist_filter:
        if album_filter:
            log_unified(f"🔍 Filtering: artist='{artist_filter}', album='{album_filter}'")
        else:
            log_unified(f"🔍 Filtering: artist='{artist_filter}'")
    elif resume_from:
        log_unified(f"⏩ Resuming from artist: '{resume_from}'")

    # Initialize popularity helpers to configure Spotify client
    from popularity_helpers import configure_popularity_helpers
    try:
        configure_popularity_helpers()
        if not skip_header:
            log_unified("✅ Spotify client configured")
    except Exception as e:
        log_unified(f"⚠ Warning: Failed to configure Spotify client: {e}")
        log_unified("Popularity scan will continue but Spotify lookups may fail")
        if VERBOSE:
            import traceback
            logging.error(f"Configuration error details: {traceback.format_exc()}")

    log_verbose("Connecting to database for popularity scan...")
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Load strict matching configuration from config.yaml
        strict_spotify_matching = False
        duration_tolerance_sec = 2
        try:
            config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f)
            features = config.get('features', {})
            strict_spotify_matching = features.get('strict_spotify_matching', False)
            duration_tolerance_sec = features.get('spotify_duration_tolerance', 2)
            if strict_spotify_matching:
                log_unified(f"✅ Strict Spotify matching enabled (duration tolerance: ±{duration_tolerance_sec}s)")
            else:
                log_verbose("Standard Spotify matching mode (highest popularity)")
        except Exception as e:
            log_verbose(f"Could not load strict matching config (using defaults): {e}")

        # Build SQL query with optional filters
        sql_conditions = []
        
        # Only filter by popularity_score if not forcing rescan
        if not (FORCE_RESCAN or force):
            sql_conditions.append("(popularity_score IS NULL OR popularity_score = 0)")
        
        sql_params = []
        
        if artist_filter:
            sql_conditions.append("artist = ?")
            sql_params.append(artist_filter)
        
        if album_filter and artist_filter:
            sql_conditions.append("album = ?")
            sql_params.append(album_filter)
        
        sql = f"""
            SELECT id, artist, title, album, isrc, duration, spotify_album_type
            FROM tracks
            {('WHERE ' + ' AND '.join(sql_conditions)) if sql_conditions else ''}
            ORDER BY artist, album, title
        """
        
        log_verbose(f"Executing SQL: {sql.strip()} with params: {sql_params}")
        cursor.execute(sql, sql_params)

        tracks = cursor.fetchall()
        log_unified(f"Found {len(tracks)} tracks to scan for popularity")
        log_verbose(f"Fetched {len(tracks)} tracks from database.")

        if not tracks:
            log_unified("No tracks found for popularity scan. Exiting.")
            return

        # Group tracks by artist and album
        from collections import defaultdict
        artist_album_tracks = defaultdict(lambda: defaultdict(list))
        for track in tracks:
            artist_album_tracks[track["artist"]][track["album"]].append(track)

        # Handle resume logic
        resume_hit = False if resume_from else True
        if resume_from:
            log_unified(f"⏩ Resuming scan from artist: {resume_from}")
        
        scanned_count = 0
        skipped_count = 0
        for artist, albums in artist_album_tracks.items():
            # Skip until resume match
            if not resume_hit:
                if artist.lower() == resume_from.lower():
                    resume_hit = True
                    log_unified(f"🎯 Resuming from: {artist}")
                elif resume_from.lower() in artist.lower():
                    resume_hit = True
                    log_unified(f"🔍 Fuzzy resume match: {resume_from} → {artist}")
                else:
                    log_verbose(f"⏭ Skipping {artist} (before resume point)")
                    continue
            
            log_unified(f"Currently Scanning Artist: {artist}")
            
            # Get Spotify artist ID once per artist (before album loop)
            spotify_artist_id = None
            try:
                log_unified(f'Looking up Spotify artist ID for: {artist}')
                spotify_artist_id = _run_with_timeout(
                    get_spotify_artist_id, 
                    API_CALL_TIMEOUT, 
                    f"Spotify artist ID lookup timed out after {API_CALL_TIMEOUT}s",
                    artist
                )
                if spotify_artist_id:
                    log_unified(f'✓ Spotify artist ID cached: {spotify_artist_id}')
                    # Batch update all tracks for this artist with the artist ID
                    update_artist_id_for_artist(artist, spotify_artist_id)
                else:
                    log_unified(f'⚠ No Spotify artist ID found for: {artist}')
            except TimeoutError as e:
                log_unified(f"⏱ Spotify artist ID lookup timed out for {artist}: {e}")
            except Exception as e:
                log_unified(f"⚠ Spotify artist ID lookup failed for {artist}: {e}")
            
            for album, album_tracks in albums.items():
                # Check if album was already scanned (unless force rescan is enabled)
                if not (FORCE_RESCAN or force) and was_album_scanned(artist, album, 'popularity'):
                    log_unified(f'⏭ Skipping already-scanned album: "{artist} - {album}"')
                    skipped_count += 1
                    continue
                
                log_unified(f'Scanning "{artist} - {album}" for Popularity')
                album_scanned = 0
                
                # Batch updates for this album (commit once at end instead of per-track)
                track_updates = []
                
                # Cache Spotify search results for singles detection reuse
                spotify_results_cache = {}
                
                for track in album_tracks:
                    track_id = track["id"]
                    title = track["title"]

                    # Progress log every track
                    log_unified(f'Scanning track: "{title}" (Track ID: {track_id})')

                    # Try to get popularity from Spotify (using cached artist ID)
                    spotify_score = 0
                    spotify_search_results = None
                    try:
                        if spotify_artist_id:
                            log_unified(f'Searching Spotify for track: {title} by {artist}')
                            # For popularity scoring, we pass album for better matching accuracy
                            spotify_search_results = _run_with_timeout(
                                search_spotify_track,
                                API_CALL_TIMEOUT,
                                f"Spotify track search timed out after {API_CALL_TIMEOUT}s",
                                title, artist, album
                            )
                            # Cache results for singles detection reuse (using title as key)
                            spotify_results_cache[title] = spotify_search_results
                            
                            log_unified(f'Spotify search completed. Results count: {len(spotify_search_results) if spotify_search_results else 0}')
                            if spotify_search_results and isinstance(spotify_search_results, list) and len(spotify_search_results) > 0:
                                # Use strict matching if enabled, otherwise use standard highest popularity
                                if strict_spotify_matching:
                                    from helpers import select_best_spotify_match_strict
                                    # Get track metadata for strict matching
                                    track_duration_ms = None
                                    track_isrc = None
                                    if track.get("duration"):
                                        # Duration is stored in seconds, convert to milliseconds
                                        track_duration_ms = int(track["duration"] * 1000)
                                    if track.get("isrc"):
                                        track_isrc = track["isrc"]
                                    
                                    best_match = select_best_spotify_match_strict(
                                        spotify_search_results,
                                        title,
                                        track_duration_ms,
                                        track_isrc,
                                        duration_tolerance_sec
                                    )
                                    if best_match:
                                        log_unified(f'✓ Strict match found for: {title}')
                                    else:
                                        log_unified(f'⚠ No strict match found for: {title} (trying standard matching)')
                                        # Fallback to standard matching if no strict match
                                        best_match = max(spotify_search_results, key=lambda r: r.get('popularity', 0))
                                else:
                                    # Standard matching: highest popularity
                                    best_match = max(spotify_search_results, key=lambda r: r.get('popularity', 0))
                                
                                if best_match:
                                    spotify_score = best_match.get("popularity", 0)
                                    spotify_track_id = best_match.get("id")
                                    log_unified(f'Spotify popularity score: {spotify_score}')
                                else:
                                    spotify_score = 0
                                    spotify_track_id = None
                                    log_unified(f'No Spotify match found for: {title}')
                                
                                # Fetch comprehensive metadata for this track
                                if spotify_track_id:
                                    try:
                                        from popularity_helpers import fetch_comprehensive_metadata
                                        log_verbose(f"Fetching comprehensive metadata for track ID: {spotify_track_id}")
                                        metadata_fetched = fetch_comprehensive_metadata(
                                            db_track_id=track_id,
                                            spotify_track_id=spotify_track_id,
                                            force_refresh=force
                                        )
                                        if metadata_fetched:
                                            log_verbose(f"✓ Comprehensive metadata fetched for: {title}")
                                        else:
                                            log_verbose(f"⚠ Failed to fetch comprehensive metadata for: {title}")
                                    except Exception as e:
                                        log_verbose(f"⚠ Error fetching comprehensive metadata for {title}: {e}")
                            else:
                                log_unified(f'No Spotify results found for: {title}')
                        else:
                            log_unified(f'No Spotify artist ID found for: {artist}')
                    except TimeoutError as e:
                        # Log timeout errors explicitly
                        log_unified(f"⏱ Spotify lookup timed out for {artist} - {title}: {e}")
                        log_verbose(f"Timeout details: {str(e)}")
                        # Continue with next step even if Spotify times out
                    except KeyboardInterrupt:
                        # Allow user to interrupt the scan
                        raise
                    except Exception as e:
                        # Catch all exceptions to prevent scanner from hanging
                        log_unified(f"⚠ Spotify lookup failed for {artist} - {title}: {e}")
                        log_verbose(f"Spotify error details: {type(e).__name__}: {str(e)}")
                        if VERBOSE:
                            import traceback
                            logging.error(f"Exception traceback: {traceback.format_exc()}")
                        # Continue with next step even if Spotify fails

                    # Try to get popularity from Last.fm
                    lastfm_score = 0
                    try:
                        log_unified(f'Getting Last.fm info for: {title} by {artist}')
                        lastfm_info = _run_with_timeout(
                            get_lastfm_track_info,
                            API_CALL_TIMEOUT,
                            f"Last.fm lookup timed out after {API_CALL_TIMEOUT}s",
                            artist, title
                        )
                        log_unified(f'Last.fm lookup completed. Result: {lastfm_info}')
                        if lastfm_info and lastfm_info.get("track_play"):
                            lastfm_score = min(100, int(lastfm_info["track_play"]) // 100)
                            log_unified(f'Last.fm play count: {lastfm_info.get("track_play")} (score: {lastfm_score})')
                        else:
                            log_unified(f'No Last.fm play count found for: {title}')
                    except TimeoutError as e:
                        # Log timeout errors explicitly
                        log_unified(f"⏱ Last.fm lookup timed out for {artist} - {title}: {e}")
                        log_verbose(f"Timeout details: {str(e)}")
                        # Continue with next step even if Last.fm times out
                    except KeyboardInterrupt:
                        # Allow user to interrupt the scan
                        raise
                    except Exception as e:
                        # Catch all exceptions to prevent scanner from hanging
                        log_unified(f"⚠ Last.fm lookup failed for {artist} - {title}: {e}")
                        log_verbose(f"Last.fm error details: {type(e).__name__}: {str(e)}")
                        # Continue with next step even if Last.fm fails

                    # Calculate popularity score and queue for batch update
                    if spotify_score > 0 or lastfm_score > 0:
                        popularity_score = (spotify_score + lastfm_score) / 2.0
                        track_updates.append((popularity_score, track_id))
                        scanned_count += 1
                        album_scanned += 1
                        log_unified(f'✓ Track scanned successfully: "{title}" (score: {popularity_score:.1f})')
                    else:
                        log_unified(f"⚠ No popularity score found for {artist} - {title}")

                    # Save progress after each track
                    save_popularity_progress(scanned_count, len(tracks))

                # Batch update all popularity scores for this album in one commit
                if track_updates:
                    cursor.executemany(
                        "UPDATE tracks SET popularity_score = ? WHERE id = ?",
                        track_updates
                    )
                    conn.commit()
                    log_verbose(f"Batch committed {len(track_updates)} popularity scores for album '{album}'")

                log_unified(f'Album Scanned: "{artist} - {album}". Popularity Applied to {album_scanned} tracks.')

                # Perform singles detection for album tracks
                log_unified(f'Detecting singles for "{artist} - {album}"')
                singles_detected = 0
                
                # Load Discogs token from config.yaml if not in environment
                discogs_token = os.environ.get("DISCOGS_TOKEN", "")
                if not discogs_token:
                    try:
                        config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")
                        with open(config_path, 'r') as f:
                            config = yaml.safe_load(f)
                        discogs_token = config.get("api_integrations", {}).get("discogs", {}).get("token", "")
                        if discogs_token:
                            log_verbose(f"   ✓ Loaded Discogs token from config.yaml")
                    except Exception as e:
                        log_verbose(f"   ⚠ Could not load Discogs token from config: {e}")
                
                # Log which sources are available for single detection
                sources_available = []
                sources_available.append("Spotify")
                if HAVE_MUSICBRAINZ:
                    sources_available.append("MusicBrainz")
                if HAVE_DISCOGS and discogs_token:
                    sources_available.append("Discogs")
                if HAVE_DISCOGS_VIDEO and discogs_token:
                    sources_available.append("Discogs Video")
                log_unified(f'   Using sources: {", ".join(sources_available)}')
                
                # Batch updates for singles detection
                singles_updates = []
                
                # Get album track count for context-based confidence adjustment
                album_track_count = len(album_tracks)
                
                for track in album_tracks:
                    track_id = track["id"]
                    title = track["title"]
                    
                    # Get additional fields for advanced detection
                    track_isrc = track["isrc"] if track["isrc"] else None
                    track_duration = track["duration"] if track["duration"] else None
                    track_album_type = track["spotify_album_type"] if track["spotify_album_type"] else None
                    
                    # Get the popularity score for this track (may have been calculated earlier)
                    track_popularity = 0.0
                    cursor.execute("SELECT popularity_score FROM tracks WHERE id = ?", (track_id,))
                    pop_row = cursor.fetchone()
                    if pop_row and pop_row[0]:
                        track_popularity = pop_row[0]
                    
                    # Use the centralized single detection function with advanced parameters
                    detection_result = detect_single_for_track(
                        title=title,
                        artist=artist,
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
                        zscore_threshold=0.20
                    )
                    
                    single_sources = detection_result["sources"]
                    single_confidence = detection_result["confidence"]
                    is_single = detection_result["is_single"]
                    
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
                            source_str = ", ".join(single_sources)
                            log_unified(f"   ✓ Single detected: {title} ({single_confidence} confidence, sources: {source_str})")
                
                # Batch update all singles detection results for this album in one commit
                if singles_updates:
                    cursor.executemany(
                        """UPDATE tracks 
                        SET is_single = ?, single_confidence = ?, single_sources = ?
                        WHERE id = ?""",
                        singles_updates
                    )
                    conn.commit()
                    log_verbose(f"Batch committed {len(singles_updates)} singles detection results for album '{album}'")
                
                # Log summary of singles detection
                high_conf_count = sum(1 for update in singles_updates if update[0] == 1)
                log_unified(f'Singles Detection Complete: {singles_detected} high-confidence single(s) detected for "{artist} - {album}" ({len(singles_updates)} tracks checked)')

                # Calculate star ratings for album tracks
                log_unified(f'Calculating star ratings for "{artist} - {album}"')
                
                # Get all tracks for this album with their popularity scores and single detection
                cursor.execute(
                    "SELECT id, title, popularity_score, is_single, single_confidence FROM tracks WHERE artist = ? AND album = ? ORDER BY popularity_score DESC",
                    (artist, album)
                )
                album_tracks_with_scores = cursor.fetchall()
                
                if album_tracks_with_scores and len(album_tracks_with_scores) > 0:
                    # Calculate star ratings using the same logic as sptnr.py
                    total_tracks = len(album_tracks_with_scores)
                    band_size = math.ceil(total_tracks / 4)
                    
                    # Calculate median score for threshold
                    scores = [t["popularity_score"] if t["popularity_score"] else 0 for t in album_tracks_with_scores]
                    median_score = median(scores) if scores else 10
                    if median_score == 0:
                        median_score = 10
                    jump_threshold = median_score * 1.7
                    
                    star_distribution = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
                    
                    # Batch updates for better performance
                    updates = []
                    
                    for i, track_row in enumerate(album_tracks_with_scores):
                        track_id = track_row["id"]
                        title = track_row["title"]
                        popularity_score = track_row["popularity_score"] if track_row["popularity_score"] else 0
                        is_single = track_row["is_single"] if track_row["is_single"] else 0
                        single_confidence = track_row["single_confidence"] if track_row["single_confidence"] else "low"
                        
                        # Calculate band-based star rating
                        band_index = i // band_size
                        stars = max(1, 4 - band_index)
                        
                        # Boost to 5 stars if score exceeds threshold (only for singles)
                        # Per requirement: no song should be rated 5 stars unless it's a high confidence single
                        # or a 4 star medium confidence single and gets a 1* bump
                        if popularity_score >= jump_threshold:
                            # Only boost to 5 if it's at least a medium confidence single
                            if single_confidence in ["high", "medium"]:
                                stars = 5
                            else:
                                stars = 4  # Cap at 4 stars if not a single
                        
                        # Boost stars for confirmed singles
                        if single_confidence == "high":
                            stars = 5  # High confidence single = 5 stars
                        elif single_confidence == "medium":
                            stars = min(stars + 1, 5)  # Boost by 1 star for medium confidence
                        
                        # Ensure at least 1 star
                        stars = max(stars, 1)
                        
                        # Collect update for batch processing
                        updates.append((stars, track_id))
                        
                        star_distribution[stars] += 1
                        
                        # Log track with star rating
                        single_tag = " (Single)" if is_single else ""
                        star_display = "★" * stars + "☆" * (5 - stars)
                        log_unified(f"   {star_display} ({stars}/5) - {title}{single_tag} (popularity: {popularity_score:.1f})")
                    
                    # Batch update all tracks at once for better performance
                    cursor.executemany(
                        """UPDATE tracks SET stars = ? WHERE id = ?""",
                        updates
                    )
                    conn.commit()
                    
                    # Sync to Navidrome after batch update
                    for stars, track_id in updates:
                        if sync_track_rating_to_navidrome(track_id, stars):
                            log_verbose(f"      ✓ Synced track {track_id} to Navidrome")
                        else:
                            log_verbose(f"      ⚠ Skipped Navidrome sync for track {track_id}")
                    
                    # Log star distribution
                    dist_str = ", ".join([f"{stars}★: {count}" for stars, count in sorted(star_distribution.items(), reverse=True) if count > 0])
                    log_unified(f'Star distribution for "{album}": {dist_str}')
                
                # Update last_scanned timestamp for all tracks in this album
                current_timestamp = datetime.now().isoformat()
                cursor.execute(
                    """UPDATE tracks SET last_scanned = ? WHERE artist = ? AND album = ?""",
                    (current_timestamp, artist, album)
                )
                
                # Ensure changes are committed before logging to scan_history to avoid database lock conflicts
                conn.commit()
                
                # Log album scan
                log_album_scan(artist, album, 'popularity', album_scanned, 'completed')

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
                create_or_update_playlist_for_artist(artist, tracks_list)

        log_verbose("Committing changes to database.")
        conn.commit()

        log_unified(f"✅ Popularity scan completed: {scanned_count} tracks updated, {skipped_count} albums skipped (already scanned)")
        log_verbose(f"Popularity scan completed: {scanned_count} tracks updated, {skipped_count} albums skipped")
    except Exception as e:
        log_unified(f"❌ Popularity scan failed: {str(e)}")
        raise
    finally:
        if conn:
            conn.close()
        log_unified("=" * 60)
        log_unified(f"✅ Popularity scan complete at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

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
            log_basic(f"🗑️ Deleted playlist: {playlist_name}")
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
        
        log_basic(f"📝 NSP created/updated: {file_path}")
        return True
    except Exception as e:
        log_basic(f"Failed to create playlist '{playlist_name}': {e}")
        return False


def create_or_update_playlist_for_artist(artist_name: str, tracks: list):
    """
    Create/refresh 'Essential {artist}' smart playlist using Navidrome's 0–5 rating scale.

    Logic:
      - Case A: if artist has >= 10 five-star tracks, build a pure 5★ essentials playlist.
      - Case B: if total tracks >= 100, build top 10% essentials sorted by rating.
    
    Args:
        artist_name: Name of the artist
        tracks: List of track dictionaries with id, artist, album, title, stars
    """
    total_tracks = len(tracks)
    five_star_tracks = [t for t in tracks if (t.get("stars") or 0) == 5]
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
    log_basic("🔄 Refreshing smart playlists for all artists from DB cache (no track rescans)...")
    
    # Pull distinct artists that have cached tracks
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT artist FROM tracks")
        artists = [row[0] for row in cursor.fetchall()]
        
        if not artists:
            log_basic("⚠️ No cached tracks in DB. Skipping playlist refresh.")
            return
        
        for name in artists:
            cursor.execute("SELECT id, artist, album, title, stars FROM tracks WHERE artist = ?", (name,))
            rows = cursor.fetchall()
            
            if not rows:
                log_basic(f"⚠️ No cached tracks found for '{name}', skipping.")
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
            log_basic(f"✅ Playlist refreshed for '{name}' ({len(tracks)} tracks)")
    except Exception as e:
        log_basic(f"❌ Error refreshing playlists: {e}")
    finally:
        if conn:
            conn.close()

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Run popularity scan.")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose output")
    parser.add_argument("--force", action="store_true", help="Force re-scan of all albums")
    args = parser.parse_args()
    popularity_scan(verbose=args.verbose, force=args.force)
