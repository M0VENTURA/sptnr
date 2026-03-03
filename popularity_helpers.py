#!/usr/bin/env python3
"""
Shared popularity helpers for Spotify/Last.fm/ListenBrainz lookups and weights.
Functions are used by both the main scanner (start.py) and popularity.py.
"""

import os
import yaml
import math
import logging
import json
import time
import difflib
from contextlib import contextmanager
from typing import Any, Tuple, List, Dict
from datetime import datetime
from collections import defaultdict
from statistics import mean, stdev, median

from api_clients.spotify import SpotifyClient
from api_clients.lastfm import LastFmClient
from api_clients.audiodb_and_listenbrainz import score_by_age as _score_by_age
from api_clients import timeout_safe_session
from helpers.helpers import strip_cover_attribution

# ============================================================================
# Shared z-score and popularity utilities (consolidated from duplicated code)
# ============================================================================

# Constants for z-score to popularity conversion
Z_SCORE_MIDPOINT = 50.0
Z_SCORE_TO_POPULARITY_SCALE = 16.7


def calculate_track_zscore(score: float, mean: float, stddev: float) -> float:
    """
    Calculate z-score for a track relative to a reference distribution.
    Z-score = (score - mean) / stddev
    """
    if stddev and stddev > 0:
        return (score - mean) / stddev
    return 0.0


def zscore_to_popularity(z_score: float) -> float:
    """
    Convert z-score to 0-100 popularity scale.
    Formula: 50 + (z_score * 16.7)
    """
    score = Z_SCORE_MIDPOINT + (z_score * Z_SCORE_TO_POPULARITY_SCALE)
    return min(100.0, max(0.0, score))


# Context manager for safe database connection handling (replaces boilerplate try/finally)
@contextmanager
def get_db_connection_context(conn=None):
    """
    Context manager for safe database connection handling.
    Automatically closes connections that were created by this manager.
    """
    should_close = conn is None
    
    if should_close:
        try:
            from helpers.db_utils import get_db_connection
            conn = get_db_connection()
        except Exception as e:
            logging.error(f"Failed to get database connection: {e}")
            raise
    
    try:
        yield conn
    finally:
        if should_close and conn:
            try:
                conn.close()
            except Exception as e:
                logging.warning(f"Error closing database connection: {e}")

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/config.yaml")

_DEFAULT_WEIGHTS = {
    "spotify": 0.10,      # Minimal: Algorithm-driven, paid API, increasingly gamed
    "lastfm": 0.30,       # Community choice: genuine scrobbles since 2002 (established metric)
    "listenbrainz": 0.35, # Community choice: open-source, not influenced by artist payola (most authentic)
    "age": 0.25,          # Recency and track maturity (slightly reduced for community primacy)
}

_DEFAULT_FEATURES = {
    "scan_worker_threads": 4,
}

_spotify_client: SpotifyClient | None = None
_lastfm_client: LastFmClient | None = None

_spotify_enabled = True
_clients_configured = False


def _load_config() -> dict:
    config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")
    if not os.path.exists(config_path):
        return {}
    try:
        with open(config_path, "r") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _resolve_weights(cfg: dict) -> Tuple[float, float, float, float]:
    """Resolve popularity weights from config (supports 4 sources: Spotify, Last.fm, ListenBrainz, Age)."""
    weights = cfg.get("weights") if isinstance(cfg, dict) else None
    weights = weights or {}
    return (
        float(weights.get("spotify", _DEFAULT_WEIGHTS["spotify"])),
        float(weights.get("lastfm", _DEFAULT_WEIGHTS["lastfm"])),
        float(weights.get("listenbrainz", _DEFAULT_WEIGHTS["listenbrainz"])),
        float(weights.get("age", _DEFAULT_WEIGHTS["age"])),
    )


SPOTIFY_WEIGHT, LASTFM_WEIGHT, LISTENBRAINZ_WEIGHT, AGE_WEIGHT = _resolve_weights(_load_config())


def _worker_threads(cfg: dict) -> int:
    features = cfg.get("features") if isinstance(cfg, dict) else None
    features = features or {}
    try:
        return int(features.get("scan_worker_threads", _DEFAULT_FEATURES["scan_worker_threads"]))
    except Exception:
        return _DEFAULT_FEATURES["scan_worker_threads"]


def configure_popularity_helpers(
    *,
    spotify_client: SpotifyClient | None = None,
    lastfm_client: LastFmClient | None = None,
    config: dict | None = None,
) -> None:
    """Configure shared clients and refresh weights based on provided config."""
    global _spotify_client, _lastfm_client
    global _spotify_enabled, _clients_configured
    global SPOTIFY_WEIGHT, LASTFM_WEIGHT, LISTENBRAINZ_WEIGHT, AGE_WEIGHT

    cfg = config if config is not None else _load_config()

    # Refresh weights from config
    SPOTIFY_WEIGHT, LASTFM_WEIGHT, LISTENBRAINZ_WEIGHT, AGE_WEIGHT = _resolve_weights(cfg)

    api_cfg = cfg.get("api_integrations") if isinstance(cfg, dict) else None
    api_cfg = api_cfg or {}

    spotify_cfg = api_cfg.get("spotify") or {}
    _spotify_enabled = bool(spotify_cfg.get("enabled", True))
    if spotify_client is not None:
        _spotify_client = spotify_client
    elif _spotify_enabled:
        _spotify_client = SpotifyClient(
            spotify_cfg.get("client_id", ""),
            spotify_cfg.get("client_secret", ""),
            http_session=timeout_safe_session,
            worker_threads=_worker_threads(cfg),
        )
    else:
        _spotify_client = None

    lastfm_cfg = api_cfg.get("lastfm") or {}
    if lastfm_client is not None:
        _lastfm_client = lastfm_client
    else:
        _lastfm_client = LastFmClient(lastfm_cfg.get("api_key", ""), http_session=timeout_safe_session)

    _clients_configured = True


def _ensure_clients_from_config() -> None:
    if not _clients_configured:
        configure_popularity_helpers()


def get_spotify_artist_id(artist_name: str) -> str | None:
    """
    Get Spotify artist ID with database caching.
    First checks the database for a cached ID, then queries Spotify API if needed.
    
    Args:
        artist_name: Artist name to lookup
        
    Returns:
        Spotify artist ID or None
    """
    _ensure_clients_from_config()
    if not _spotify_enabled or _spotify_client is None:
        return None
    
    # First, try to get from database cache
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT spotify_artist_id FROM tracks WHERE artist = ? AND spotify_artist_id IS NOT NULL LIMIT 1",
            (artist_name,)
        )
        row = cursor.fetchone()
        conn.close()
        
        if row and row[0]:
            logging.info(f"✓ Using cached Spotify artist ID for '{artist_name}': {row[0]}")
            return row[0]
    except Exception as e:
        logging.debug(f"Failed to lookup cached Spotify artist ID for '{artist_name}': {e}")
    
    # If not in database, query Spotify API
    logging.info(f"Querying Spotify API for artist ID: '{artist_name}'")
    return _spotify_client.get_artist_id(artist_name)


def get_spotify_artist_single_track_ids(artist_id: str) -> set[str]:
    _ensure_clients_from_config()
    if not _spotify_enabled or _spotify_client is None:
        return set()
    return _spotify_client.get_artist_singles(artist_id) or set()


def normalize_title_for_lastfm(title: str) -> str:
    """
    Normalize titles for Last.fm API searches by standardizing special characters.
    
    Removes or converts:
    - All apostrophe variants (curly, straight, backtick) → removed
    - Smart/curly double quotes → removed
    - Angle quotes (guillemets) → removed
    - Dashes and hyphens → normalized to regular hyphen
    - Prime marks → converted then removed
    - Ellipsis → converted to three dots
    - Multiple spaces → single space
    
    This ensures "Still Swingin'" matches Last.fm's "Still Swingin" database entry.
    Many music databases have inconsistent punctuation handling.
    
    Examples:
    - "Where Did the Angels Go?" → "Where Did the Angels Go"
    - '"Love" Song' → "Love Song"
    - "Word—dash" → "Word-dash"
    - "Fade…away" → "Fade...away"
    
    Args:
        title: Track title to normalize
        
    Returns:
        Normalized title string
    """
    if not title:
        return title
    
    import re
    
    # Debug: detect and log character codes for problematic punctuation
    if any(c in title for c in "'?!''\"\"«»–—−′″…¿¡"):
        problem_chars = {c: ord(c) for c in title if c in "'?!''\"\"«»–—−′″…¿¡"}
        logging.debug(f"normalize_title_for_lastfm input '{title}': Found special chars: {problem_chars}")
    
    # === AGGRESSIVE APOSTROPHE REMOVAL (regex-based) ===
    # Match any character that could be an apostrophe/quote (including Unicode variants)
    # This catches characters we might not have explicitly listed
    title = re.sub(r"[\u2018\u2019\u0060\u0027\u2032\u2033]", '', title)  # Remove apostrophe/prime variants by Unicode code point
    
    # === SMART/CURLY QUOTE REMOVAL ===
    # " (U+201D right double quotation mark)
    # " (U+201C left double quotation mark)
    # « (U+00AB left-pointing double angle quotation mark)
    # » (U+00BB right-pointing double angle quotation mark)
    title = title.replace('"', '')  # curly right double
    title = title.replace('"', '')  # curly left double
    title = title.replace('«', '')  # left angle quote
    title = title.replace('»', '')  # right angle quote
    
    # === DASH/HYPHEN NORMALIZATION (convert to regular hyphen) ===
    # – (U+2013 en dash)
    # — (U+2014 em dash)
    # − (U+2212 minus sign)
    title = title.replace('–', '-')  # en dash
    title = title.replace('—', '-')  # em dash
    title = title.replace('−', '-')  # minus sign
    
    # === PRIME MARKS (remove directly, not convert) ===
    # ′ (U+2032 prime) - already handled by regex above
    # ″ (U+2033 double prime) - already handled by regex above
    # No need to convert - the regex handled removal
    
    # === ELLIPSIS (convert to three dots) ===
    # … (U+2026 horizontal ellipsis)
    title = title.replace('…', '...')
    
    # === QUESTION MARKS (convert smart variants to regular, then remove trailing) ===
    title = title.replace('¿', '?')  # ¿ (U+00BF inverted question - Spanish)
    title = title.rstrip('?')  # Remove trailing question marks
    
    # === EXCLAMATION MARKS (convert smart variants to regular, then remove trailing) ===
    title = title.replace('¡', '!')  # ¡ (U+00A1 inverted exclamation - Spanish)
    title = title.rstrip('!')  # Remove trailing exclamation marks
    
    # === MULTIPLE SPACES (collapse to single space) ===
    title = re.sub(r'\s+', ' ', title).strip()
    
    return title


def search_spotify_track(title: str, artist: str, album: str | None = None):
    _ensure_clients_from_config()
    if not _spotify_enabled or _spotify_client is None:
        return []
    normalized_title = normalize_title_for_lastfm(strip_cover_attribution(title))
    return _spotify_client.search_track(normalized_title, artist, album)


def get_lastfm_track_info(artist: str, title: str) -> dict:
    _ensure_clients_from_config()
    if _lastfm_client is None:
        return {"track_play": 0}
    stripped_title = strip_cover_attribution(title)
    normalized_title = normalize_title_for_lastfm(stripped_title)
    
    # Debug: Show character codes for titles with apostrophes or punctuation
    if "'" in stripped_title or "'" in stripped_title or "?" in stripped_title or "!" in stripped_title or "'" in stripped_title or "'" in stripped_title:
        stripped_codes = [ord(c) for c in stripped_title if c in "'?!'']"]
        normalized_codes = [ord(c) for c in normalized_title if c in "'?!'']"]
        logging.debug(f"Title chars - original: {stripped_codes}, normalized: {normalized_codes}")
        logging.debug(f"Title normalization: '{stripped_title}' → '{normalized_title}'")
    elif stripped_title != normalized_title:
        logging.debug(f"Title normalization: '{stripped_title}' → '{normalized_title}'")
    
    # Try exact match first
    result = _lastfm_client.get_track_info(artist, normalized_title)
    
    # If exact match failed (no listeners/playcount), try fuzzy matching
    if result.get("listeners", 0) == 0 and result.get("track_play", 0) == 0:
        logging.debug(f"Exact match failed for '{normalized_title}' by '{artist}', trying fuzzy search...")
        
        # Search for tracks by same artist
        search_results = _lastfm_client.search_track(artist, normalized_title, limit=10)
        
        if search_results:
            # Find best match using fuzzy string matching
            best_match = None
            best_ratio = 0.0
            
            for track in search_results:
                track_name = track.get("name", "")
                track_normalized = normalize_title_for_lastfm(track_name)
                
                # Calculate similarity ratio
                ratio = difflib.SequenceMatcher(None, normalized_title.lower(), track_normalized.lower()).ratio()
                
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_match = track_name
            
            # Accept fuzzy match if similarity > 0.85 (same threshold as Discogs verification)
            if best_ratio > 0.85 and best_match:
                logging.info(f"🔍 Fuzzy matched '{title}' → '{best_match}' by '{artist}' (similarity: {best_ratio:.2f})")
                
                # Fetch track info using the matched title
                result = _lastfm_client.get_track_info(artist, best_match)
            else:
                logging.debug(f"No fuzzy match above threshold (best: {best_ratio:.2f}) for '{title}' by '{artist}'")
    
    return result


def calculate_lastfm_popularity_score(listeners: int, artist_max_listeners: int = 0) -> float:
    """
    Calculate a normalized Last.fm popularity score (0-100) from listener count.
    
    Uses logarithmic normalization. Listeners are typically 10-100x smaller than playcount,
    so scaling is adjusted accordingly.
    
    Algorithm:
    1. If artist_max_listeners is provided, normalize relative to artist (0-100 scale)
    2. Otherwise, use global logarithmic scale:
       - log10(100) = 2.0 → 25 points
       - log10(1000) = 3.0 → 37.5 points
       - log10(10000) = 4.0 → 50 points
       - log10(100000) = 5.0 → 62.5 points
       - log10(1000000) = 6.0 → 75 points
       - log10(10000000) = 7.0 → 87.5 points
       
    Args:
        listeners: Last.fm unique listener count for the track
        artist_max_listeners: Optional maximum listener count for the artist (for artist-relative scoring)
        
    Returns:
        Popularity score (0-100)
    """
    if listeners <= 0:
        return 0.0
    
    # Artist-relative scoring (preferred when available)
    if artist_max_listeners > 0:
        # Linear scale relative to artist's most popular track
        # Cap at 100 if track exceeds artist max (shouldn't happen in practice)
        return min(100.0, (listeners / artist_max_listeners) * 100.0)
    
    # Global logarithmic scaling
    # Use log base 10, scaled to 0-100 range
    # Formula: score = 12.5 * log10(listeners)
    # This gives:
    #   10 listeners    → 12.5 points
    #   100 listeners   → 25 points
    #   1,000 listeners → 37.5 points
    #   10,000 listeners → 50 points
    #   100,000 listeners → 62.5 points
    #   1,000,000 listeners → 75 points
    score = 12.5 * math.log10(listeners)
    
    # Cap at 100
    return min(100.0, max(0.0, score))


def calculate_lastfm_zscore_popularity(
    listeners: int,
    playcount: int,
    album_listeners: List[int],
    album_playcounts: List[int]
) -> float:
    """
    Calculate Last.fm popularity score using z-score normalization that combines
    both listeners and scrobbles (playcount), normalized by album.
    
    This method is more robust than single-metric scoring as it:
    - Uses both unique listeners (reach) and play count (engagement)
    - Normalizes within album context (z-scores using median) to account for album-level popularity
    - Combines metrics to balance reach vs. engagement
    
    Algorithm:
    1. Calculate z-score for listeners within album: (listeners - median_listeners) / stdev_listeners
    2. Calculate z-score for playcount within album: (playcount - median_playcount) / stdev_playcount  
    3. Average the two z-scores: (z_listeners + z_playcount) / 2
    4. Convert averaged z-score to 0-100 scale
    
    Args:
        listeners: Track's unique listener count on Last.fm
        playcount: Track's scrobble count on Last.fm
        album_listeners: List of all track listener counts in the album
        album_playcounts: List of all track scrobble counts in the album
        
    Returns:
        Popularity score (0-100) normalized to album context
    """
    if listeners <= 0 or playcount <= 0:
        return 0.0
    
    # If we have fewer than 2 tracks, fall back to simple logarithmic scoring
    # This handles the case where z-score calculation is attempted before all album tracks are fetched
    if len(album_listeners) < 2 or len(album_playcounts) < 2:
        # Use simple logarithmic scoring as fallback
        return calculate_lastfm_popularity_score(listeners)
    
    try:
        # Calculate z-scores for listeners (using median for centering)
        listeners_median = median(album_listeners)
        listeners_stdev = stdev(album_listeners)
        z_listeners = calculate_track_zscore(listeners, listeners_median, listeners_stdev)
        
        # Calculate z-scores for playcounts (using median for centering)
        playcount_median = median(album_playcounts)
        playcount_stdev = stdev(album_playcounts)
        z_playcount = calculate_track_zscore(playcount, playcount_median, playcount_stdev)
        
        # Average the two z-scores (equal weight for reach and engagement)
        average_zscore = (z_listeners + z_playcount) / 2.0
        
        # Convert z-score to 0-100 scale
        score = zscore_to_popularity(average_zscore)
        return score
        
    except (ValueError, ZeroDivisionError):
        return 0.0


# Re-export score_by_age from api_clients for backward compatibility
score_by_age = _score_by_age


def apply_mean_popularity_adjustment(
    track_popularity: float,
    artist_name: str,
    release_year: int | None = None,
    conn = None
) -> float:
    """
    Apply median+MAD-based popularity adjustment with optional time decay for pre-2005 releases.
    
    Algorithm:
    1. Calculate track z-score relative to artist median: (track_pop - artist_median) / max(artist_MAD, MIN_SPREAD)
    2. Apply time decay for releases before 2005 (account for sparse Last.fm data pre-2005)
    3. Convert z-score to 0-100 scale using formula: 50 + (z_score * 16.7)
    
    Why median+MAD instead of mean+stddev:
    - Median is robust to outliers and skewed distributions
    - MAD (Median Absolute Deviation) is less sensitive to extreme values
    - MIN_SPREAD floor prevents flat albums from over-amplifying small differences
    - Better handles artists with varied catalog quality (e.g., hits + deep cuts)
    
    Rationale:
    - Avoids algorithmic bias (Spotify weighting issue)
    - Artist context improves accuracy (top 5% of artist > absolute score)
    - Time adjustment acknowledges data sparsity pre-2005
    - Z-score threshold of 1.0 aligns with "popular for this artist" classification
    
    Args:
        track_popularity: Current popularity score (0-100, weighted average)
        artist_name: Artist name for context lookup
        release_year: Optional year to apply time decay (pre-2005 reduces confidence)
        conn: Optional database connection to fetch artist stats
        
    Returns:
        Adjusted popularity score (0-100)
    """
    if track_popularity <= 0:
        return 0.0
    
    # MIN_SPREAD floor to prevent flat-album over-amplification
    MIN_SPREAD = 10.0
    
    with get_db_connection_context(conn) as db_conn:
        try:
            cursor = db_conn.cursor()
            
            # Fetch artist statistics (median, MAD)
            cursor.execute("""
                SELECT median_popularity, popularity_mad
                FROM artist_stats
                WHERE artist_name = ?
            """, (artist_name,))
            
            row = cursor.fetchone()
            if not row:
                # Artist stats not yet computed, return original score
                return track_popularity
            
            artist_median, artist_mad = row[0], row[1]
            
            if artist_median is None or artist_median <= 0:
                # No valid median, return original score
                return track_popularity
            
            # Apply MIN_SPREAD floor to prevent flat-album noise amplification
            # For flat albums (low MAD), MIN_SPREAD prevents tiny differences
            # from being turned into large z-scores
            artist_spread = max(artist_mad if artist_mad else 0, MIN_SPREAD)
            
            # Calculate z-score relative to artist median
            # Note: calculate_track_zscore expects (score, center, spread)
            # We're now passing median as center and MAD (with floor) as spread
            if artist_spread > 0:
                z_score = (track_popularity - artist_median) / artist_spread
            else:
                z_score = 0
            
            # Apply time decay for pre-2005 releases
            # Pre-2005: Last.fm had fewer active users, resulting in sparse/incomplete data
            # Reduce confidence by scaling down the z-score
            # Linear decay: 2005 = 1.0x, 2000 = 0.8x, 1995 = 0.6x, 1990 = 0.4x, pre-1990 = 0.2x
            if release_year and release_year < 2005:
                years_before_2005 = 2005 - release_year
                # Decay formula: 1.0 - (years_before * 0.04) with floor at 0.2
                # This gives ~4% reduction per year pre-2005
                decay_factor = max(0.2, 1.0 - (years_before_2005 * 0.04))
                z_score *= decay_factor
                logging.debug(f"Applied time decay to '{artist_name}' release ({release_year}): z_score {(track_popularity - artist_median) / artist_spread if artist_spread > 0 else 0:.2f} -> {z_score:.2f} (decay_factor={decay_factor:.2f})")
            
            # Convert z-score to 0-100 scale
            adjusted_score = zscore_to_popularity(z_score)
            
            logging.debug(f"Median+MAD popularity adjustment for '{artist_name}': original={track_popularity:.1f}, z_score={z_score:.2f}, adjusted={adjusted_score:.1f} (artist_median={artist_median:.1f}, MAD={artist_mad:.1f}, spread={artist_spread:.1f})")
            
            return adjusted_score
            
        except Exception as e:
            logging.debug(f"Error applying median+MAD popularity adjustment for '{artist_name}': {e}")
            return track_popularity


def apply_album_deviation_adjustment(
    track_popularity: float,
    artist_name: str,
    album_name: str,
    artist_mean_popularity: float | None = None,
    conn = None
) -> float:
    """
    Apply album-level z-score deviation adjustment for tracks in lower-popularity albums.
    
    This function refines popularity scores by considering the track's position within
    its album's popularity distribution. It's especially useful for identifying standout
    tracks in niche or lower-popularity albums.
    
    Algorithm:
    1. Fetch all popularities for tracks in the album
    2. Calculate album mean and stddev
    3. Calculate track z-score within album: (track_pop - album_mean) / album_stddev
    4. Determine weight factor based on album popularity tier
    5. Blend with original score: (original * (1 - weight)) + (album_zscore_converted * weight)
    
    Weight tiers:
    - Low popularity albums (mean < 40): 40% album weight (identify gems in niche catalogs)
    - Mid-tier albums (40-60): 30% album weight (balance artist + album context)
    - High popularity albums (> 60): 15% album weight (artist consistency dominates)
    
    Rationale:
    - Single-track albums: No adjustment (stddev = 0)
    - Compilations with mixed artists: Skip (requires artist filtering)
    - Sparse albums (2-3 tracks): Still apply but with caution (limited variance data)
    
    Args:
        track_popularity: Current popularity score (0-100)
        artist_name: Artist name for context
        album_name: Album name
        artist_mean_popularity: Optional artist mean (for efficiency if already calculated)
        conn: Optional database connection
        
    Returns:
        Adjusted popularity score (0-100)
    """
    if track_popularity <= 0:
        return track_popularity
    
    with get_db_connection_context(conn) as db_conn:
        try:
            cursor = db_conn.cursor()
            
            # Fetch all track popularities in this album
            cursor.execute("""
                SELECT popularity
                FROM tracks
                WHERE artist = ? AND album = ? AND popularity > 0
                ORDER BY popularity
            """, (artist_name, album_name))
            
            rows = cursor.fetchall()
            if not rows or len(rows) < 2:
                # Skip adjustment if album has fewer than 2 tracks with popularity data
                return track_popularity
            
            album_popularities = [row[0] for row in rows]
            
            # Calculate album statistics
            try:
                album_mean = mean(album_popularities)
                if len(album_popularities) < 2:
                    album_stddev = 0.0
                else:
                    album_stddev = stdev(album_popularities)
            except (ValueError, ZeroDivisionError):
                return track_popularity
            
            # Skip if no variance in album
            if album_stddev == 0:
                return track_popularity
            
            # Calculate track z-score within album
            album_zscore = calculate_track_zscore(track_popularity, album_mean, album_stddev)
            
            # Determine weight factor based on album popularity tier
            if album_mean < 40:
                # Low popularity album: higher weight on album deviation
                album_weight = 0.40
            elif album_mean < 60:
                # Mid-tier album
                album_weight = 0.30
            else:
                # High popularity album: lower weight on album deviation
                album_weight = 0.15
            
            # Convert album z-score to 0-100 scale
            album_zscore_pop = zscore_to_popularity(album_zscore)
            
            # Blend with original score
            adjusted_score = (track_popularity * (1.0 - album_weight)) + (album_zscore_pop * album_weight)
            
            logging.debug(
                f"Album deviation adjustment for '{artist_name}' - '{album_name}': "
                f"original={track_popularity:.1f}, album_mean={album_mean:.1f}, album_stddev={album_stddev:.2f}, "
                f"album_zscore={album_zscore:.2f}, weight={album_weight:.0%}, adjusted={adjusted_score:.1f}"
            )
            
            return adjusted_score
            
        except Exception as e:
            logging.debug(f"Error applying album deviation adjustment for '{artist_name}' - '{album_name}': {e}")
            return track_popularity


# --- Shared DB/API/Helper Functions (moved from start.py) ---
from helpers.db_utils import get_db_connection

# Cache for NavidromeClient instance
_nav_client_cache = None

def _get_nav_client():
    """Get or create NavidromeClient instance with caching."""
    global _nav_client_cache
    
    # Return cached client if available
    if _nav_client_cache is not None:
        return _nav_client_cache
    
    try:
        from start import nav_client
        if nav_client is not None:
            _nav_client_cache = nav_client
            return nav_client
    except (ImportError, AttributeError):
        pass
    
    # Fallback: create a new client from config
    import yaml
    import os
    from api_clients.navidrome import NavidromeClient
    
    config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")
    try:
        with open(config_path, 'r') as f:
            config = yaml.safe_load(f)
        
        # Try multi-user config first
        nav_users = config.get('navidrome_users')
        if nav_users and len(nav_users) > 0:
            # Use first user's config
            user_config = nav_users[0]
            base_url = user_config.get('base_url')
            username = user_config.get('user')
            password = user_config.get('pass')
        else:
            # Fall back to single-user config
            nav_config = config.get('navidrome', {})
            base_url = nav_config.get('base_url')
            username = nav_config.get('user')
            password = nav_config.get('pass')
        
        if base_url and username and password:
            _nav_client_cache = NavidromeClient(base_url, username, password)
            return _nav_client_cache
    except Exception as e:
        import logging
        logging.error(f"Failed to create NavidromeClient: {e}")
    
    return None

def fetch_artist_albums(artist_id):
    """Fetch albums for an artist (wrapper using NavidromeClient)."""
    nav_client = _get_nav_client()
    if nav_client is None:
        raise RuntimeError("NavidromeClient not available - check your configuration")
    return nav_client.fetch_artist_albums(artist_id)

def fetch_album_tracks(album_id):
    """
    Fetch all tracks for an album using Subsonic API (wrapper using NavidromeClient).
    :param album_id: Album ID in Navidrome
    :return: Dict with 'tracks' (list of track objects) and 'artist' (album artist name)
    """
    nav_client = _get_nav_client()
    if nav_client is None:
        raise RuntimeError("NavidromeClient not available - check your configuration")
    return nav_client.fetch_album_tracks(album_id)

def save_to_db(track_data):
    """
    Save or update a track in the database.
    
    This function implements duplicate prevention by checking if a track with the same
    (artist, album, title, duration) already exists. If it does, it updates the existing
    track instead of creating a duplicate with a different ID.
    
    Priority for choosing which track to keep:
    1. Track with beets_mbid (beets has verified it)
    2. Track with mbid (has MusicBrainz ID)  
    3. Track with file_path (has file location)
    4. Most recently scanned track
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Log the genres being saved for debugging
    if track_data.get('genres'):
        logging.debug(f"[GENRE] Saving track '{track_data.get('title')}' with genres: '{track_data.get('genres')}'")
    
    # Convert any list values to comma-separated strings for SQLite compatibility
    sanitized_data = {}
    for key, value in track_data.items():
        if isinstance(value, list):
            # Convert list to comma-separated string
            sanitized_data[key] = ', '.join(str(v) for v in value) if value else ''
        else:
            sanitized_data[key] = value
    
    # Check for existing track by content (artist, album, title, duration)
    # This prevents duplicate albums when Navidrome IDs change
    track_id = sanitized_data.get('id')
    artist = sanitized_data.get('artist')
    album = sanitized_data.get('album')
    title = sanitized_data.get('title')
    duration = sanitized_data.get('duration')
    file_path = sanitized_data.get('file_path')
    
    if artist and album and title:
        # First try to match by file_path if available (most reliable)
        if file_path:
            cursor.execute("""
                SELECT id, beets_mbid, mbid, file_path, last_scanned 
                FROM tracks 
                WHERE file_path = ? AND id != ?
                LIMIT 1
            """, (file_path, track_id))
            existing = cursor.fetchone()
        else:
            existing = None
        
        # If no match by file_path, try content matching
        if not existing:
            # Look for existing track with same content
            if duration:
                # Match by artist, album, title, and duration (within 2 seconds tolerance)
                cursor.execute("""
                    SELECT id, beets_mbid, mbid, file_path, last_scanned 
                    FROM tracks 
                    WHERE artist = ? AND album = ? AND title = ? 
                      AND ABS(COALESCE(duration, 0) - ?) <= 2
                      AND id != ?
                    LIMIT 1
                """, (artist, album, title, duration, track_id))
            else:
                # Match by artist, album, title only
                cursor.execute("""
                    SELECT id, beets_mbid, mbid, file_path, last_scanned 
                    FROM tracks 
                    WHERE artist = ? AND album = ? AND title = ? 
                      AND id != ?
                    LIMIT 1
                """, (artist, album, title, track_id))
            
            existing = cursor.fetchone()
        
        if existing:
            existing_id = existing['id']
            existing_beets_mbid = existing['beets_mbid']
            existing_mbid = existing['mbid']
            existing_file_path = existing['file_path']
            
            # Determine which track to keep based on priority
            new_beets_mbid = sanitized_data.get('beets_mbid')
            new_mbid = sanitized_data.get('mbid')
            new_file_path = sanitized_data.get('file_path')
            
            # Calculate scores for existing and new track
            existing_score = 0
            new_score = 0
            
            if existing_beets_mbid:
                existing_score += 1000
            if existing_mbid:
                existing_score += 500
            if existing_file_path:
                existing_score += 200
                
            if new_beets_mbid:
                new_score += 1000
            if new_mbid:
                new_score += 500
            if new_file_path:
                new_score += 200
            
            # If new track has better metadata, use new ID, otherwise use existing ID
            if new_score > existing_score:
                # Keep new ID, delete old duplicate
                logging.debug(f"Duplicate found: Keeping new track ID {track_id}, deleting {existing_id} (artist={artist}, title={title})")
                cursor.execute("DELETE FROM tracks WHERE id = ?", (existing_id,))
            else:
                # Keep existing ID, update it with new data
                logging.debug(f"Duplicate found: Keeping existing track ID {existing_id}, updating instead of inserting {track_id} (artist={artist}, title={title})")
                sanitized_data['id'] = existing_id
    
    # Perform insert or update
    columns = ', '.join(sanitized_data.keys())
    placeholders = ', '.join(['?'] * len(sanitized_data))
    update_clause = ', '.join([f"{k}=excluded.{k}" for k in sanitized_data.keys()])
    sql = f"INSERT INTO tracks ({columns}) VALUES ({placeholders}) ON CONFLICT(id) DO UPDATE SET {update_clause}"
    
    # Log if genres are being inserted/updated
    if 'genres' in sanitized_data:
        logging.debug(f"[GENRE] Saving to DB - id={sanitized_data.get('id')}, title={sanitized_data.get('title')}, genres='{sanitized_data.get('genres')}'")
        has_backslash = '\\' in sanitized_data.get('genres', '')
        logging.debug(f"[GENRE] Genre string length: {len(sanitized_data.get('genres', ''))}, Contains backslash: {has_backslash}")
    
    cursor.execute(sql, list(sanitized_data.values()))
    conn.commit()
    conn.close()
    
    # Log confirmation after successful save
    if 'genres' in sanitized_data and sanitized_data.get('genres'):
        logging.debug(f"[GENRE] Successfully saved track ID {sanitized_data.get('id')} with genres to database")

def build_artist_index(verbose: bool = False):
    """Build artist index from Navidrome (wrapper using NavidromeClient)."""
    nav_client = _get_nav_client()
    if nav_client is None:
        raise RuntimeError("NavidromeClient not available - check your configuration")
    artist_map_from_api = nav_client.build_artist_index()
    max_retries = 3
    for attempt in range(max_retries):
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            for artist_name, info in artist_map_from_api.items():
                artist_id = info.get("id")
                cursor.execute("""
                    INSERT OR REPLACE INTO artist_stats (artist_id, artist_name, album_count, track_count, last_updated)
                    VALUES (?, ?, ?, ?, ?)
                """, (artist_id, artist_name, 0, 0, None))
                if verbose:
                    print(f"   📝 Added artist to index: {artist_name} (ID: {artist_id})")
                    logging.info(f"Added artist to index: {artist_name} (ID: {artist_id})")
            conn.commit()
            conn.close()
            break
        except Exception as e:
            if "locked" in str(e) and attempt < max_retries - 1:
                logging.debug(f"Database locked during artist index build, retrying ({attempt + 1}/{max_retries})...")
                time.sleep(1.0 * (attempt + 1))
                continue
            else:
                logging.error(f"Failed to build artist index after {max_retries} attempts: {e}")
                raise
    logging.info(f"✅ Cached {len(artist_map_from_api)} artists in DB")
    print(f"✅ Cached {len(artist_map_from_api)} artists in DB")
    return artist_map_from_api

def load_artist_map():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT artist_id, artist_name, album_count, track_count, last_updated FROM artist_stats")
    rows = cursor.fetchall()
    conn.close()
    return {row[1]: {"id": row[0], "album_count": row[2], "track_count": row[3], "last_updated": row[4]} for row in rows}

def get_album_last_scanned_from_db(artist_name: str, album_name: str) -> str | None:
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT MAX(last_scanned) FROM tracks WHERE artist = ? AND album = ?",
            (artist_name, album_name),
        )
        row = cursor.fetchone()
        conn.close()
        return (row[0] if row and row[0] else None)
    except Exception as e:
        logging.debug(f"get_album_last_scanned_from_db failed for '{artist_name} / {album_name}': {e}")
        return None

def get_album_track_count_in_db(artist_name: str, album_name: str) -> int:
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM tracks WHERE artist = ? AND album = ?",
            (artist_name, album_name),
        )
        count = cursor.fetchone()[0] or 0
        conn.close()
        return count
    except Exception as e:
        logging.debug(f"get_album_track_count_in_db failed for '{artist_name} / {album_name}': {e}")
        return 0

def update_artist_id_for_artist(artist_name: str, artist_id: str) -> int:
    """
    Update all tracks for an artist with the cached Spotify artist ID.
    This helps populate the cache for existing tracks.
    
    Args:
        artist_name: Artist name
        artist_id: Spotify artist ID to cache
        
    Returns:
        Number of tracks updated
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE tracks SET spotify_artist_id = ? WHERE artist = ? AND spotify_artist_id IS NULL",
            (artist_id, artist_name)
        )
        updated = cursor.rowcount
        conn.commit()
        conn.close()
        logging.debug(f"Updated {updated} tracks with Spotify artist ID for '{artist_name}'")
        return updated
    except Exception as e:
        logging.error(f"Failed to update artist ID for '{artist_name}': {e}")
        return 0


def update_discogs_artist_id_for_artist(artist_name: str, discogs_artist_id: str) -> int:
    """
    Update all tracks for an artist with the Discogs artist ID.
    This helps populate the cache for existing tracks.
    
    Args:
        artist_name: Artist name
        discogs_artist_id: Discogs artist ID to cache
        
    Returns:
        Number of tracks updated
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE tracks SET discogs_artist_id = ? WHERE artist = ? AND discogs_artist_id IS NULL",
            (discogs_artist_id, artist_name)
        )
        updated = cursor.rowcount
        conn.commit()
        conn.close()
        logging.debug(f"Updated {updated} tracks with Discogs artist ID for '{artist_name}'")
        return updated
    except Exception as e:
        logging.error(f"Failed to update Discogs artist ID for '{artist_name}': {e}")
        return 0


def fetch_comprehensive_metadata(db_track_id: str, spotify_track_id: str, force_refresh: bool = False) -> bool:
    """
    Fetch comprehensive Spotify metadata for a track and store in database.
    
    This function creates its own database connection to ensure thread safety when
    called from ThreadPoolExecutor or other background threads.
    
    Args:
        db_track_id: Database track ID (primary key)
        spotify_track_id: Spotify track ID
        force_refresh: Force refresh even if recently updated
        
    Returns:
        True if metadata was successfully fetched and stored
    """
    _ensure_clients_from_config()
    if not _spotify_enabled or _spotify_client is None or not spotify_track_id:
        return False
    
    # Always create a new connection in this thread to ensure thread safety
    # SQLite connections cannot be shared across threads
    conn = get_db_connection()
    
    try:
        from spotify_metadata_fetcher import SpotifyMetadataFetcher
        
        fetcher = SpotifyMetadataFetcher(_spotify_client, conn)
        
        result = fetcher.fetch_and_store_track_metadata(
            track_id=spotify_track_id,
            db_track_id=db_track_id,
            force_refresh=force_refresh
        )
        
        return result
    except Exception as e:
        logging.debug(f"Failed to fetch comprehensive metadata for track {spotify_track_id}: {e}")
        return False
    finally:
        conn.close()


def get_spotify_client() -> SpotifyClient | None:
    """
    Get the configured Spotify client.
    
    Returns:
        SpotifyClient instance or None if not configured
    """
    _ensure_clients_from_config()
    return _spotify_client if _spotify_enabled else None


def get_lastfm_client() -> LastFmClient | None:
    """
    Get the configured Last.fm client.
    
    Returns:
        LastFmClient instance or None if not configured
    """
    _ensure_clients_from_config()
    return _lastfm_client


def detect_via_iterative_zscore(
    current_track_score: float,
    artist: str,
    album: str,
    conn=None,
    verbose: bool = False
) -> bool:
    """
    Detect if a track is a standout using iterative z-score method.
    
    Returns True if the track is identified as a standout via:
    - album z-score >= 1.0 after iterative removal
    - artist z-score >= 0.5 (if artist stats exist)
    """
    if not current_track_score or current_track_score <= 0:
        if verbose:
            from helpers.logging_config import log_debug
            log_debug(f"detect_via_iterative_zscore: current_track_score invalid: {current_track_score}")
        return False
    
    with get_db_connection_context(conn) as db_conn:
        try:
            cursor = db_conn.cursor()
            cursor.execute("""
                SELECT id, title, popularity_score
                FROM tracks
                WHERE artist = ? AND album = ? AND popularity_score > 0
                ORDER BY popularity_score DESC
            """, (artist, album))
            
            album_tracks = cursor.fetchall()
            if not album_tracks or len(album_tracks) < 2:
                return False
            
            album_data = [(row[0], row[1], row[2]) for row in album_tracks]
            identified_standouts = set()
            
            iteration = 0
            max_iterations = 5
            
            while iteration < max_iterations:
                iteration += 1
                remaining_scores = [score for _, _, score in album_data if score > 0]
                if not remaining_scores or len(remaining_scores) < 2:
                    break
                
                try:
                    album_mean = mean(remaining_scores)
                    album_stdev = stdev(remaining_scores) if len(remaining_scores) > 1 else 0
                except (ValueError, ZeroDivisionError):
                    break
                
                if album_stdev == 0:
                    break
                
                top_score = max(remaining_scores)
                top_z = calculate_track_zscore(top_score, album_mean, album_stdev)
                if top_z < 1.0:
                    break
                
                found_standout = False
                for track_id, title, score in album_data:
                    # Use approximate equality for float comparison (within 0.01 tolerance)
                    if abs(score - top_score) < 0.01 and track_id not in identified_standouts:
                        artist_z = _check_artist_zscore(cursor, artist, track_id)
                        if artist_z >= 0.5 or artist_z == -999:
                            identified_standouts.add(track_id)
                            found_standout = True
                            # Use approximate float equality instead of exact comparison
                            # This handles floating-point rounding errors from database retrieval
                            if abs(score - current_track_score) < 0.01:  # Within 0.01 tolerance
                                return True
                            album_data = [(tid, tit, ts) for tid, tit, ts in album_data if tid != track_id]
                        break
                
                if not found_standout:
                    break
            
            return False
        except Exception as e:
            if verbose:
                logging.debug(f"Iterative zscore error: {e}")
            return False


def _check_artist_zscore(cursor, artist: str, track_id: int) -> float:
    """Get z-score for a track within its artist catalog. Returns -999 on failure."""
    try:
        cursor.execute("SELECT popularity_score FROM tracks WHERE id = ?", (track_id,))
        row = cursor.fetchone()
        if not row:
            return -999
        
        track_score = row[0]
        if not track_score:
            return -999
        
        cursor.execute("""
            SELECT mean_popularity, popularity_stddev
            FROM artist_stats
            WHERE artist = ?
        """, (artist,))
        stats_row = cursor.fetchone()
        if not stats_row or not stats_row[0]:
            return -999
        
        artist_mean = stats_row[0]
        artist_stdev = stats_row[1] if stats_row[1] else 1
        if artist_stdev == 0:
            return -999
        
        return calculate_track_zscore(track_score, artist_mean, artist_stdev)
    except Exception as e:
        logging.debug(f"Artist zscore error: {e}")
        return -999


def get_top_standout_tracks_with_gap(
    artist: str,
    album: str,
    conn=None,
    gap_threshold: float = 0.5,
    is_compilation: bool = False,
    verbose: bool = False
) -> set:
    """
    Identify tracks at the top of an album with a clear gap from lower tracks.
    
    For greatest hits and compilations, the 50% rule is skipped since these albums
    are specifically curated to contain mostly standout tracks.
    """
    with get_db_connection_context(conn) as db_conn:
        try:
            cursor = db_conn.cursor()
            cursor.execute("""
                SELECT id, title, popularity_score
                FROM tracks
                WHERE artist = ? AND album = ? AND popularity_score > 0
                ORDER BY popularity_score DESC
            """, (artist, album))
            
            album_tracks = cursor.fetchall()
            if not album_tracks or len(album_tracks) < 2:
                return set()
            
            album_data = [(row[0], row[1], row[2]) for row in album_tracks]
            scores = [score for _, _, score in album_data]
            try:
                album_mean = mean(scores)
                album_stdev = stdev(scores) if len(scores) > 1 else 0
            except (ValueError, ZeroDivisionError):
                return set()
            if album_stdev == 0:
                return set()
            
            top_standouts = set()
            prev_z = None
            for track_id, title, score in album_data:
                current_z = calculate_track_zscore(score, album_mean, album_stdev)
                if prev_z is None:
                    # First track must have z-score >= 0.8 (medium confidence threshold)
                    if current_z >= 0.8:
                        top_standouts.add(track_id)
                        prev_z = current_z
                    else:
                        break
                else:
                    # Stop if we drop below z-score of 0.5 (above average but not exceptional)
                    if current_z < 0.5:
                        break
                    gap = prev_z - current_z
                    # Gap must be small (< threshold) to be in the same "cluster"
                    if gap < gap_threshold:
                        top_standouts.add(track_id)
                        prev_z = current_z
                    else:
                        break
            
            # Check if this is a greatest hits album (by name patterns)
            album_lower = album.lower()
            greatest_hits_patterns = [
                'greatest hits', 'best of', 'the best', 'collection', 'anthology',
                'essentials', ' hits', 'singles', 'the very best', 'gold', 'platinum',
                'ultimate collection', 'complete', 'definitive'
            ]
            is_greatest_hits = any(pattern in album_lower for pattern in greatest_hits_patterns)
            
            # If more than half the album is in the "standout" cluster, then nothing is really standing out
            # UNLESS it's a compilation or greatest hits album (which are supposed to have mostly standouts)
            # Return empty set to prevent inflating ratings when the whole album is consistently good
            total_tracks = len(album_data)
            standout_count = len(top_standouts)
            if standout_count > total_tracks / 2 and not is_compilation and not is_greatest_hits:
                if verbose:
                    logging.debug(f"Top standouts: {standout_count}/{total_tracks} tracks qualify (>50%), returning empty set - no clear standouts")
                return set()
            elif standout_count > total_tracks / 2 and (is_compilation or is_greatest_hits):
                if verbose:
                    album_type = "compilation" if is_compilation else "greatest hits"
                    logging.debug(f"Top standouts: {standout_count}/{total_tracks} tracks qualify (>50%) but this is a {album_type} album - allowing standouts")
            
            return top_standouts
        except Exception as e:
            if verbose:
                logging.debug(f"Top standouts detection error: {e}")
            return set()


__all__ = [
    "configure_popularity_helpers",
    "get_spotify_artist_id",
    "get_spotify_artist_single_track_ids",
    "search_spotify_track",
    "get_lastfm_track_info",
    "score_by_age",
    "apply_mean_popularity_adjustment",
    "apply_album_deviation_adjustment",
    "SPOTIFY_WEIGHT",
    "LASTFM_WEIGHT",
    "LISTENBRAINZ_WEIGHT",
    "AGE_WEIGHT",
    "fetch_artist_albums",
    "fetch_album_tracks",
    "save_to_db",
    "build_artist_index",
    "load_artist_map",
    "get_album_last_scanned_from_db",
    "get_album_track_count_in_db",
    "update_artist_id_for_artist",
    "fetch_comprehensive_metadata",
    "get_spotify_client",
    "get_lastfm_client",
    "detect_via_iterative_zscore",
    "get_top_standout_tracks_with_gap",
]
