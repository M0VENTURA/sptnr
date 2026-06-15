#!/usr/bin/env python3
"""
Popularity Scanner - Detects track popularity from external sources (Last.fm + age/recency).
Calculates popularity scores and updates database.
Note: Singles detection is handled separately by sptnr.py rate_artist() function.
"""

import os
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
import threading
import requests
from contextlib import contextmanager
from datetime import datetime, timedelta
from statistics import median, mean, stdev
from concurrent.futures import ThreadPoolExecutor
import concurrent.futures
from api_clients import session, timeout_safe_session
from api_clients.musicbrainz import _USER_AGENT as MUSICBRAINZ_USER_AGENT
from helpers.helpers import find_matching_spotify_single, strip_cover_attribution, strip_parentheses as _strip_parentheses_unified
from helpers.matching_utils import normalize_album, strip_search_parentheses
from database_abstraction import DatabaseQuery

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
_timeout_executor_lock = threading.Lock()
_interpreter_shutting_down = False

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
    'star_5_single': {'artist_pct': 0.25},  # Single detection must also be in top 25% of artist catalog for 5★
    'popularity_5star_z_threshold': 2.0,     # Configurable z threshold for popularity-only 5★
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

# DEFAULT_HIGH_CONF_OFFSET and DEFAULT_MEDIUM_CONF_THRESHOLD were previously
# defined as hardcoded literals.  They are now kept as backwards-compatible
# aliases pointing to the same default values used by get_zscore_thresholds()
# (which reads from config.yaml at runtime).  All internal callers that previously
# used these constants already call get_zscore_thresholds() instead; these names
# are preserved only for external code that may import them directly.
DEFAULT_HIGH_CONF_OFFSET: float = 1.0    # default high z-score threshold — see get_zscore_thresholds()['high']
DEFAULT_MEDIUM_CONF_THRESHOLD: float = 0.6  # default medium z-score threshold — see get_zscore_thresholds()['medium']
DEFAULT_POPULARITY_MEAN = 50          # Default mean popularity if no valid scores available (0-100 scale)

# --- End Config ---

# Metadata source display constant
POPULARITY_METADATA_SOURCE_NAME = "Last.fm/recency popularity"  # Display name for tracks with popularity data but no single sources


def get_lastfm_config(config: dict) -> dict:
    """Return Last.fm config, supporting both lastfm and last_fm keys."""
    api_integrations = config.get("api_integrations", {}) if isinstance(config, dict) else {}
    return api_integrations.get("lastfm") or api_integrations.get("last_fm") or {}


def strip_parentheses(title: str) -> str:
    """
    Remove TRAILING parenthesized content from track title to get base version.

    Delegates to the unified ``helpers.helpers.strip_parentheses`` with
    ``trailing_only=True``.  Only the last parenthetical group is removed so
    that mid-title parentheses like "Track (One) Two" are left intact.

    Example: "Track (Live)" -> "Track"
    Example: "Track (One) Two" -> "Track (One) Two"  (no change)
    """
    return _strip_parentheses_unified(title, trailing_only=True)


def is_compilation_type(album_type: str) -> bool:
    """
    Check if album type indicates compilation.

    Handles both:
    - Old format: 'compilation' (standalone)
    - New MusicBrainz secondary type format: 'album+compilation'
    - MusicBrainz parentheses format: 'album (compilation)'

    Args:
        album_type: Album type string from database or MusicBrainz

    Returns:
        True if album is a compilation, False otherwise
    """
    if not album_type:
        return False
    album_type_lower = album_type.lower()
    return (
        album_type_lower == 'compilation'
        or '+compilation' in album_type_lower
        or '(compilation)' in album_type_lower
    )


def normalize_primary_release_type(album_type: str) -> str:
    """Normalize composite album type strings to primary release type.

    Examples:
    - "album (live)" -> "album"
    - "album+compilation" -> "album"
    - "single" -> "single"
    """
    value = (album_type or '').strip().lower()
    if not value:
        return 'album'

    if '+' in value:
        value = value.split('+', 1)[0].strip()
    if '(' in value:
        value = value.split('(', 1)[0].strip()

    if value in {'album', 'ep', 'single'}:
        return value
    return 'album'


def should_exclude_track_from_stats(title: str, album: str = "", is_live: int = 0, album_context_live: int = 0) -> bool:
    """
    Determine if a track should be excluded from album/artist statistics calculations.

    Excludes tracks that are:
    - Live versions (detected from title, album name, or is_live / album_context_live flags)
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
        is_live: Whether the track is explicitly flagged as live (1 = live)
        album_context_live: Whether the album context is flagged as live (1 = live album)

    Returns:
        True if track should be excluded from statistics, False otherwise
    """
    # If the track or album is explicitly flagged as live, exclude it immediately
    if is_live or album_context_live:
        return True

    # Strip cover attributions and single-release suffixes first so
    # "Song (Radio Edit)" doesn't match "edit" and get excluded from statistics
    base_title = strip_single_release_suffix(strip_cover_attribution(title))

    # Check base title and album name for keywords
    combined_text = f"{base_title} {album}".lower()
    return any(keyword in combined_text for keyword in IGNORE_SINGLE_KEYWORDS)


# Modifiers that make a remastered track NOT equivalent to the original studio release
# (i.e. it is also live, acoustic, etc.)
_REMASTER_INCOMPATIBLE_MODIFIERS = [
    r'\blive\b', r'\bunplugged\b', r'\bacoustic\b', r'\borchestral\b',
    r'\bsymphonic\b', r'\bremix\b', r'\bdemo\b', r'\binstrumental\b',
    r'\bkaraoke\b',
]


def strip_remaster_suffix(title: str) -> str:
    """Strip remastered/remaster markers from a track title to get the base title.

    Handles patterns such as:
      - "Higher (remastered 2024)"              → "Higher"
      - "Higher (remastered)"                   → "Higher"
      - "Higher (Remastered Version)"           → "Higher"
      - "Higher (2024 Remastered Edition)"      → "Higher"
      - "Higher (radio edit / remastered 2024)" → "Higher (radio edit)"
      - "Song - Remastered 2024"                → "Song"
    """
    result = title
    # First, remove "/ remastered" or "- remastered" [year] inside parentheticals
    # This preserves other keywords like "radio edit" that appear with remastered
    result = re.sub(r'\s*[/\-]\s*remaster(?:ed)?(?:\s+\d{4})?', '', result, flags=re.IGNORECASE)

    # Now remove standalone parenthetical containing "remaster" or "remastered"
    # Use negative lookbehind/lookahead to avoid matching mixed cases already handled above
    # This pattern removes parentheticals where remaster is the ONLY keyword (or with just a year/edition text)
    result = re.sub(r'\s*\([^()]*remaster(?:ed)?[^()]*\)', '', result, flags=re.IGNORECASE)

    # Remove trailing "- Remastered [year]" (dash-separated title suffix)
    result = re.sub(r'\s*-\s*remaster(?:ed)?(?:\s+\d{4})?\s*$', '', result, flags=re.IGNORECASE)
    # Clean up empty parentheses left over after stripping
    result = re.sub(r'\(\s*\)', '', result)
    return result.strip()


# Parenthetical version suffixes that identify a single/radio release cut.
# These should be stripped before API lookups so that "Song (Radio Edit)"
# is searched as "Song" – the title databases actually index.
# Unlike live/remix/acoustic suffixes, these indicate the same performance
# just packaged differently for radio or singles release.
#
# Regex structure:
#   \s*\(\s*   – optional leading whitespace and opening parenthesis
#   (?:…)      – non-capturing group of alternates for the version keyword
#   \s*\)\s*$  – closing parenthesis followed by optional whitespace at end-of-string ($)
#                The $ anchor ensures only trailing suffixes are removed; mid-title
#                parentheses like "Song (Part 1) (Radio Edit)" are only stripped at the end.
_SINGLE_RELEASE_SUFFIX_RE = re.compile(
    r'\s*\(\s*(?:'
    r'radio\s+(?:edit|mix|version)'    # (Radio Edit), (Radio Mix), (Radio Version)
    r'|single\s+(?:version|edit|mix)'  # (Single Version), (Single Edit), (Single Mix)
    r'|album\s+version'                # (Album Version)
    r')\s*\)\s*$',
    re.IGNORECASE,
)


def strip_single_release_suffix(title: str) -> str:
    """Strip common single/radio-release version suffixes from a track title.

    These parenthetical suffixes indicate a specific cut of the same song
    (e.g. a radio-ready edit or the version released as a single) rather than
    a substantially different arrangement (live, acoustic, remix).  Stripping
    them before querying Last.fm, Spotify, MusicBrainz, or Discogs ensures
    that the lookup targets the base song entry, which is what those services
    index under.

    Examples:
      "Higher (Radio Edit)"       → "Higher"
      "Song (Single Version)"     → "Song"
      "Track (Album Version)"     → "Track"
      "Live Song (Live)"          → "Live Song (Live)"   (unchanged)
    """
    return _SINGLE_RELEASE_SUFFIX_RE.sub('', title).strip()


def normalize_title_for_lookup(title: str, extra_strip_patterns: list[str] | None = None) -> str:
    """Normalise a track title for external API lookups.

    Applies all title-cleaning steps in sequence:
    1. ``strip_cover_attribution`` – removes cover credits like "(cover)"
    2. ``strip_remaster_suffix``   – removes remastered year markers
    3. ``strip_single_release_suffix`` – removes radio-edit/single-version suffixes
    4. Optional extra patterns from ``strip_parentheses_filters`` config key

    The resulting title matches how Last.fm, MusicBrainz, Discogs, and Spotify
    index tracks – without release-specific qualifiers that cause lookup misses.

    Examples:
      "Higher (Radio Edit)"                           → "Higher"
      "Higher (remastered 2024)"                      → "Higher"
      "Higher (radio edit / remastered 2024)"         → "Higher"
      "With Arms Wide Open (single version / remastered 2024)" → "With Arms Wide Open"
    """
    result = strip_single_release_suffix(strip_remaster_suffix(strip_cover_attribution(title)))
    if extra_strip_patterns:
        result = _strip_parentheses_unified(result, trailing_only=False, extra_patterns=extra_strip_patterns)
    return result


def is_remastered_only_variant(title: str) -> bool:
    """Return True when the track title is a remastered version with no other
    non-canonical modifiers (live, acoustic, remix, etc.).

    Remastered tracks inherently have lower Spotify popularity than the original
    because listeners tend to stream the original release.  Their negative z-score
    does NOT mean they are not singles — we should still run single detection on them.

    Examples that return True:
      - "Higher (remastered 2024)"
      - "Higher (radio edit / remastered 2024)"
      - "What If (remastered 2024)"

    Examples that return False:
      - "Roadhouse Blues (live at Woodstock / remastered 2024)"  – has 'live'
      - "With Arms Wide Open (acoustic version / remastered 2024)" – has 'acoustic'
      - "Higher"  – no remaster marker at all
    """
    base = strip_remaster_suffix(title)
    # No remaster marker found — return False so normal gating applies
    if base.lower().strip() == title.lower().strip():
        return False
    # If the remaining base title still contains a non-canonical (incompatible) modifier,
    # this is NOT a simple remastered variant.
    base_lower = base.lower()
    for pattern in _REMASTER_INCOMPATIBLE_MODIFIERS:
        if re.search(pattern, base_lower):
            return False
    return True


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
        r'\bin\s+concert\b',       # "in concert"
    ]

    import re
    return any(re.search(pattern, album_lower) for pattern in live_patterns)


def _detect_live_album_type(album: str, album_type_from_field: str = '') -> str:
    """Determine whether a live/unplugged album is specifically acoustic or live.

    Returns ``'acoustic'`` for unplugged/acoustic albums, ``'live'`` for everything
    else that passes ``is_live_or_alternate_album``.  The acoustic check takes
    priority because acoustic/unplugged versions differ more from the studio
    recording than a typical live performance.
    """
    album_lower = (album or '').lower()
    type_lower = (album_type_from_field or '').lower()

    acoustic_patterns = [r'\bunplugged\b', r'\bacoustic\b']
    if any(re.search(p, album_lower) for p in acoustic_patterns) or \
       any(re.search(p, type_lower) for p in acoustic_patterns):
        return 'acoustic'

    return 'live'


def is_live_or_unplugged_track_title(title: str) -> bool:
    """Return True when a track title clearly indicates a live/unplugged version."""
    if not title:
        return False

    title_lower = title.lower()
    live_title_patterns = [
        r'\blive\s+at\b',
        r'\blive\s+in\b',
        r'\blive\s+from\b',
        r'\blive\s+session\b',
        r'\(live[^)]*\)',
        r'\[live[^\]]*\]',
        r'-\s*live\b',
        r'\bunplugged\b',
    ]
    return any(re.search(pattern, title_lower) for pattern in live_title_patterns)


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
        if album_artist_lower in ('various artists', 'various artists -', 'various', 'compilation', 'soundtrack'):
            log_debug(f'Compilation detected for "{album}": album_artist="{album_artist}"')
            return True

    # Check Spotify/MusicBrainz classification (handles 'compilation', 'album+compilation',
    # 'album (compilation)' and any other composite format containing "compilation")
    if spotify_album_type and is_compilation_type(spotify_album_type):
        log_debug(f'Compilation detected for "{album}": spotify_album_type="{spotify_album_type}"')
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


def detect_greatest_hits_album(album: str, artist: str, conn: object, album_tracks: list = None) -> bool:
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
                    placeholder = "%s"
                    cursor = conn.cursor()
                    cursor.execute(f"""
                        SELECT AVG(popularity_score) as avg_pop, COUNT(*) as track_count
                        FROM tracks
                        WHERE artist = {placeholder} AND popularity_score > 0
                    """, (artist,))
                    row = cursor.fetchone()

                    if row and row_get(row, 'avg_pop') and row_get(row, 'track_count', 0) > 10:  # Need at least 10 tracks for meaningful comparison
                        artist_avg_pop = row_get(row, 'avg_pop')

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


def detect_and_queue_missing_tracks(artist: str, album: str, album_tracks: list, release_group_mbid: str = None, conn: object = None):
    """
    Detect missing tracks from an album by comparing local tracks with MusicBrainz tracklist.
    Returns a count of missing tracks so UI/workflows can present them for manual action.

    Args:
        artist: Artist name
        album: Album name
        album_tracks: List of track dicts from local database
        release_group_mbid: MusicBrainz release group MBID (optional, faster lookup if provided)
        conn: Database connection (optional, will create new connection if not provided)

    Returns:
        Number of missing tracks detected (0 if none or error)
    """
    try:
        from folder_matching_enhancements import get_musicbrainz_release_tracks

        # Get release ID if not provided
        if not release_group_mbid:
            # Try to get from database. Prefer release-group MBID since the tracklist
            # lookup supports it natively; fall back to release MBID for compatibility.
            if conn:
                placeholder = "%s"
                cursor = conn.cursor()
                cursor.execute(f"""
                    SELECT musicbrainz_releasegroupid, musicbrainz_album_mbid FROM tracks
                    WHERE COALESCE(NULLIF(album_artist, ''), artist) = {placeholder}
                      AND album = {placeholder}
                      AND (musicbrainz_releasegroupid IS NOT NULL AND musicbrainz_releasegroupid != ''
                           OR musicbrainz_album_mbid IS NOT NULL AND musicbrainz_album_mbid != '')
                    LIMIT 1
                """, (artist, album))
                mbid_row = cursor.fetchone()
                if mbid_row:
                    _rg_mbid = row_get(mbid_row, 'musicbrainz_releasegroupid')
                    _rel_mbid = row_get(mbid_row, 'musicbrainz_album_mbid')
                    release_group_mbid = _rg_mbid or _rel_mbid

        if not release_group_mbid:
            log_debug(f"No MusicBrainz release ID found for '{artist} - {album}', skipping missing track detection")
            return 0

        # Fetch complete tracklist from MusicBrainz
        log_debug(f"Fetching MusicBrainz tracklist for '{artist} - {album}' (MBID: {release_group_mbid})")
        mb_tracks = get_musicbrainz_release_tracks(release_group_mbid, source='musicbrainz')

        if not mb_tracks:
            log_debug(f"No tracklist returned from MusicBrainz for '{artist} - {album}'")
            return 0

        log_info(f"MusicBrainz shows {len(mb_tracks)} track(s) for '{artist} - {album}', local library has {len(album_tracks)} track(s)")

        # Build a consumable local-track pool so duplicate titles are matched
        # one-to-one against MusicBrainz rows instead of via reusable set lookups.
        local_track_entries = []
        for track in album_tracks:
            track_title = track.get('title', '')
            normalized = ''
            if track_title:
                # Normalize: lowercase, remove special chars
                normalized = unicodedata.normalize("NFKD", track_title)
                normalized = "".join(c for c in normalized if not unicodedata.combining(c))
                normalized = normalized.lower().strip()
                normalized = re.sub(r'[^a-z0-9]+', ' ', normalized)
                normalized = ' '.join(normalized.split())
            track_num = track.get('track_number')
            track_num_int = None
            if track_num is not None:
                try:
                    track_num_int = int(str(track_num).split('/')[0].strip())
                except (ValueError, TypeError):
                    track_num_int = None
            disc_num = track.get('disc_number')
            try:
                disc_num_int = int(disc_num or 1)
            except (ValueError, TypeError):
                disc_num_int = 1
            local_track_entries.append({
                'norm_title': normalized,
                'track_num': track_num_int,
                'disc_num': disc_num_int,
            })

        remaining_local_entries = list(local_track_entries)

        def _pop_local_match(predicate):
            for idx, entry in enumerate(remaining_local_entries):
                if predicate(entry):
                    return remaining_local_entries.pop(idx)
            return None

        def _title_matches_entry(entry, norm, norm_rec, norm_stripped):
            if entry['norm_title'] == norm:
                return True
            if norm_rec and entry['norm_title'] == norm_rec:
                return True
            if norm_stripped and entry['norm_title'] == norm_stripped:
                return True
            return False

        # Find missing tracks
        missing_tracks = []
        for mb_track in mb_tracks:
            mb_title = mb_track.get('title', '')
            if not mb_title:
                continue

            # Normalize MB track title
            mb_normalized = unicodedata.normalize("NFKD", mb_title)
            mb_normalized = "".join(c for c in mb_normalized if not unicodedata.combining(c))
            mb_normalized = mb_normalized.lower().strip()
            mb_normalized = re.sub(r'[^a-z0-9]+', ' ', mb_normalized)
            mb_normalized = ' '.join(mb_normalized.split())

            # Feat-stripped variant: remove "feat.", "ft.", "featuring" suffixes
            # so a library title "Until the End" matches MB "Until the End (feat. X)".
            feat_stripped = re.sub(
                r'\s*[\(\[](?:feat\.?|ft\.?|featuring|with)\b[^\)\]]*[\)\]]',
                '', mb_title, flags=re.IGNORECASE,
            )
            feat_stripped = re.sub(r'\s+(?:feat\.?|ft\.?|featuring)\s+.+$', '', feat_stripped, flags=re.IGNORECASE)
            feat_stripped = unicodedata.normalize("NFKD", feat_stripped)
            feat_stripped = "".join(c for c in feat_stripped if not unicodedata.combining(c))
            feat_stripped = feat_stripped.lower().strip()
            feat_stripped = re.sub(r'[^a-z0-9]+', ' ', feat_stripped)
            mb_normalized_stripped = ' '.join(feat_stripped.split())
            if mb_normalized_stripped == mb_normalized:
                mb_normalized_stripped = ''  # no change; skip extra check

            # Also normalise the canonical recording title (may differ from track
            # title for live releases where the track adds a venue suffix).
            # e.g. recording_title "Dig" matches library "Dig" even when
            # mb_title is "Dig (live at Candlestick Park, ...)".
            mb_recording_title = mb_track.get('recording_title', '') or ''
            mb_normalized_recording = ''
            if mb_recording_title and mb_recording_title != mb_title:
                mb_normalized_recording = unicodedata.normalize("NFKD", mb_recording_title)
                mb_normalized_recording = "".join(c for c in mb_normalized_recording if not unicodedata.combining(c))
                mb_normalized_recording = mb_normalized_recording.lower().strip()
                mb_normalized_recording = re.sub(r'[^a-z0-9]+', ' ', mb_normalized_recording)
                mb_normalized_recording = ' '.join(mb_normalized_recording.split())

            # Determine MB track number
            mb_number = mb_track.get('number')
            mb_track_num = None
            if mb_number is not None:
                try:
                    mb_track_num = int(str(mb_number).split('/')[0].strip())
                except (ValueError, TypeError):
                    pass
            mb_disc_num = mb_track.get('disc_number')
            try:
                mb_disc_num_int = int(mb_disc_num or 1)
            except (ValueError, TypeError):
                mb_disc_num_int = 1

            matched_entry = None
            if mb_track_num is not None:
                matched_entry = _pop_local_match(
                    lambda entry: (
                        entry['disc_num'] == mb_disc_num_int
                        and entry['track_num'] == mb_track_num
                        and _title_matches_entry(entry, mb_normalized, mb_normalized_recording, mb_normalized_stripped)
                    )
                )

            if matched_entry is None:
                matched_entry = _pop_local_match(
                    lambda entry: _title_matches_entry(entry, mb_normalized, mb_normalized_recording, mb_normalized_stripped)
                )

            if matched_entry is None:
                missing_tracks.append(mb_track)

        if not missing_tracks:
            log_debug(f"All tracks present for '{artist} - {album}'")
            return 0

        # Report missing tracks for manual download workflows
        log_info(f"🔍 Found {len(missing_tracks)} missing track(s) for '{artist} - {album}'")

        for mb_track in missing_tracks:
            track_title = mb_track.get('title', '')
            track_number = mb_track.get('number', '')
            log_info(f"  • Missing track detected: {artist} - {track_title} (track {track_number})")

        log_info(f"ℹ️ Missing tracks were detected but not auto-queued for '{artist} - {album}'")

        return len(missing_tracks)

    except Exception as e:
        log_debug(f"Error detecting missing tracks for '{artist} - {album}': {e}")
        import traceback
        log_debug(f"Traceback: {traceback.format_exc()}")
        return 0


def should_skip_spotify_lookup(track_id: str, conn: object) -> bool:
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
        placeholder = "%s"
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT last_spotify_lookup, popularity_score
            FROM tracks
            WHERE id = {placeholder}
        """, (track_id,))
        row = cursor.fetchone()

        if not row or not row_get(row, 'last_spotify_lookup'):
            # No cached lookup timestamp
            return False

        last_lookup_str = row_get(row, 'last_spotify_lookup')
        popularity_score = row_get(row, 'popularity_score')

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
    Get a value from a DB row object with a default fallback.

    Some row objects do not expose a .get() method like dictionaries,
    so this helper provides similar functionality.

    Args:
        row: Row object
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


def is_track_older_than_years(track_year: int = None, min_age_years: int = 2) -> bool:
    """Return True when a track year indicates the release is at least min_age_years old."""
    if not track_year:
        return False
    try:
        current_year = datetime.now().year
        return (current_year - int(track_year)) >= min_age_years
    except (ValueError, TypeError):
        return False


def should_freeze_mature_track_popularity(track: object, min_age_years: int = 2) -> bool:
    """
    Skip popularity refresh for mature releases that already have completed Last.fm data.

    This preserves historical popularity for older albums and avoids repeated refreshes when
    the scan already has both a stored popularity score and non-zero Last.fm listeners.
    Tracks with zero/missing Last.fm listeners are still eligible for retry.
    """
    if not is_track_older_than_years(row_get(track, 'year'), min_age_years=min_age_years):
        return False

    return (
        (row_get(track, 'popularity_score', 0) or 0) > 0
        and (row_get(track, 'lastfm_track_playcount', 0) or 0) > 0
    )


def get_mature_track_freeze_cutoff_years(config: dict = None, default_years: int = 2) -> int:
    """Return the configured age threshold for freezing mature-track popularity refreshes."""
    try:
        features_config = config.get('features', {}) if isinstance(config, dict) else {}
        cutoff_years = int(features_config.get('mature_track_min_age_years', default_years))
        return max(1, cutoff_years)
    except (TypeError, ValueError, AttributeError):
        return default_years


def popularity_values_changed(track: object, new_values: dict) -> bool:
    """Return True when any persisted popularity-related value differs from the current row."""

    def _normalize_number(value):
        if value is None:
            return 0.0
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _numbers_equal(left, right, tolerance: float = 0.01) -> bool:
        return abs(_normalize_number(left) - _normalize_number(right)) < tolerance

    text_fields = (
        'spotify_genres',
        'lastfm_tags',
        'listenbrainz_genres',
        'discogs_genres',
        'musicbrainz_genres',
        'cover_art_url',
    )
    numeric_fields = (
        'popularity_score',
        'spotify_score',
        'lastfm_ratio',
        'lastfm_track_playcount',
    )

    for field_name in numeric_fields:
        current_value = row_get(track, field_name)
        if field_name == 'spotify_score' and current_value is None:
            current_value = row_get(track, 'spotify_popularity', 0)
        if not _numbers_equal(current_value, new_values.get(field_name)):
            return True

    for field_name in text_fields:
        current_value = row_get(track, field_name)
        new_value = new_values.get(field_name)
        if (current_value or None) != (new_value or None):
            return True

    return False


def should_use_cached_score(track: object, cache_field: str, last_lookup_field: str = 'last_spotify_lookup') -> bool:
    """
    Check if a cached API score should be reused instead of fetching from API.

    Uses age-based cache duration - older albums are cached longer.

    Args:
        track: Track row with cached values
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


def calculate_artist_popularity_stats(artist_name: str, conn: object) -> dict:
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
        - top_25_percentile: Popularity threshold for top 25% of artist's tracks
    """
    try:
        placeholder = "%s"
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
        except Exception as e:
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
            popularity_score = row_get(row, 'popularity_score', 0)
            title = row_get(row, 'title', '')
            album = row_get(row, 'album', '') if has_album_column else ""
            _is_live_flag = row_get(row, 'is_live', 0) or 0
            _album_context_live_flag = row_get(row, 'album_context_live', 0) or 0

            # Exclude live/remix/alternate versions from artist statistics
            if not should_exclude_track_from_stats(title, album, _is_live_flag, _album_context_live_flag):
                scores.append(popularity_score)

        if not scores:
            return {
                'avg_popularity': 0,
                'median_popularity': 0,
                'stddev_popularity': 0,
                'track_count': 0,
                'top_15_percentile': 0,
                'top_20_percentile': 0,
                'top_25_percentile': 0
            }

        # Sort scores to calculate percentiles
        sorted_scores = sorted(scores, reverse=True)
        track_count = len(sorted_scores)

        # Calculate percentile thresholds
        # Top 15%: This is approximately 85th percentile (top artists of artist's work)
        # Top 20%: This is approximately 80th percentile (broader standout tracks)
        # Top 25%: Used for single-detection star rating gate (issue #770)

        top_15_index = max(0, int(track_count * 0.15) - 1)  # -1 for 0-based index
        top_20_index = max(0, int(track_count * 0.20) - 1)
        top_25_index = max(0, int(track_count * 0.25) - 1)

        top_15_threshold = sorted_scores[top_15_index] if top_15_index < track_count else 0
        top_20_threshold = sorted_scores[top_20_index] if top_20_index < track_count else 0
        top_25_threshold = sorted_scores[top_25_index] if top_25_index < track_count else 0

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
            'top_20_percentile': top_20_threshold,   # Top 20% of artist's tracks
            'top_25_percentile': top_25_threshold,   # Top 25% of artist's tracks (single gate)
        }
    except Exception as e:
        try:
            if conn is not None:
                conn.rollback()
        except Exception:
            pass
        log_verbose(f"   Error calculating artist stats: {e}")
        return {
            'avg_popularity': 0,
            'median_popularity': 0,
            'stddev_popularity': 0,
            'mad_popularity': 0,
            'track_count': 0,
            'top_15_percentile': 0,
            'top_20_percentile': 0,
            'top_25_percentile': 0,
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

    # Don't apply suffix-based exclusion when the majority of tracks have parenthetical
    # suffixes - this indicates a fully-formatted album (e.g. deluxe with all tracks as
    # "(remastered 2024)") rather than a few appended bonus tracks.  In those cases every
    # track would be excluded, leaving no valid scores for statistics.
    suffix_ratio_threshold = 0.5
    if len(tracks_with_suffix) >= len(tracks_with_scores) * suffix_ratio_threshold:
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
    if re.search(r'\blive\b', title.lower()) or re.search(r'\blive\b', album.lower()):
        genre_scores["live"] += 0.5
    # Christmas detection: strong boost so the genre reliably surfaces at the top
    _christmas_keywords = ["christmas", "xmas", "yuletide", "holiday season", "jingle bells",
                           "silent night", "deck the halls", "winter wonderland", "feliz navidad",
                           "rudolph", "santa claus", "sleigh bells"]
    if any(word in title.lower() or word in album.lower() for word in _christmas_keywords):
        genre_scores["christmas"] += 2.0  # boosted from 0.5 so it reliably appears first

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
    global _timeout_executor, _interpreter_shutting_down
    _interpreter_shutting_down = True
    with _timeout_executor_lock:
        if _timeout_executor:
            _timeout_executor.shutdown(wait=False)
            _timeout_executor = None


def _ensure_timeout_executor():
    """Ensure the shared timeout executor exists and can accept work."""
    global _timeout_executor
    with _timeout_executor_lock:
        if _interpreter_shutting_down:
            return None
        if _timeout_executor is None or getattr(_timeout_executor, "_shutdown", False):
            if _timeout_executor is not None and getattr(_timeout_executor, "_shutdown", False):
                log_debug("Timeout executor was already shut down; creating a new shared executor")
            _timeout_executor = ThreadPoolExecutor(max_workers=20, thread_name_prefix="api_timeout")
        return _timeout_executor


def _execute_tracks_batch_with_retry(conn, cursor, query, params, context, max_retries=5):
    """Execute a batch tracks UPDATE with deadlock recovery and retry."""
    rows = list(params or [])
    if not rows:
        return 0

    delay = 0.15
    for attempt in range(max_retries):
        try:
            cursor.executemany(query, rows)
            return len(rows)
        except Exception as e:
            err = str(e).lower()
            is_deadlock_like = (
                "deadlock" in err
                or "could not serialize" in err
                or "infailedsqltransaction" in err
            )
            try:
                conn.rollback()
            except Exception:
                pass

            if is_deadlock_like and attempt < (max_retries - 1):
                sleep_for = min(delay * (2 ** attempt), 2.0)
                log_debug(
                    f"{context}: transient DB contention ({type(e).__name__}) "
                    f"retry {attempt + 1}/{max_retries} in {sleep_for:.2f}s"
                )
                time.sleep(sleep_for)
                continue

            raise


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
    log_verbose(f"[TIMEOUT DEBUG] Submitting task {func.__name__} with timeout {timeout_seconds}s")
    executor = _ensure_timeout_executor()
    if executor is None:
        raise TimeoutError(f"{error_message} (service shutting down)")
    try:
        future = executor.submit(func, *args, **kwargs)
    except RuntimeError as submit_err:
        submit_err_text = str(submit_err).lower()
        if "cannot schedule new futures after interpreter shutdown" in submit_err_text:
            raise TimeoutError(f"{error_message} (service shutting down)")
        if "cannot schedule new futures after shutdown" in submit_err_text:
            log_debug("Timeout executor was shut down during submit; recreating and retrying once")
            global _timeout_executor
            with _timeout_executor_lock:
                _timeout_executor = None
            executor = _ensure_timeout_executor()
            if executor is None:
                raise TimeoutError(f"{error_message} (service shutting down)")
            future = executor.submit(func, *args, **kwargs)
        else:
            raise
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
    get_lastfm_track_info,
    calculate_lastfm_popularity_score,
    calculate_lastfm_zscore_popularity,
    calculate_listenbrainz_popularity_score,
    calculate_combined_popularity_score,
    get_listenbrainz_batch_for_tracks,
    score_by_age,
    update_artist_id_for_artist,
    get_lastfm_client,
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
    placeholder = "%s"
    cursor = conn.cursor()
    cursor.execute(f"""
        SELECT popularity_score
        FROM tracks
        WHERE artist = {placeholder} AND album = {placeholder} AND popularity_score > 0
    """, (artist, album))

    popularities = [row_get(row, 'popularity_score', 0) for row in cursor.fetchall()]

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
    placeholder = "%s"
    cursor = conn.cursor()
    cursor.execute(f"""
        SELECT popularity_score
        FROM tracks
        WHERE artist = {placeholder} AND popularity_score > 0
    """, (artist,))

    popularities = [row_get(row, 'popularity_score', 0) for row in cursor.fetchall()]

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
    """Get PostgreSQL connection only (SQLite is not supported)."""
    if not (PG_HOST and PG_USER and PG_DATABASE):
        raise RuntimeError(
            "PostgreSQL is required. Configure PG_HOST, PG_USER, and PG_DATABASE."
        )
    if psycopg2 is None:
        raise RuntimeError("psycopg2 not available - install with: pip install psycopg2-binary")

    return psycopg2.connect(
        host=PG_HOST,
        port=PG_PORT,
        user=PG_USER,
        password=PG_PASSWORD,
        dbname=PG_DATABASE,
        cursor_factory=psycopg2.extras.RealDictCursor
    )


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

def _load_navidrome_users_from_config() -> list:
    """Return a list of Navidrome user credential dicts from config.

    Each dict has keys: base_url, user, pass.
    Returns an empty list when no credentials are configured.
    """
    users = []
    try:
        config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")
        with open(config_path, 'r') as f:
            config = yaml.safe_load(f) or {}

        nav_users = config.get('navidrome_users', [])
        for u in nav_users:
            base_url = (u.get('base_url') or '').strip().rstrip('/')
            user = (u.get('user') or '').strip()
            pw = (u.get('pass') or '').strip()
            if base_url and user and pw:
                users.append({'base_url': base_url, 'user': user, 'pass': pw})

        if not users:
            # Fall back to the legacy single-user block
            nav_cfg = config.get('navidrome', {}) or {}
            base_url = (nav_cfg.get('base_url') or '').strip().rstrip('/')
            user = (nav_cfg.get('user') or '').strip()
            pw = (nav_cfg.get('pass') or '').strip()
            if base_url and user and pw:
                users.append({'base_url': base_url, 'user': user, 'pass': pw})
    except Exception:
        # Try env vars as last resort
        nav_url = os.environ.get("NAV_BASE_URL", "").strip("/")
        nav_user = os.environ.get("NAV_USER", "")
        nav_pass = os.environ.get("NAV_PASS", "")
        if nav_url and nav_user and nav_pass:
            users.append({'base_url': nav_url, 'user': nav_user, 'pass': nav_pass})
    return users


def _is_sync_ratings_to_all_users_enabled() -> bool:
    """Return True when the sync_ratings_to_all_users feature flag is enabled."""
    try:
        config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")
        with open(config_path, 'r') as f:
            config = yaml.safe_load(f) or {}
        return bool((config.get('features') or {}).get('sync_ratings_to_all_users', False))
    except Exception:
        return False


def sync_track_rating_to_navidrome(track_id: str, stars: int) -> bool:
    """
    Sync a single track rating to Navidrome using the Subsonic API.

    When the ``sync_ratings_to_all_users`` feature flag is enabled the rating
    is pushed to every configured Navidrome user account.  Otherwise only the
    primary (first) user is updated — matching the historical behaviour.

    Args:
        track_id: Navidrome track ID
        stars: Star rating (1-5)

    Returns:
        True if at least one user sync succeeded, False otherwise
    """
    try:
        all_users = _load_navidrome_users_from_config()
        if not all_users:
            # Nothing from config — try legacy env vars directly
            nav_url = os.environ.get("NAV_BASE_URL", "").strip("/")
            nav_user = os.environ.get("NAV_USER", "")
            nav_pass = os.environ.get("NAV_PASS", "")
            if not all([nav_url, nav_user, nav_pass]):
                log_verbose("Navidrome credentials not configured, skipping rating sync")
                return False
            all_users = [{'base_url': nav_url, 'user': nav_user, 'pass': nav_pass}]

        # Decide which users to sync to
        sync_all = _is_sync_ratings_to_all_users_enabled()
        users_to_sync = all_users if sync_all else all_users[:1]

        any_success = False
        for creds in users_to_sync:
            nav_url = creds['base_url']
            nav_user = creds['user']
            nav_pass = creds['pass']

            params = {
                "u": nav_user,
                "p": nav_pass,
                "v": "1.16.1",
                "c": "sptnr",
                "f": "json",
                "id": track_id,
                "rating": stars
            }

            try:
                response = session.get(f"{nav_url}/rest/setRating.view", params=params, timeout=10)
                response.raise_for_status()
                result = response.json()
                if result.get("subsonic-response", {}).get("status") == "ok":
                    any_success = True
                else:
                    error_msg = result.get("subsonic-response", {}).get("error", {}).get("message", "Unknown error")
                    log_basic(f"Navidrome API error for track {track_id} (user {nav_user}): {error_msg}")
            except Exception as exc:
                log_basic(f"Failed to sync rating to Navidrome for track {track_id} (user {nav_user}): {exc}")

        return any_success

    except Exception as e:
        log_basic(f"Failed to sync rating to Navidrome for track {track_id}: {e}")
        return False

def save_popularity_progress(
    processed_artists: int,
    total_artists: int,
    current_artist: str = None,
    progress_file: str = None,
    scan_type: str = "popularity_scan",
    last_completed_artist: str = None,
):
    """Save popularity scan progress to file.

    ``current_artist`` is the artist currently being processed (used for
    dashboard display).  ``last_completed_artist``, when provided, records the
    most-recently *fully-completed* artist and is used as the resume checkpoint
    on restart.  Keeping these two values separate ensures that a DB
    disconnection that kills the scan mid-artist does not cause infinite
    resume-from-the-same-artist loops: resume always starts from the last
    *completed* artist, not the one that may have been in-flight when the
    process died.
    """
    try:
        target_progress_file = progress_file or POPULARITY_PROGRESS_FILE

        # Preserve the existing last_completed_artist when the caller does not
        # explicitly supply one (e.g. the in-progress marker at the *start* of
        # processing an artist should not change the completed checkpoint).
        preserved_last_completed = last_completed_artist
        if preserved_last_completed is None:
            try:
                if os.path.exists(target_progress_file):
                    with open(target_progress_file, 'r') as _f:
                        _existing = json.load(_f)
                    preserved_last_completed = _existing.get('last_completed_artist')
            except Exception:
                pass

        progress_data = {
            "is_running": True,
            # Explicit "running" status so that load_scan_progress() does not
            # let a stale "starting" marker overwrite it after a restart.
            # Without this, detect_interrupted_scan() rejects the checkpoint
            # (status="starting" + no resume_from_artist → returns None).
            "status": "running",
            "scan_type": scan_type,
            "processed_artists": processed_artists,
            "total_artists": total_artists,
            "percent_complete": int((processed_artists / total_artists * 100)) if total_artists > 0 else 0,
            "current_artist": current_artist,
            "last_updated": datetime.now().isoformat()
        }
        if preserved_last_completed is not None:
            progress_data["last_completed_artist"] = preserved_last_completed

        with open(target_progress_file, 'w') as f:
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
        headers = {"User-Agent": MUSICBRAINZ_USER_AGENT}

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

        placeholder = "%s"

        # For PostgreSQL, use information_schema.
        cursor = db_query.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'tracks'
        """)
        track_columns = {row['column_name'] for row in cursor.fetchall()}

        mb_album_column = "musicbrainz_album_mbid" if "musicbrainz_album_mbid" in track_columns else None
        mb_rg_column = "musicbrainz_releasegroupid" if "musicbrainz_releasegroupid" in track_columns else None

        album_mbid = None
        release_group_mbid_art = None
        if mb_album_column or mb_rg_column:
            select_cols = []
            if mb_album_column:
                select_cols.append(f"{mb_album_column} AS album_mbid")
            if mb_rg_column:
                select_cols.append(f"{mb_rg_column} AS release_group_mbid")
            cursor = db_query.execute(
                f"""
                SELECT {', '.join(select_cols)} FROM tracks
                WHERE COALESCE(NULLIF(album_artist, ''), artist) = {placeholder}
                  AND album = {placeholder}
                  AND ({' OR '.join([f"{col} IS NOT NULL" for col in [c for c in [mb_album_column, mb_rg_column] if c]])})
                LIMIT 1
                """,
                (artist, album),
            )
            result = cursor.fetchone()
            if result:
                album_mbid = result.get('album_mbid') or None
                release_group_mbid_art = result.get('release_group_mbid') or None
        conn.close()

        # If we don't have MBID, try to search for it
        if not album_mbid and not release_group_mbid_art:
            try:
                search_url = "https://musicbrainz.org/ws/2/release-group"
                params = {
                    "query": f'release:"{album}" AND artist:"{artist}"',
                    "fmt": "json",
                    "limit": 1
                }
                headers = {"User-Agent": MUSICBRAINZ_USER_AGENT}
                resp = requests.get(search_url, params=params, headers=headers, timeout=3)
                resp.raise_for_status()
                data = resp.json()
                rgs = data.get("release-groups", [])
                if rgs:
                    release_group_mbid_art = rgs[0].get("id")
                    log_debug(f"[ALBUM_ART] Found MBID via search - {artist} - {album}: {release_group_mbid_art}")
            except Exception as e:
                log_debug(f"[ALBUM_ART] MusicBrainz album search failed: {e}")
                return None

        if not album_mbid and not release_group_mbid_art:
            log_debug(f"[ALBUM_ART] No MBID found for {artist} - {album}")
            return None

        # Build candidate CAA URLs: prefer release-group art, fall back to release-specific art.
        _caa_urls = []
        if release_group_mbid_art:
            _caa_urls.append(f"https://coverartarchive.org/release-group/{release_group_mbid_art}/front-500")
        if album_mbid:
            _caa_urls.append(f"https://coverartarchive.org/release/{album_mbid}/front-500")

        for cover_url in _caa_urls:
            try:
                head_resp = requests.head(cover_url, timeout=3, allow_redirects=True)
                if head_resp.status_code == 200:
                    log_debug(f"[ALBUM_ART] Constructed CAA URL for {artist} - {album}: {cover_url}")
                    return cover_url
            except Exception as e:
                log_debug(f"[ALBUM_ART] CAA HEAD check failed for {cover_url}: {e}")

        # If none of the URLs validated, return the first one anyway and let the downloader handle it.
        if _caa_urls:
            log_debug(f"[ALBUM_ART] Constructed CAA URL for {artist} - {album}: {_caa_urls[0]} (unverified)")
            return _caa_urls[0]
        return None

    except Exception as e:
        log_debug(f"[ALBUM_ART] Failed to fetch album art URL from MusicBrainz: {e}")
        return None


_album_art_pg_schema_ensured = False

# Stable advisory lock key for serialising album_art schema migrations across
# concurrent workers.  The value must be unique within the PostgreSQL instance
# but is otherwise arbitrary — a CRC-32 of the string "album_art_schema_init".
_ALBUM_ART_SCHEMA_ADVISORY_LOCK_KEY = 1986627450


def _ensure_album_art_pg_schema(conn, cursor) -> None:
    """Ensure album_art table exists with correct binary schema and a UNIQUE constraint
    on (artist_name, album_name) for PostgreSQL.  Runs at most once per process lifetime.

    The table may have been created without the UNIQUE constraint in older deployments,
    which causes every ON CONFLICT upsert to fail with
    "there is no unique or exclusion constraint matching the ON CONFLICT specification".

    A PostgreSQL session-level advisory lock is used to serialise concurrent workers
    that all attempt this migration at startup, preventing duplicate-key and deadlock
    errors on pg_class when multiple processes race to CREATE SEQUENCE / CREATE INDEX.
    """
    global _album_art_pg_schema_ensured
    if _album_art_pg_schema_ensured:
        return

    # Acquire a session-level advisory lock so that only one connection runs the
    # migration at a time.  Use the non-blocking variant with retries so a dead
    # lock holder cannot hang this worker forever.
    lock_acquired = False
    for _lock_attempt in range(5):
        try:
            cursor.execute(
                "SELECT pg_try_advisory_lock(%s) AS acquired",
                (_ALBUM_ART_SCHEMA_ADVISORY_LOCK_KEY,),
            )
            if cursor.fetchone()['acquired']:
                lock_acquired = True
                break
        except Exception as _lock_err:
            log_debug(f"[ALBUM_ART] Could not acquire advisory lock: {_lock_err}")
            break
        time.sleep(0.3)
    if not lock_acquired:
        log_debug("[ALBUM_ART] Could not acquire advisory lock; deferring schema migration")
        return

    # Step 1: Ensure table exists and fix the id column default if needed.
    # This sub-step is committed independently so the fix is durable even if
    # the subsequent unique-index creation fails (e.g. due to duplicate rows
    # that haven't been cleaned up yet).
    # _id_fix_needed tracks whether the id column requires a fix; only when it is
    # False (no fix needed / fix succeeded) can Step 2 mark the schema as fully ensured.
    _id_fix_needed = False
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS album_art (
                artist_name TEXT NOT NULL,
                album_name TEXT NOT NULL,
                image_data BYTEA,
                image_mime_type TEXT,
                source TEXT,
                downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Fix: if a legacy 'id' column exists with NOT NULL but no default,
        # the INSERT (which never supplies an id) will always fail with a NOT NULL
        # violation, aborting the shared scan transaction.  Create a sequence and
        # set it as the column default so INSERTs that omit 'id' get an
        # auto-incremented value.  Also handles GENERATED ALWAYS AS IDENTITY
        # columns (identity_generation='ALWAYS') by switching to BY DEFAULT.
        cursor.execute("""
            SELECT column_default, is_nullable, identity_generation
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = 'album_art'
              AND column_name = 'id'
        """)
        _id_info = cursor.fetchone()
        if _id_info is not None:
            _col_default = (_id_info.get('column_default') if hasattr(_id_info, 'get') else _id_info[0])
            _is_nullable = (_id_info.get('is_nullable') if hasattr(_id_info, 'get') else _id_info[1])
            if hasattr(_id_info, 'get'):
                _identity_gen = _id_info.get('identity_generation')
            elif len(_id_info) >= 3:
                _identity_gen = _id_info[2]
            else:
                _identity_gen = None
            if _identity_gen == 'ALWAYS':
                # GENERATED ALWAYS AS IDENTITY blocks plain INSERTs without
                # OVERRIDING SYSTEM VALUE.  Change to BY DEFAULT so that
                # omitting the column still gets an auto-generated value.
                _id_fix_needed = True
                try:
                    cursor.execute(
                        "ALTER TABLE album_art ALTER COLUMN id SET GENERATED BY DEFAULT"
                    )
                    conn.commit()
                    _id_fix_needed = False
                    log_debug("[ALBUM_ART] Changed id from GENERATED ALWAYS to GENERATED BY DEFAULT")
                except Exception as _identity_err:
                    logging.warning(f"[ALBUM_ART] Could not change identity generation to BY DEFAULT: {_identity_err}")
                    try:
                        conn.rollback()
                    except Exception:
                        pass
            elif not _col_default and _is_nullable == 'NO':
                # PostgreSQL does not allow DROP NOT NULL on a PRIMARY KEY column.
                # Instead, create a sequence and set it as the column default so that
                # INSERTs that omit 'id' receive an auto-incremented value.
                # Wrapped in a DO block so concurrent callers that race past the
                # advisory lock handle the duplicate_object error gracefully.
                _id_fix_needed = True
                try:
                    cursor.execute("""
                        DO $$
                        BEGIN
                            CREATE SEQUENCE album_art_id_seq;
                        EXCEPTION WHEN duplicate_object THEN NULL;
                        END $$
                    """)
                    cursor.execute("""
                        SELECT setval(
                            'album_art_id_seq',
                            COALESCE((SELECT MAX(id) FROM album_art WHERE id IS NOT NULL), 0) + 1,
                            false
                        )
                    """)
                    cursor.execute(
                        "ALTER TABLE album_art ALTER COLUMN id SET DEFAULT nextval('album_art_id_seq')"
                    )
                    conn.commit()
                    _id_fix_needed = False
                    log_debug("[ALBUM_ART] Added auto-increment sequence for legacy id column")
                except Exception as _seq_err:
                    logging.warning(f"[ALBUM_ART] Sequence approach failed for legacy id column: {_seq_err}")
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    # Fallback: try ADD GENERATED BY DEFAULT AS IDENTITY
                    try:
                        cursor.execute(
                            "ALTER TABLE album_art ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY"
                        )
                        conn.commit()
                        _id_fix_needed = False
                        log_debug("[ALBUM_ART] Set id column as GENERATED BY DEFAULT AS IDENTITY (fallback)")
                    except Exception as _identity_fb_err:
                        logging.warning(f"[ALBUM_ART] All id-fix approaches failed: {_identity_fb_err}")
                        try:
                            conn.rollback()
                        except Exception:
                            pass
    except Exception as _id_fix_err:
        logging.warning(f"[ALBUM_ART] album_art id-column check/fix failed: {_id_fix_err}")
        try:
            conn.rollback()
        except Exception:
            pass
        # Even if the id-column fix failed we still try to create the unique index
        # below so that ON CONFLICT works.  The id issue will be retried next call.
        _id_fix_needed = True

    # Step 2: Remove duplicates and create unique index.
    # Run in a separate try/except so a failure here does not roll back the
    # id-column fix committed above.
    # Only mark the schema as fully ensured when no id-fix was pending (or it succeeded),
    # so that future calls retry the fix if it failed in Step 1.
    try:
        # Remove duplicate rows (keep the most recently inserted ctid) before
        # adding a unique index, so the CREATE INDEX does not fail on existing data.
        cursor.execute("""
            DELETE FROM album_art a
            USING album_art b
            WHERE a.ctid < b.ctid
              AND a.artist_name = b.artist_name
              AND a.album_name = b.album_name
        """)
        cursor.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_album_art_artist_album
            ON album_art (artist_name, album_name)
        """)
        conn.commit()
        if not _id_fix_needed:
            _album_art_pg_schema_ensured = True
    except Exception as _schema_err:
        log_debug(f"[ALBUM_ART] album_art unique-index ensure failed: {_schema_err}")
        try:
            conn.rollback()
        except Exception:
            pass
    finally:
        if lock_acquired:
            try:
                cursor.execute("SELECT pg_advisory_unlock(%s)", (_ALBUM_ART_SCHEMA_ADVISORY_LOCK_KEY,))
                conn.commit()
            except Exception:
                pass


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
    own_connection = False
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
        if conn is None:
            conn = get_db_connection()
            own_connection = True
        if cursor is None:
            cursor = conn.cursor()

        _ensure_album_art_pg_schema(conn, cursor)
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

        # Only commit if we created our own connection
        if own_connection:
            conn.commit()
            conn.close()


        log_info(f"[ALBUM_ART] Successfully downloaded and saved album art for {artist} - {album} from {source} ({len(image_data)} bytes)")
        return True

    except requests.exceptions.Timeout:  # type: ignore
        log_debug(f"[ALBUM_ART] Timeout downloading image from {source} for {artist} - {album}")
        if conn is not None and not own_connection:
            try:
                conn.rollback()
            except Exception:
                pass
        return False
    except Exception as e:
        log_debug(f"[ALBUM_ART] Failed to download/save album art for {artist} - {album}: {e}")
        # In PostgreSQL, any statement error aborts the current transaction until rollback.
        # Roll back shared scan transactions so subsequent writer/single updates can proceed.
        if conn is not None and not own_connection:
            try:
                conn.rollback()
            except Exception:
                pass
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
    artist_median_popularity: float = 0.0,
    lastfm_client=None,
    existing_conn=None,
    persist_result: bool = True,
    mb_cached_singles: set = None,
) -> dict:
    """
    Detect if a track is a single using multiple data sources.

    This is the canonical single detection logic used by popularity.py.
    Other modules should call this function to ensure consistent behavior.

    NEW: Enhanced with advanced single detection logic including:
    - ISRC-based track version matching
    - Title+duration matching (⏱2 seconds)
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
        spotify_results_cache: Deprecated legacy argument; ignored by scan-side detection
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
    log_info(f"🔎 [SINGLE DETECTION ENTRY] Checking detection options for: {title}")
    log_debug(f"Advanced detection check - use_advanced_detection={use_advanced_detection}, track_id={track_id}, album={album}, title={title}, artist={artist}")
    if use_advanced_detection and track_id and album:
        log_info(f"✅ [SINGLE DETECTION] Using ADVANCED detection path for: {title}")
        conn = None
        owns_connection = existing_conn is None
        try:
            from single_detection_enhanced import detect_single_enhanced, store_single_detection_result
            # get_db_connection is already available in this module
            conn = existing_conn or get_db_connection()

            # Spotify lookups are deprecated and no longer performed during scans.
            spotify_search_results = None

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
            detection_lastfm_client = lastfm_client or get_lastfm_client()

            # Run enhanced detection
            log_info(f"🔍 [SINGLE DETECTION] Starting enhanced detection for: {title}")
            log_debug(f"[SINGLE DETECTION] Enhanced detection params: isrc={isrc}, duration={duration}, popularity={popularity}, album_type={album_type}")
            log_debug(f"[SINGLE DETECTION] API clients available: discogs={'YES' if discogs_client else 'NO'}, musicbrainz={'YES' if musicbrainz_client else 'NO'}, lastfm={'YES' if lastfm_client else 'NO'}")

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
                lastfm_client=detection_lastfm_client,
                verbose=verbose,
                album_type=album_type,
                album_is_underperforming=album_is_underperforming,
                artist_median_popularity=artist_median_popularity,
                mb_cached_singles=mb_cached_singles,
            )

            log_info(f"✅ [SINGLE DETECTION] Enhanced detection complete for: {title}")
            log_debug(f"[SINGLE DETECTION] Result: is_single={result.get('is_single')}, confidence={result.get('single_confidence')}, sources={result.get('single_sources')}")

            if persist_result:
                # CRITICAL: Close owned read connection before storing results.
                # detect_single_enhanced() creates multiple cursors and may leave read locks open.
                # Close the read connection, then use a fresh connection for writes.
                try:
                    if owns_connection and conn is not None:
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
            if not owns_connection and conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass
            # Fall through to standard detection
        except Exception as e:
            if verbose:
                log_unified(f"   âš  Enhanced detection failed, falling back to standard: {e}")
            import traceback
            if verbose:
                log_unified(f"   Error details: {traceback.format_exc()}")
            if not owns_connection and conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass
            # Fall through to standard detection
        finally:
            if owns_connection and conn is not None:
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
        log_info(f"⚠️ [SINGLE DETECTION] Using STANDARD detection path for: {title} (reasons: {', '.join(skip_reason)})")
        log_debug(f"Skipping advanced detection for {title}: {', '.join(skip_reason)}")

    # Ignore obvious non-singles by keywords
    # Strip cover attributions AND single-release version suffixes so that
    # "(Radio Edit)" / "(Single Version)" / "(Album Version)" are not caught
    # by the "edit" keyword and incorrectly excluded from single detection.
    base_title = strip_single_release_suffix(strip_cover_attribution(title))
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
            placeholder = "%s"
            cursor = conn.cursor()

            # STAGE 1: Album-level filter (must be album standout)
            # Skip this filter for compilations and greatest hits albums (all tracks are hits)
            is_compilation_or_greatest_hits = (
                album_type in ["various_artists", "greatest_hits", "compilation"] or
                is_compilation_type(album_type)
            )

            if is_compilation_or_greatest_hits:
                if verbose:
                    log_verbose(f"   ⓘ Skipping album popularity filter for compilation/greatest hits album")
            else:
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
            try:
                if conn is not None:
                    conn.rollback()
            except Exception:
                pass
            if verbose:
                log_verbose(f"   âš  Could not calculate album mean for popularity filter: {e}")
            # Continue with detection if we can't calculate album mean

    single_sources = []
    medium_confidence_sources = []  # Track medium confidence sources for 2 medium = 1 high rule

    # Normalize lookup title before external API searches.
    # This removes parenthetical release qualifiers so variants like
    # "Song (live at ...)", "Song (remastered 2024)", or "Song (radio edit)"
    # can resolve against the canonical song entry.
    lookup_title = normalize_title_for_lookup(title)

    # Load discogs token and feature settings from config
    mb_compilation_confidence = "medium"
    _feature_config = {}
    try:
        config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")
        with open(config_path, 'r') as f:
            _cfg = yaml.safe_load(f)
        if discogs_token is None:
            discogs_token = _cfg.get("api_integrations", {}).get("discogs", {}).get("token", "")
            if discogs_token and verbose:
                log_unified(f"   ✔ Loaded Discogs token from config.yaml")
        _feature_config = _cfg.get("features", {}) if isinstance(_cfg, dict) else {}
    except Exception as e:
        if discogs_token is None:
            discogs_token = ""
            # Always log config loading errors, not just in verbose mode
            log_unified(f"   ⚠  Could not load Discogs token from config at {config_path}: {e}")
    _raw_mb_comp_conf = _feature_config.get("source_musicbrainz_compilation_confidence", "medium")
    if isinstance(_raw_mb_comp_conf, str) and _raw_mb_comp_conf.lower() in ("high", "medium", "low"):
        mb_compilation_confidence = _raw_mb_comp_conf.lower()

    # First active check: MusicBrainz single detection
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
                    lookup_title, artist, None, album_track_count
                )
                if result:
                    single_sources.append("musicbrainz")
                    medium_confidence_sources.append("musicbrainz")
                    log_info(f"   âœ“ MusicBrainz confirms single: {title}")
                else:
                    log_info(f"   â“˜ MusicBrainz does not confirm single: {title}")

                # Check for Various Artists appearances
                try:
                    on_compilations = _run_with_timeout(
                        mb_client.appears_on_various_artists,
                        API_CALL_TIMEOUT,
                        f"MusicBrainz compilation check timed out after {API_CALL_TIMEOUT}s",
                        lookup_title, artist
                    )
                    if on_compilations:
                        single_sources.append("musicbrainz_compilation")
                        if mb_compilation_confidence == "high":
                            log_info(f"   ✅ MusicBrainz: Track appears on multiple compilation albums (HIGH confidence): {title}")
                            return {
                                "sources": list(dict.fromkeys(single_sources)),
                                "confidence": "high",
                                "is_single": True
                            }
                        elif mb_compilation_confidence == "medium":
                            medium_confidence_sources.append("musicbrainz_compilation")
                            log_info(f"   ✅ MusicBrainz: Track appears on multiple compilation albums: {title}")
                        else:
                            log_info(f"   ✅ MusicBrainz: Track appears on multiple compilation albums (low confidence, not counting toward promotion): {title}")
                except TimeoutError:
                    log_debug(f"   ⏱ MusicBrainz compilation check timed out for {title}")
                except Exception as e:
                    log_debug(f"   MusicBrainz compilation check error for {title}: {e}")

                # Check if 2 medium sources = high confidence (early exit)
                if len(medium_confidence_sources) >= 2:
                    log_info(f"   🎯 EARLY EXIT: 2 medium sources detected ({medium_confidence_sources}), promoting to HIGH")
                    return {
                        "sources": list(dict.fromkeys(single_sources)),
                        "confidence": "high",
                        "is_single": True
                    }
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
            log_debug(f"   Discogs API: Searching for single '{lookup_title}' by '{artist}'")
            # Use timeout-safe client to prevent retries from exceeding timeout
            discogs_client = _get_timeout_safe_discogs_client(discogs_token)
            if discogs_client:
                result = _run_with_timeout(
                    lambda: discogs_client.is_single(lookup_title, artist, album_context=None),
                    API_CALL_TIMEOUT,
                    f"Discogs single detection timed out after {API_CALL_TIMEOUT}s"
                )
                if result:
                    single_sources.append("discogs")
                    log_info(f"   âœ“ Discogs confirms single: {title}")
                    log_debug(f"   Discogs result: Single confirmed for '{lookup_title}'")
                else:
                    log_info(f"   â“˜ Discogs does not confirm single: {title}")
                    log_debug(f"   Discogs result: No single found for '{lookup_title}'")
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
            log_debug(f"   Discogs API: Searching for music video '{lookup_title}' by '{artist}'")
            # Use timeout-safe client to prevent retries from exceeding timeout
            discogs_client = _get_timeout_safe_discogs_client(discogs_token)
            if discogs_client:
                result = _run_with_timeout(
                    lambda: discogs_client.has_official_video(lookup_title, artist),
                    API_CALL_TIMEOUT,
                    f"Discogs video detection timed out after {API_CALL_TIMEOUT}s"
                )
                if result:
                    single_sources.append("discogs_video")
                    medium_confidence_sources.append("discogs_video")
                    log_info(f"   ✓ Discogs confirms music video: {title}")
                    log_debug(f"   Discogs result: Music video confirmed for '{lookup_title}'")
                else:
                    log_info(f"   â“˜ Discogs does not confirm music video: {title}")
                    log_debug(f"   Discogs result: No music video found for '{lookup_title}'")
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
                # NOTE: Z-score is a popularity metric, NOT a confidence indicator
                # It detects statistical outliers but doesn't confirm single release status
                log_info(f"   Iterative z-score method: {title} passed album standout test")
            else:
                log_debug(f"   Iterative z-score: {title} did not meet threshold")
        except Exception as e:
            log_debug(f"   Iterative z-score detection error for {title}: {e}")

    # Calculate confidence based on sources.
    # Discogs is the only high-confidence source; all other sources are medium.
    has_iterative_zscore = "iterative_zscore" in single_sources
    has_discogs_single = "discogs" in single_sources
    has_discogs_video = "discogs_video" in single_sources
    has_other_sources = any(s in single_sources for s in ["spotify", "musicbrainz", "lastfm", "radio_edit"])

    # NEW RULE: 2 medium sources = high confidence
    if has_discogs_single or len(medium_confidence_sources) >= 2:
        single_confidence = "high"
    elif has_other_sources or has_discogs_video:
        single_confidence = "medium"
    else:
        single_confidence = "low"

    # Album context rule: downgrade medium -> low if album has >3 tracks
    if single_confidence == "medium" and album_track_count > 3:
        single_confidence = "low"
        if verbose:
            log_verbose(f"   Downgraded {title} confidence to low (album has {album_track_count} tracks)")

    # is_single = True only for high confidence singles (Discogs-confirmed)
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


def get_artist_lastfm_context(artist_name: str, conn: object, artist_mbid: str = None) -> dict:
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
        placeholder = "%s"

        # Ensure the caching columns exist in the artists table (idempotent, run once per startup).
        try:
            cursor.execute("ALTER TABLE artists ADD COLUMN IF NOT EXISTS artist_lastfm_context_json TEXT")
            cursor.execute("ALTER TABLE artists ADD COLUMN IF NOT EXISTS artist_context_cached_at TIMESTAMPTZ")
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass

        # --- DB cache check: skip expensive API calls if fresh data is available ---
        _CONTEXT_CACHE_TTL_DAYS = 7
        try:
            cursor.execute(f"""
                SELECT artist_lastfm_context_json, artist_context_cached_at
                FROM artists
                WHERE name = {placeholder}
                LIMIT 1
            """, (artist_name,))
            _ctx_row = cursor.fetchone()
            if _ctx_row:
                _ctx_json = row_get(_ctx_row, 'artist_lastfm_context_json')
                _ctx_cached_at = row_get(_ctx_row, 'artist_context_cached_at')
                if _ctx_json and _ctx_cached_at:
                    from datetime import datetime as _dt2, timedelta as _td2, timezone as _tz2
                    if isinstance(_ctx_cached_at, str):
                        _ctx_cached_at = _dt2.fromisoformat(_ctx_cached_at.replace('Z', '+00:00'))
                    if _ctx_cached_at.tzinfo is not None:
                        _ctx_age_days = (_dt2.now(_tz2.utc) - _ctx_cached_at).days
                    else:
                        _ctx_age_days = (_dt2.now() - _ctx_cached_at).days
                    if _ctx_age_days < _CONTEXT_CACHE_TTL_DAYS:
                        cached_ctx = json.loads(_ctx_json)
                        # track_zscores keys were serialised as strings; convert back to int
                        # to match the dict[int, float] type expected by callers.
                        if 'track_zscores' in cached_ctx and isinstance(cached_ctx['track_zscores'], dict):
                            try:
                                cached_ctx['track_zscores'] = {int(k): v for k, v in cached_ctx['track_zscores'].items()}
                            except (ValueError, TypeError):
                                pass  # Keys may already be ints in some serialisation formats
                        log_debug(f"Using cached artist_lastfm_context for '{artist_name}' ({_ctx_age_days}d old, TTL={_CONTEXT_CACHE_TTL_DAYS}d)")
                        # End the cache SELECT transaction before returning so
                        # the shared connection does not remain idle-in-transaction
                        # during the subsequent per-artist API calls in the caller.
                        try:
                            conn.commit()
                        except Exception:
                            pass
                        return cached_ctx
        except Exception as _ctx_cache_err:
            log_debug(f"Could not read artist_lastfm_context cache for '{artist_name}': {_ctx_cache_err}")
        # --- end cache check ---

        # Get all tracks by artist with Last.fm listener data
        # Exclude live/remix/alternate versions to avoid skewing stats
        is_single_false_expr = "is_single = FALSE"
        cursor.execute(f"""
            SELECT id, title, album, lastfm_track_playcount
            FROM tracks
            WHERE artist = {placeholder} AND lastfm_track_playcount > 0 AND {is_single_false_expr}
                AND album NOT IN (
                    SELECT DISTINCT album FROM tracks WHERE artist = {placeholder} AND album_context_live = 1
                )
                AND album NOT IN (
                    SELECT DISTINCT album FROM tracks WHERE artist = {placeholder} AND discogs_format_descriptions LIKE {placeholder}
                )
        """, (artist_name, artist_name, artist_name, '%live%'))

        tracks = cursor.fetchall()
        listeners_list = [row_get(row, 'lastfm_track_playcount', 0) for row in tracks if row_get(row, 'lastfm_track_playcount', 0) > 0]

        # End the SELECT transaction before the outbound API calls so the shared
        # connection does not remain idle-in-transaction during network I/O.
        try:
            conn.commit()
        except Exception:
            pass

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
                'mean': 0,
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
            track_id = row_get(track_row, 'id')
            title = row_get(track_row, 'title', '')
            listeners = row_get(track_row, 'lastfm_track_playcount', 0)

            if artist_stdev > 0:
                z = (listeners - artist_mean) / artist_stdev
                track_zscores[track_id] = z
                # Log tracks that are significant outliers (for debugging)
                if abs(z) >= 2.0:
                    in_top_10 = "✓ in top 10%" if listeners >= top_10_percentile_threshold else "✗ not in top 10%"
                    log_debug(f"Artist outlier detected: {title} (z={z:.2f}, listeners={listeners:.0f}, artist_mean={artist_mean:.0f}, {in_top_10})")

        context_result = {
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

        # Persist computed context to artists table so future calls within the TTL window
        # skip the expensive DB scan + Last.fm API requests entirely.
        if listeners_list:
            try:
                _ctx_serialisable = dict(context_result)
                # Convert track_zscores keys to str for safe JSON serialization
                _ctx_serialisable['track_zscores'] = {str(k): v for k, v in track_zscores.items()}
                _ctx_json_str = json.dumps(_ctx_serialisable)
                cursor.execute(f"""
                    INSERT INTO artists (id, name, artist_lastfm_context_json, artist_context_cached_at)
                    VALUES ({placeholder}, {placeholder}, {placeholder}, CURRENT_TIMESTAMP)
                    ON CONFLICT (name) DO UPDATE SET
                        artist_lastfm_context_json = EXCLUDED.artist_lastfm_context_json,
                        artist_context_cached_at = EXCLUDED.artist_context_cached_at
                """, (artist_name, artist_name, _ctx_json_str))
                conn.commit()
                log_debug(f"Cached artist_lastfm_context for '{artist_name}' in artists table (TTL={_CONTEXT_CACHE_TTL_DAYS}d)")
            except Exception as _write_err:
                log_debug(f"Could not cache artist_lastfm_context for '{artist_name}': {_write_err}")
                try:
                    conn.rollback()
                except Exception:
                    pass

        return context_result

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


def get_dynamic_lastfm_weight(
    artist_context: dict,
    track_lastfm_listeners: int,
    base_lastfm_weight: float = 0.3,
) -> float:
    """Adjust the active Last.fm weight based on artist-catalogue outlier context."""
    try:
        artist_mean = artist_context.get('mean', 0)
        artist_stdev = artist_context.get('stdev', 0)

        if artist_stdev == 0 or artist_mean == 0 or track_lastfm_listeners <= 0:
            return base_lastfm_weight

        track_zscore = (track_lastfm_listeners - artist_mean) / artist_stdev
        abs_zscore = abs(track_zscore)

        if abs_zscore >= 2.0 and track_lastfm_listeners > artist_mean * 1.5:
            adjustment = max(1.0, 1.5 - abs(track_zscore) * 0.05)
            new_lastfm = min(0.6, base_lastfm_weight * adjustment)
            log_debug(
                f"Outlier boost (above mean): Last.fm weight {base_lastfm_weight:.2f} → {new_lastfm:.2f} "
                f"(z={track_zscore:.2f})"
            )
            return new_lastfm

        return base_lastfm_weight

    except Exception as e:
        log_debug(f"Error calculating dynamic Last.fm weight: {e}")
        return base_lastfm_weight


def popularity_scan(
    verbose: bool = False,
    resume_from: str = None,
    artist_filter: str = None,
    album_filter: str = None,
    skip_header: bool = False,
    force: bool = False,
    filter_missing: bool = False,
    singles_only: bool = False,
    singles_with_missing_popularity: bool = False,
    popularity_only: bool = False,
    metadata_only: bool = False,
    clear_single_detection_sources: list = None,
    stop_progress_file: str = None,
    caller_scan_type: str = None,
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
        singles_with_missing_popularity: Run singles detection for all albums; only fetch popularity
                                         data from external sources for albums that have no existing
                                         popularity scores in the database.
        popularity_only: Run popularity scoring only; skip singles detection/star assignment.
        metadata_only: Run metadata enrichment lookups only (tags/writer/album context/missing track checks);
                   skip popularity scoring, singles detection, and star assignments.
        clear_single_detection_sources: List of sources to clear from cache (e.g., ['discogs', 'spotify'])
                                       If force=True, all sources are cleared automatically
        stop_progress_file: Optional progress file path used to cooperatively stop an in-flight scan
        caller_scan_type: When set, use this scan_type in progress writes and skip the final
                          is_running=False write (the calling scan manages the progress file lifecycle)
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

    if metadata_only:
        progress_scan_type = "metadata_lookup_scan"
    elif singles_only or singles_with_missing_popularity:
        progress_scan_type = "singles_scan"
    else:
        progress_scan_type = "popularity_scan"
    progress_file_path = stop_progress_file or POPULARITY_PROGRESS_FILE
    metadata_enrichment_enabled = bool(metadata_only)

    if not skip_header:
        log_unified("Popularity Scan - Starting Popularity Scan")
        log_info("=" * 60)
        log_info("Popularity Scanner Started")
        log_info("=" * 60)
        log_info(f"Popularity scan started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        log_debug(f"Popularity scan params - verbose: {verbose}, resume: {resume_from}, artist: {artist_filter}, album: {album_filter}, force: {force}, filter_missing: {filter_missing}, singles_only: {singles_only}, singles_with_missing_popularity: {singles_with_missing_popularity}")

    # Log scan mode details to info
    if singles_only:
        log_info("Singles-only mode enabled - will only rescan singles detection")
    elif singles_with_missing_popularity:
        log_info("Singles scan mode enabled - will run popularity scan only for albums with no existing popularity data")
    elif popularity_only:
        log_info("Popularity-only mode enabled - will score popularity and skip singles/star stages")
    elif metadata_only:
        log_info("Metadata-only mode enabled - will run metadata lookups without popularity/singles/star scoring")
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
            cache_placeholder = "%s"

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

    if not metadata_only and not singles_only:
        # Initialize popularity helpers for scan-time weighting logic.
        # Force Spotify disabled for scans to avoid any Spotify client/API usage.
        from popularity_helpers import configure_popularity_helpers
        try:
            helper_config = {}
            helper_config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")
            try:
                with open(helper_config_path, 'r') as helper_config_file:
                    helper_config = yaml.safe_load(helper_config_file) or {}
            except Exception:
                helper_config = {}

            if not isinstance(helper_config, dict):
                helper_config = {}

            api_integrations = helper_config.get("api_integrations")
            if not isinstance(api_integrations, dict):
                api_integrations = {}
                helper_config["api_integrations"] = api_integrations

            spotify_cfg = api_integrations.get("spotify")
            if not isinstance(spotify_cfg, dict):
                spotify_cfg = {}
            spotify_cfg["enabled"] = False
            api_integrations["spotify"] = spotify_cfg

            configure_popularity_helpers(config=helper_config)
            if not skip_header:
                log_info("Popularity helpers configured successfully")
            log_debug("Popularity helper configuration complete")
        except Exception as e:
            log_info(f"Warning: Failed to configure popularity helpers: {e}")
            import traceback
            log_debug(f"Configuration error details: {traceback.format_exc()}")

    log_debug("Connecting to database for popularity scan...")
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # Determine database type for proper placeholder syntax
        placeholder = "%s"

        # Load configuration from config.yaml
        # Initialize config to empty dict to ensure it's always defined
        config = {}
        album_skip_days = 7  # Default value
        try:
            config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f)
            features = config.get('features', {})
            album_skip_days = features.get('album_skip_days', 7)
            log_debug(f"Configuration loaded - album_skip_days: {album_skip_days}")
            log_info(f"Album skip days: {album_skip_days} (albums scanned within {album_skip_days} days will be skipped)")
        except Exception as e:
            log_debug(f"Could not load scan config (using defaults): {e}")

        # Build SQL query with optional filters
        sql_conditions = []
        sql_params = []

        # Normalize last_scanned to timestamptz for mixed schemas where the
        # column may be stored as text in legacy databases.
        last_scanned_ts_expr = (
            "CASE "
            "WHEN last_scanned IS NULL OR TRIM(CAST(last_scanned AS TEXT)) = '' THEN NULL "
            "WHEN CAST(last_scanned AS TEXT) ~ '^\\\\d{4}-\\\\d{2}-\\\\d{2}' THEN CAST(CAST(last_scanned AS TEXT) AS TIMESTAMPTZ) "
            "ELSE NULL END"
        )

        # Never scan placeholder queue rows created before files are imported.
        # Use placeholders so PostgreSQL does not misinterpret raw '%' characters
        # when parameters are bound.
        sql_conditions.append(f"COALESCE(file_path, '') NOT LIKE {placeholder}")
        sql_params.append('__queued_for_download__%')
        sql_conditions.append(f"CAST(id AS TEXT) NOT LIKE {placeholder}")
        sql_params.append('queue_%')

        # Only filter by popularity_score if not forcing rescan
        if not (FORCE_RESCAN or force) and not metadata_only and not singles_only and not singles_with_missing_popularity:
            sql_conditions.append(
                f"(popularity_score IS NULL OR popularity_score = 0 OR {last_scanned_ts_expr} IS NULL OR {last_scanned_ts_expr} < (NOW() - ({placeholder} * INTERVAL '1 day')))"
            )
            sql_params.append(max(1, int(album_skip_days)))

        if not (FORCE_RESCAN or force) and metadata_only:
            sql_conditions.append(
                f"(" \
                f"{last_scanned_ts_expr} IS NULL OR {last_scanned_ts_expr} < (NOW() - ({placeholder} * INTERVAL '1 day')) OR " \
                f"COALESCE(NULLIF(mbid, ''), '') = '' OR COALESCE(NULLIF(musicbrainz_album_mbid, ''), '') = '' OR " \
                f"writer IS NULL OR TRIM(CAST(writer AS TEXT)) IN ('', '[]', 'null', 'None') OR " \
                f"lastfm_tags IS NULL OR TRIM(CAST(lastfm_tags AS TEXT)) = '' OR " \
                f"listenbrainz_genres IS NULL OR TRIM(CAST(listenbrainz_genres AS TEXT)) = '' OR " \
                f"discogs_genres IS NULL OR TRIM(CAST(discogs_genres AS TEXT)) = '' OR " \
                f"musicbrainz_genres IS NULL OR TRIM(CAST(musicbrainz_genres AS TEXT)) = ''" \
                f")"
            )
            sql_params.append(max(1, int(album_skip_days)))

        if artist_filter:
            # Artist scans should only include albums owned by that album artist.
            # Fall back to track artist only when album_artist is missing.
            sql_conditions.append(f"(COALESCE(NULLIF(album_artist, ''), artist) = {placeholder})")
            sql_params.append(artist_filter)

        if album_filter and artist_filter:
            sql_conditions.append(f"album = {placeholder}")
            sql_params.append(album_filter)

        select_clause = (
            "SELECT id, artist, title, album, isrc, duration, spotify_album_type, track_number, mbid, year, "
            "spotify_popularity, spotify_score, lastfm_track_playcount, lastfm_ratio, last_spotify_lookup, "
            "popularity_score, album_artist, writer, spotify_genres, lastfm_tags, "
            "listenbrainz_genres, discogs_genres, musicbrainz_genres, cover_art_url, "
            "is_live, is_acoustic, is_remix, is_cover, musicbrainz_albumtype, discogs_release_id, "
            "album_context_live, file_path, genres"
        )
        where_clause = f" WHERE {' AND '.join(sql_conditions)}" if sql_conditions else ""
        # Order by album_artist (falling back to track artist when absent) so that the
        # dict insertion order of artist_album_tracks matches the album-artist grouping
        # key.  Using only "artist" caused "Various Artists" compilations to sort at
        # the position of the first individual track artist (e.g. "ABBA") rather than
        # alphabetically at "V", making the scan spend a long time on VA before
        # advancing to regular artists — and causing resume to mis-position the
        # checkpoint when the scan was restarted.
        sql = f"{select_clause} FROM tracks{where_clause} ORDER BY COALESCE(NULLIF(album_artist, ''), artist), album, title"

        log_debug(f"Executing SQL: {sql.strip()} with params: {sql_params}")
        cursor.execute(sql, sql_params)

        tracks_raw = cursor.fetchall()
        # Convert row objects to dictionaries to allow item assignment
        tracks = [dict(row) for row in tracks_raw]
        log_info(f"Found {len(tracks)} tracks to scan for popularity")
        log_debug(f"Fetched {len(tracks)} tracks from database")
        # Commit the SELECT transaction immediately so the connection transitions
        # to "idle" (not "idle in transaction") before the per-artist API calls
        # begin.  Without this, PostgreSQL can kill the connection with
        # idle_in_transaction_session_timeout during the minutes-long API loop.
        conn.commit()

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
            log_unified("Popularity Scan - No tracks found. All tracks may already have popularity data (run in Forced mode to rescan).")
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
        try:
            config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f)
            if config.get("api_integrations", {}).get("lastfm", {}).get("api_key"):
                enabled_apis.append("Last.FM")
            if config.get("api_integrations", {}).get("listenbrainz", {}).get("token"):
                enabled_apis.append("ListenBrainz")
            if config.get("api_integrations", {}).get("discogs", {}).get("enabled", False):
                enabled_apis.append("Discogs")
            if config.get("api_integrations", {}).get("musicbrainz", {}).get("enabled", True):
                enabled_apis.append("MusicBrainz")
        except (FileNotFoundError, yaml.YAMLError, KeyError, AttributeError) as e:
            log_debug(f"Could not load API configuration: {e}")
            enabled_apis.extend(["Last.FM", "Discogs", "MusicBrainz"])

        mature_track_freeze_cutoff_years = get_mature_track_freeze_cutoff_years(config, default_years=2)

        if enabled_apis:
            log_unified(f"Popularity Scan - Scanning {', '.join(enabled_apis)} for Metadata")
            log_debug(f"Enabled APIs: {enabled_apis}")
        log_debug(f"Mature track popularity freeze cutoff: {mature_track_freeze_cutoff_years} year(s)")

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

            # Mark this artist as in-progress immediately so that if the scan is
            # interrupted during album processing the checkpoint correctly points to
            # this artist rather than the previously completed one.
            save_popularity_progress(
                processed_artists,
                total_artists,
                current_artist=artist,
                progress_file=progress_file_path,
                scan_type=caller_scan_type or progress_scan_type,
            )
            log_debug(f"In-progress checkpoint saved for artist: {artist}")

            # Get artist MBID from database cache for Last.fm context enrichment
            artist_mbid = None
            try:
                cursor.execute(f"""
                    SELECT musicbrainz_artist_id
                    FROM tracks
                    WHERE artist = {placeholder} AND musicbrainz_artist_id IS NOT NULL
                    ORDER BY LENGTH(musicbrainz_artist_id) ASC
                    LIMIT 1
                """, (artist,))
                row = cursor.fetchone()
                if row and row_get(row, 'musicbrainz_artist_id'):
                    artist_mbid = row_get(row, 'musicbrainz_artist_id')
                    log_debug(f"Using cached MusicBrainz artist ID for {artist}: {artist_mbid}")
            except Exception as e:
                log_debug(f"Failed to get cached MusicBrainz artist ID for {artist}: {e}")

            # Pre-fetch artist's Last.fm context for dynamic weight adjustment
            # This allows us to boost Last.fm weight for tracks that are outliers in the artist's catalogue
            artist_lastfm_context = get_artist_lastfm_context(artist, conn, artist_mbid)
            if artist_lastfm_context.get('track_count', 0) > 0:
                context_mean = float(artist_lastfm_context.get('mean', 0) or 0)
                context_stdev = float(artist_lastfm_context.get('stdev', 0) or 0)
                context_min = float(artist_lastfm_context.get('min', 0) or 0)
                context_max = float(artist_lastfm_context.get('max', 0) or 0)
                log_info(f"Artist Last.fm context: {artist_lastfm_context.get('track_count', 0)} tracks, mean={context_mean:.0f} listeners, stdev={context_stdev:.0f}")
                log_debug(f"Artist catalogue range: {context_min:.0f} - {context_max:.0f} listeners")
            else:
                log_debug(f"No Last.fm listener data available for artist {artist} - will use base weights")

            # Spotify popularity lookups are deprecated and removed from popularity scanning.
            is_compilation_group = artist.lower() in ('various artists', 'various artists -', 'various', 'compilation', 'soundtrack')
            if not is_compilation_group and not singles_only and not singles_with_missing_popularity:
                # Fetch and update Discogs artist ID from Discogs API during popularity scan.
                # This remains useful metadata even after Spotify popularity removal.
                try:
                    from popularity_helpers import update_discogs_artist_id_for_artist
                    from api_clients.discogs import DiscogsClient

                    discogs_config = config.get("api_integrations", {}).get("discogs", {})
                    if discogs_config.get("enabled") and discogs_config.get("token"):
                        try:
                            discogs_client = DiscogsClient(token=discogs_config.get("token"))
                            discogs_artist_id = _run_with_timeout(
                                discogs_client.get_artist_id,
                                12,
                                f"Discogs artist ID lookup timed out after 12s",
                                artist
                            )

                            if discogs_artist_id:
                                log_info(f'Discogs artist ID found: {artist} -> {discogs_artist_id}')
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
                log_info(f"Skipping artist-ID lookup for compilation album group: {artist}")

            # Fetch and update artist metadata (country, bio, image) for ALL artists
            # This is independent of Spotify lookup success and applies to all artists
            def _get_discogs_bio_from_saved_artist_id() -> str:
                """Fetch artist bio from Discogs using the saved discogs_artist_id when available."""
                try:
                    discogs_cfg = config.get("api_integrations", {}).get("discogs", {})
                    discogs_token_local = discogs_cfg.get("token", "")
                    if not (discogs_cfg.get("enabled") and discogs_token_local):
                        return ""

                    cursor.execute(
                        f"""
                            SELECT MAX(NULLIF(TRIM(CAST(discogs_artist_id AS TEXT)), '')) AS discogs_id
                            FROM tracks
                            WHERE COALESCE(NULLIF(album_artist, ''), artist) = {placeholder}
                        """,
                        (artist,)
                    )
                    discogs_id_row = cursor.fetchone()
                    saved_discogs_id = str(row_get(discogs_id_row, 'discogs_id', '')).strip() if discogs_id_row else ""
                    if not saved_discogs_id:
                        return ""

                    # End the SELECT transaction before the Discogs API call so
                    # the shared connection is not idle-in-transaction during network I/O.
                    try:
                        conn.commit()
                    except Exception:
                        pass

                    discogs_client = _get_timeout_safe_discogs_client(discogs_token_local)
                    if not discogs_client:
                        return ""

                    discogs_bio_data = _run_with_timeout(
                        discogs_client.get_artist_biography_by_id,
                        8,
                        "Discogs artist bio lookup by ID timed out after 8s",
                        saved_discogs_id
                    )
                    discogs_bio = (discogs_bio_data or {}).get("profile", "")
                    if discogs_bio:
                        log_info(f"Saved artist bio from Discogs for {artist} using artist_id {saved_discogs_id} ({len(discogs_bio)} chars)")
                    return discogs_bio or ""
                except Exception as discogs_bio_err:
                    log_debug(f"Discogs bio lookup by saved ID failed for {artist}: {discogs_bio_err}")
                    return ""

            try:
                if HAVE_MUSICBRAINZ and not singles_only and not singles_with_missing_popularity:
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
                            ON CONFLICT(name) DO UPDATE SET country = excluded.country
                        """, (artist, artist, artist_country))

                        # Update tracks table with artist country
                        cursor.execute(f"UPDATE tracks SET artist_country = {placeholder} WHERE COALESCE(album_artist, artist) = {placeholder}",
                                     (artist_country, artist))

                        # Also backfill releasecountry for tracks that have no
                        # release country from their file tags, so Navidrome's
                        # smart-playlist "Release Country" field is populated.
                        cursor.execute(
                            f"UPDATE tracks SET releasecountry = {placeholder}"
                            f" WHERE COALESCE(album_artist, artist) = {placeholder}"
                            f"   AND (releasecountry IS NULL OR TRIM(releasecountry) = '')",
                            (artist_country, artist)
                        )
                        conn.commit()
                        log_debug(f'Updated artist country in database: {artist} -> {artist_country}')
                    else:
                        log_debug(f'No country information found for artist: {artist}')

                    # Fetch and save artist bio/image from AudioDB during scan.
                    # Skip the network round-trip when both fields are already stored.
                    _existing_artist_bio = ""
                    _existing_artist_image = ""
                    try:
                        cursor.execute(
                            f"SELECT bio, image_url FROM artists WHERE id = {placeholder}",
                            (artist,)
                        )
                        _ea = cursor.fetchone()
                        _existing_artist_bio = row_get(_ea, 'bio', '') if _ea else ''
                        _existing_artist_image = row_get(_ea, 'image_url', '') if _ea else ''
                    except Exception:
                        pass
                    # End the SELECT transaction before outbound API calls so the
                    # connection does not sit idle-in-transaction during network I/O.
                    try:
                        conn.commit()
                    except Exception:
                        pass

                    if HAVE_AUDIODB and not (_existing_artist_bio and _existing_artist_image):
                        try:
                            log_debug(f'Fetching artist bio and image from AudioDB for: {artist}')

                            # Fetch artist biography only when missing
                            artist_bio = _existing_artist_bio or _run_with_timeout(
                                get_artist_biography,
                                8,  # 8 second timeout for bio lookup
                                f"Artist bio lookup timed out after 8s",
                                artist,
                                enabled=True
                            )

                            # Fetch artist image/fanart only when missing
                            artist_image = _existing_artist_image or _run_with_timeout(
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
                                    ON CONFLICT(name) DO UPDATE SET
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
                                # Prefer Discogs biography by saved artist ID, then fall back to Last.fm.
                                try:
                                    discogs_bio = _get_discogs_bio_from_saved_artist_id()
                                    lastfm_bio = ""
                                    final_image = ""

                                    lastfm_config = get_lastfm_config(config)
                                    if (not discogs_bio) and lastfm_config.get("enabled") and lastfm_config.get("api_key"):
                                        from api_clients.lastfm import LastFmClient
                                        lastfm_client = LastFmClient(lastfm_config.get("api_key"))

                                        artist_info = _run_with_timeout(
                                            lastfm_client.get_artist_info,
                                            8,
                                            "Last.fm artist info lookup timed out after 8s",
                                            artist
                                        )
                                        lastfm_bio = artist_info.get("bio", "") or artist_info.get("bio_text", "")
                                        final_image = artist_info.get("image", "") or ""

                                    selected_bio = discogs_bio or lastfm_bio
                                    if selected_bio or final_image:
                                        cursor.execute(f"""
                                            INSERT INTO artists (id, name, bio, image_url)
                                            VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder})
                                            ON CONFLICT(name) DO UPDATE SET
                                                bio = excluded.bio,
                                                image_url = excluded.image_url
                                        """, (artist, artist, selected_bio or "", final_image or ""))
                                        conn.commit()

                                        if selected_bio:
                                            source = "Discogs" if discogs_bio else "Last.fm"
                                            log_info(f"Saved artist bio from {source} for {artist} ({len(selected_bio)} chars)")
                                        if final_image:
                                            log_info(f"Saved artist image URL from Last.fm for {artist}: {final_image[:60]}...")
                                    else:
                                        log_debug(f'No bio or image found from Discogs/Last.fm for artist: {artist}')
                                except Exception as e:
                                    log_debug(f"Discogs/Last.fm fallback failed for {artist}: {e}")
                        except TimeoutError as e:
                            log_debug(f"Artist bio/image lookup timed out for {artist}: {e}")
                            # Still try Discogs first, then Last.fm as fallback on timeout.
                            try:
                                discogs_timeout_bio = _get_discogs_bio_from_saved_artist_id()
                                selected_bio = discogs_timeout_bio
                                source = "Discogs"
                                if not selected_bio:
                                    lastfm_config = get_lastfm_config(config)
                                    if lastfm_config.get("enabled") and lastfm_config.get("api_key"):
                                        from api_clients.lastfm import LastFmClient
                                        lastfm_client = LastFmClient(lastfm_config.get("api_key"))
                                        artist_info = lastfm_client.get_artist_info(artist)
                                        selected_bio = artist_info.get("bio", "") or artist_info.get("bio_text", "")
                                        source = "Last.fm"

                                if selected_bio:
                                    cursor.execute(f"""
                                        INSERT INTO artists (id, name, bio)
                                        VALUES ({placeholder}, {placeholder}, {placeholder})
                                        ON CONFLICT(name) DO UPDATE SET bio = excluded.bio
                                    """, (artist, artist, selected_bio))
                                    conn.commit()
                                    log_info(f'Saved artist bio from {source} for {artist} (AudioDB timed out)')
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
            if not HAVE_MUSICBRAINZ and not singles_only and not singles_with_missing_popularity:
                try:
                    # Check what (if anything) is already stored so we don't re-fetch.
                    _fb_bio = ""
                    _fb_image = ""
                    try:
                        cursor.execute(
                            f"SELECT bio, image_url FROM artists WHERE id = {placeholder}",
                            (artist,)
                        )
                        _fb_ea = cursor.fetchone()
                        _fb_bio = row_get(_fb_ea, 'bio', '') if _fb_ea else ''
                        _fb_image = row_get(_fb_ea, 'image_url', '') if _fb_ea else ''
                    except Exception:
                        pass

                    if _fb_bio and _fb_image:
                        log_debug(f"Artist bio and image already in DB for {artist} (MusicBrainz unavailable path), skipping fetch")
                    else:
                        log_debug(f"MusicBrainz unavailable; fetching artist bio/image without country lookup for: {artist}")

                        artist_bio = _fb_bio
                        artist_image = _fb_image

                        if HAVE_AUDIODB and not (artist_bio and artist_image):
                            try:
                                artist_bio = artist_bio or _run_with_timeout(
                                    get_artist_biography,
                                    8,
                                    f"Artist bio lookup timed out after 8s",
                                    artist,
                                    enabled=True
                                ) or ""

                                artist_image = artist_image or _run_with_timeout(
                                    get_artist_fanart,
                                    8,
                                    f"Artist image lookup timed out after 8s",
                                    artist,
                                    enabled=True
                                ) or ""
                            except Exception as e:
                                log_debug(f"AudioDB bio/image lookup failed for {artist} (MusicBrainz unavailable): {e}")

                        # If AudioDB has no metadata (or is unavailable), prefer Discogs bio by saved ID, then Last.fm.
                        if not artist_bio and not artist_image:
                            try:
                                artist_bio = _get_discogs_bio_from_saved_artist_id() or ""
                                if not artist_bio:
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
                                log_debug(f"Discogs/Last.fm bio/image fallback failed for {artist} (MusicBrainz unavailable): {e}")

                        if artist_bio or artist_image:
                            cursor.execute(f"""
                                INSERT INTO artists (id, name, bio, image_url)
                                VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder})
                                ON CONFLICT(name) DO UPDATE SET
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
            similar_artists_cached = False

            try:
                cursor.execute(f"""
                    SELECT similar_artists_lastfm, similar_artists_listenbrainz, similar_artists_last_updated
                    FROM artists
                    WHERE id = {placeholder} OR name = {placeholder}
                    LIMIT 1
                """, (artist, artist))
                cached_row = cursor.fetchone()
                if cached_row:
                    cached_lastfm_raw = row_get(cached_row, 'similar_artists_lastfm')
                    cached_listenbrainz_raw = row_get(cached_row, 'similar_artists_listenbrainz')
                    similar_artists_lastfm = json.loads(cached_lastfm_raw) if cached_lastfm_raw else []
                    similar_artists_listenbrainz = json.loads(cached_listenbrainz_raw) if cached_listenbrainz_raw else []
                    similar_artists_cached = bool(similar_artists_lastfm or similar_artists_listenbrainz)
                    if similar_artists_cached:
                        # Expire the cache after 90 days so stale data is refreshed
                        _sa_last_updated = row_get(cached_row, 'similar_artists_last_updated')
                        if _sa_last_updated:
                            try:
                                from datetime import datetime as _dt, timedelta as _td, timezone as _tz
                                if isinstance(_sa_last_updated, str):
                                    _sa_last_updated = _dt.fromisoformat(_sa_last_updated.replace('Z', '+00:00'))
                                if _sa_last_updated.tzinfo is not None:
                                    _age_days = (_dt.now(_tz.utc) - _sa_last_updated).days
                                else:
                                    _age_days = (_dt.now() - _sa_last_updated).days
                                if _age_days > 90:
                                    log_debug(f"Similar artists cache expired for '{artist}' ({_age_days} days old > 90 day TTL) — will re-fetch")
                                    similar_artists_cached = False
                                    similar_artists_lastfm = []
                                    similar_artists_listenbrainz = []
                            except Exception as _ttl_err:
                                log_debug(f"Could not check similar artists TTL for '{artist}': {_ttl_err}")
                        if similar_artists_cached:
                            log_debug(
                                f"Using cached similar artists for '{artist}' "
                                f"(Last.fm: {len(similar_artists_lastfm)}, ListenBrainz: {len(similar_artists_listenbrainz)})"
                            )
            except Exception as cache_err:
                log_debug(f"Could not read cached similar artists for {artist}: {cache_err}")

            # Fetch similar artists for all artists (including compilations for recommendation purposes)
            try:
                if singles_only or singles_with_missing_popularity:
                    raise StopIteration
                if similar_artists_cached:
                    raise StopIteration

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
                            ON CONFLICT(name) DO UPDATE SET
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
            except StopIteration:
                pass

            # Fetch and store artist tags from Last.fm
            try:
                if singles_only or singles_with_missing_popularity:
                    raise StopIteration
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
            except StopIteration:
                pass
            except Exception as e:
                log_debug(f"Last.fm artist tags lookup failed for {artist}: {e}")

            # Fetch missing releases from MusicBrainz and update database
            try:
                if HAVE_MUSICBRAINZ and not singles_only and not singles_with_missing_popularity:
                    log_debug(f"Checking for missing releases for '{artist}' on MusicBrainz")

                    # Get existing albums for this artist
                    cursor.execute(f"SELECT DISTINCT album FROM tracks WHERE artist = {placeholder}", (artist,))
                    existing_albums = [row_get(row, 'album') for row in cursor.fetchall()]
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

                    # Get artist MBID for more accurate lookup.
                    # Use MAX() as a tie-breaker, but first filter out any
                    # concatenated/multi-value entries by preferring rows whose
                    # musicbrainz_artist_id looks like a single UUID.
                    cursor.execute(f"""
                        SELECT musicbrainz_artist_id
                        FROM tracks
                        WHERE artist = {placeholder}
                          AND musicbrainz_artist_id IS NOT NULL
                          AND TRIM(musicbrainz_artist_id) != ''
                        ORDER BY LENGTH(musicbrainz_artist_id) ASC
                        LIMIT 1
                    """, (artist,))
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
                    headers = {"User-Agent": MUSICBRAINZ_USER_AGENT}
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
                        # EPs and singles should remain in their own buckets regardless of secondary types
                        if primary_type == "ep":
                            category = "EP"
                        elif primary_type == "single" or "single" in secondary:
                            category = "Single"
                        elif "compilation" in secondary:
                            category = "Compilation"
                        elif "live" in secondary:
                            category = "Live Album"
                        elif "remix" in secondary:
                            category = "Remix"

                        # Only include singles released in the current calendar year.
                        if category == "Single":
                            release_year_str = (rg.get("first-release-date") or "").split("-")[0]
                            try:
                                release_year = int(release_year_str)
                            except (ValueError, TypeError):
                                release_year = 0
                            if release_year < datetime.now().year:
                                continue

                        # Insert missing release
                        cursor.execute(f"""
                            INSERT INTO missing_releases
                            (artist, artist_mbid, release_id, title, primary_type, first_release_date, cover_art_url, category, last_checked)
                            VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, CURRENT_TIMESTAMP)
                            ON CONFLICT (artist, release_id) DO UPDATE SET
                                artist_mbid = EXCLUDED.artist_mbid,
                                title = EXCLUDED.title,
                                primary_type = EXCLUDED.primary_type,
                                first_release_date = EXCLUDED.first_release_date,
                                cover_art_url = EXCLUDED.cover_art_url,
                                category = EXCLUDED.category,
                                last_checked = EXCLUDED.last_checked
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

            # Pre-load MB single release titles for this artist from missing_releases.
            # The missing_releases table is populated above with ALL MusicBrainz releases
            # including singles not yet in the library.  Using these titles avoids one MB
            # API call per track in the single-detection stage below.
            # Note: this covers singles NOT in the user's library.  Tracks that are singles
            # AND already in the library are handled by the per-track MB API path in
            # detect_single_enhanced (which itself caches per artist MBID within the scan run).
            mb_artist_singles_normalized: set = set()
            try:
                cursor.execute(f"""
                    SELECT title FROM missing_releases
                    WHERE LOWER(artist) = LOWER({placeholder}) AND category = 'Single'
                """, (artist,))
                for _mr_row in cursor.fetchall():
                    _mr_title = row_get(_mr_row, 'title') or ''
                    if _mr_title:
                        mb_artist_singles_normalized.add(_mr_title.lower().strip())
                if mb_artist_singles_normalized:
                    log_debug(f"Pre-loaded {len(mb_artist_singles_normalized)} MB single titles for '{artist}' from missing_releases cache")
            except Exception as _mr_err:
                log_debug(f"Could not pre-load MB singles for '{artist}' from missing_releases: {_mr_err}")
                mb_artist_singles_normalized = set()

            album_num = 0
            total_albums = len(albums)

            for album, album_tracks in albums.items():
                if _stop_requested():
                    log_info(f"Stop requested via progress file; exiting during artist '{artist}'")
                    return False

                log_info(f"\n" + "="*80)
                log_info(f"📂 ALBUM: {album} [{album_num + 1}/{total_albums}]")
                log_info(f"🎵 ARTIST: {artist}")
                log_info(f"📊 TRACKS: {len(album_tracks)}")
                log_info(f"="*80)

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
                if hasattr(conn, "get_transaction_status"):
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

                # Fetch and store album tags from Last.fm (metadata enrichment — skip in singles modes)
                if not singles_only and not singles_with_missing_popularity:
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
                    log_info(f'⏭️ SINGLES-ONLY MODE: Skipping popularity scan for "{artist} - {album}"')
                    log_info(f'🔍 Will proceed directly to singles detection')
                elif singles_with_missing_popularity:
                    # Smart singles scan: only fetch popularity from external sources when the album
                    # has no existing popularity data in the database, or when z-score context
                    # cannot be validated from existing scores.
                    log_unified(f'Singles Detection - Scanning Album {album} ({album_num}/{len(albums)})')
                    if not (FORCE_RESCAN or force) and not album_filter:
                        zscore_ready_track_count = 0
                        has_popularity_for_all_tracks = True
                        for t in album_tracks:
                            pop_value = row_get(t, 'popularity_score', 0) or 0
                            if float(pop_value) <= 0:
                                has_popularity_for_all_tracks = False
                            if float(pop_value) > 0 and not should_exclude_track_from_stats(row_get(t, 'title', ''), row_get(t, 'album', ''), row_get(t, 'is_live', 0) or 0, row_get(t, 'album_context_live', 0) or 0):
                                zscore_ready_track_count += 1

                        has_pop_data = has_popularity_for_all_tracks
                        has_zscore_context = zscore_ready_track_count >= 2

                        if has_pop_data and has_zscore_context:
                            log_info(f'⏭️ SINGLES SCAN: Album "{artist} - {album}" already has popularity data and z-score context - running singles detection only')
                            skip_popularity_for_album = True
                        else:
                            log_info(
                                f'📊 SINGLES SCAN: Album "{artist} - {album}" missing popularity/z-score context '
                                f'(all_tracks_scored={has_pop_data}, zscore_tracks={zscore_ready_track_count}) - '
                                f'running popularity scan first'
                            )
                    else:
                        log_info(f'📊 SINGLES SCAN: Force mode - running full popularity scan for "{artist} - {album}"')
                else:
                        # ------------------------------------------------------------------
                        # Fast "skip-if-unchanged" check: when not forced and not a targeted
                        # album rescan, skip the entire album when nothing has changed.
                        #
                        # Rules:
                        #   * ALL tracks have popularity_score > 0  → skip popularity scoring
                        #   * ALL tracks have single_detection_last_updated IS NOT NULL → skip singles too
                        #   * Either condition missing → run the appropriate pass
                        #   * force=True or album_filter set → bypass this check entirely
                        # ------------------------------------------------------------------
                        if not (FORCE_RESCAN or force) and not album_filter and not metadata_only:
                            try:
                                total_in_album = len(album_tracks)
                                if total_in_album > 0:
                                    track_ids_in_album = [t["id"] for t in album_tracks]
                                    id_placeholders = ", ".join([placeholder] * len(track_ids_in_album))
                                    is_single_high_expr = (
                                        "CASE WHEN is_single AND single_confidence = 'high' THEN 1 ELSE 0 END"
                                    )
                                    cursor.execute(
                                        f"""
                                        SELECT
                                            COUNT(*) AS total,
                                            SUM(CASE WHEN popularity_score > 0 THEN 1 ELSE 0 END) AS scored,
                                            SUM(CASE WHEN single_detection_last_updated IS NOT NULL THEN 1 ELSE 0 END) AS singles_assessed,
                                            SUM({is_single_high_expr}) AS high_conf_singles
                                        FROM tracks
                                        WHERE id IN ({id_placeholders})
                                        """,
                                        track_ids_in_album,
                                    )
                                    check_row = cursor.fetchone()
                                    if check_row:
                                        scored_count = int(row_get(check_row, "scored", 0) or 0)
                                        singles_assessed = int(row_get(check_row, "singles_assessed", 0) or 0)
                                        high_conf_singles = int(row_get(check_row, "high_conf_singles", 0) or 0)
                                        all_scored = scored_count >= total_in_album
                                        all_singles_assessed = singles_assessed >= total_in_album
                                        if all_scored and all_singles_assessed:
                                            if high_conf_singles == 0:
                                                log_unified(f'Popularity Scan - Skipping album "{album}" (no changes detected)')
                                                log_info(f'Album "{artist} - {album}" unchanged — all {total_in_album} tracks scored & singles assessed, skipping')
                                                skipped_count += 1
                                                continue
                                            else:
                                                # High-confidence single tracks present: run the star rating
                                                # loop to validate stored confidence against current evidence
                                                # (see stale high-confidence re-validation in star rating pass).
                                                log_info(
                                                    f'Album "{artist} - {album}" has {high_conf_singles} high-confidence '
                                                    f'single(s) — skipping popularity/singles but running star rating validation'
                                                )
                                                skip_popularity_for_album = True
                                        elif all_scored:
                                            # Popularity is current but some singles haven't been assessed yet
                                            log_info(f'Album "{artist} - {album}" popularity unchanged — running singles detection only')
                                            skip_popularity_for_album = True
                            except Exception as _unch_err:
                                log_debug(f"skip-if-unchanged check failed for '{artist} - {album}': {_unch_err}")

                        # Check if album was already scanned (unless force rescan is enabled)
                        if not (FORCE_RESCAN or force) and not metadata_only and was_album_scanned(artist, album, 'popularity', album_skip_days):
                            log_unified(f'Popularity Scan - Skipping album "{album}" (scanned within last {album_skip_days} days)')
                            log_info(f'⏱️ Album "{artist} - {album}" was already scanned within {album_skip_days} days - POPULARITY SKIP')
                            skipped_count += 1
                            skip_popularity_for_album = True

                        log_unified(f'Popularity Scan - Scanning Album {album} ({album_num}/{len(albums)})')
                        log_info(f'🔎 Starting POPULARITY SCAN for album: "{artist} - {album}"')

                # ALBUM TYPE DETECTION - Do this once per album at the start
                # Detect album type from MusicBrainz/auto-detection and apply to all tracks
                log_info(f'🏷️ Starting album type detection for "{artist} - {album}"')
                log_debug(f'Starting album type detection for "{artist} - {album}"')

                # Get current album type from first track (if any)
                current_album_type = album_tracks[0].get('spotify_album_type', '') if album_tracks else ''
                detected_album_type = None
                type_detection_source = None
                release_group_mbid = None
                _mbid_was_newly_discovered = False
                discovered_release_group_mbid = None

                # Auto-detect Various Artists / Compilation / Soundtrack album_artist → Album (Compilation)
                # These album_artist values indicate multi-artist compilation albums and should all
                # be treated identically to standard compilations for popularity and single scanning.
                artist_lower = artist.lower()
                compilation_artists = ('various artists', 'various artists -', 'various', 'compilation', 'soundtrack')
                if artist_lower in compilation_artists:
                    detected_album_type = 'album+compilation'
                    type_detection_source = f'auto-detected (album_artist: {artist})'
                    log_info(f'✅ AUTO-DETECTED: Compilation album - "{album}" (artist: {artist})')

                # Auto-detect Soundtrack in album name → Album (Soundtrack)
                # (only when album_artist is NOT already a compilation-type artist)
                elif 'soundtrack' in album.lower():
                    detected_album_type = 'album+soundtrack'
                    type_detection_source = 'auto-detected (Soundtrack in name)'
                    log_info(f'Auto-detected soundtrack album: "{album}"')

                # Otherwise, fetch from MusicBrainz with Spotify fallback
                else:
                    # Skip MB API call if musicbrainz_albumtype is already stored as a confirmed
                    # non-plain value (contains '+' or is 'ep'/'single') — album types never change.
                    _stored_mb_type = (album_tracks[0].get('musicbrainz_albumtype') or '').strip().lower() if album_tracks else ''
                    _type_already_confirmed = bool(
                        _stored_mb_type and (
                            '+' in _stored_mb_type or
                            '(' in _stored_mb_type or
                            _stored_mb_type in ('ep', 'single')
                        )
                    )
                    if _type_already_confirmed:
                        detected_album_type = album_tracks[0].get('musicbrainz_albumtype')
                        type_detection_source = 'cached (musicbrainz_albumtype column)'
                        log_debug(f'Album type already confirmed from stored musicbrainz_albumtype: "{detected_album_type}" — skipping MB API call')
                    else:
                        try:
                            from api_clients.musicbrainz import get_album_type_with_fallback
                            # Look up release group MBID for a direct, accurate lookup if available.
                            # Prefer musicbrainz_releasegroupid (the canonical release-group UUID)
                            # and fall back to musicbrainz_album_mbid for backwards compatibility.
                            try:
                                cursor.execute(f"""
                                    SELECT musicbrainz_releasegroupid, musicbrainz_album_mbid FROM tracks
                                    WHERE COALESCE(NULLIF(album_artist, ''), artist) = {placeholder}
                                      AND album = {placeholder}
                                      AND (musicbrainz_releasegroupid IS NOT NULL AND musicbrainz_releasegroupid != ''
                                           OR musicbrainz_album_mbid IS NOT NULL AND musicbrainz_album_mbid != '')
                                    LIMIT 1
                                """, (artist, album))
                                mbid_row = cursor.fetchone()
                                if mbid_row:
                                    _rg_mbid = row_get(mbid_row, 'musicbrainz_releasegroupid')
                                    _rel_mbid = row_get(mbid_row, 'musicbrainz_album_mbid')
                                    if _rg_mbid:
                                        release_group_mbid = _rg_mbid
                                        log_debug(f'Using stored release-group MBID {release_group_mbid} for direct MusicBrainz lookup')
                                    # Do NOT fall back to musicbrainz_album_mbid here.
                                    # musicbrainz_album_mbid is a release MBID (specific pressing),
                                    # not a release-group MBID. Passing it as release_group_mbid
                                    # causes the MusicBrainz direct lookup to fail (404) because
                                    # it queries /release-group/{release_mbid}. The text-search
                                    # fallback then finds the correct release group, but the old
                                    # code skipped propagating it because release_group_mbid was
                                    # already set. Only use musicbrainz_releasegroupid for the
                                    # release-group lookup.
                            except Exception:
                                pass  # Column may not exist in older schemas
                            detected_album_type, type_detection_source, discovered_release_group_mbid = get_album_type_with_fallback(
                                artist, album, current_album_type, enabled=HAVE_MUSICBRAINZ,
                                track_count=len(album_tracks), release_group_mbid=release_group_mbid
                            )
                            log_debug(f'MusicBrainz album type: "{detected_album_type}" (source: {type_detection_source})')
                            # Capture a newly-discovered MBID (text-search path) so it can
                            # be propagated to all tracks that are currently missing it.
                            # The value returned by get_album_type_with_fallback is a
                            # release-group MBID, but musicbrainz_album_mbid stores a
                            # release MBID (specific pressing). Resolve a representative
                            # release before writing so Navidrome groups tracks correctly.
                            if discovered_release_group_mbid:
                                _mbid_was_newly_discovered = True
                                _representative_release_mbid = None
                                _mb_meta = None
                                try:
                                    from post_download_processor import fetch_musicbrainz_release_metadata
                                    _mb_meta = fetch_musicbrainz_release_metadata(discovered_release_group_mbid)
                                    if _mb_meta and _mb_meta.get('release_mbid'):
                                        _representative_release_mbid = _mb_meta['release_mbid']
                                        log_debug(f'Resolved release-group {discovered_release_group_mbid} to release {_representative_release_mbid}')
                                except Exception as _conv_err:
                                    log_debug(f'Could not resolve release-group MBID to release MBID: {_conv_err}')
                                if _representative_release_mbid:
                                    release_group_mbid = _representative_release_mbid
                                else:
                                    # Skip writing rather than storing a release-group ID
                                    # in the release-level column.
                                    release_group_mbid = None
                                # Update album name to the canonical MusicBrainz release-group title
                                # so the library stays consistent with MB naming.
                                _mb_title = (_mb_meta.get('release_title') or '').strip() if _mb_meta else ''
                                if _mb_title and _mb_title.lower() != album.lower():
                                    try:
                                        cursor.execute(f"""
                                            UPDATE tracks
                                            SET album = {placeholder}
                                            WHERE COALESCE(NULLIF(album_artist, ''), artist) = {placeholder}
                                              AND album = {placeholder}
                                        """, (_mb_title, artist, album))
                                        conn.commit()
                                        log_info(f'Updated album name from "{album}" to "{_mb_title}" (MusicBrainz release-group title)')
                                    except Exception as _name_err:
                                        log_debug(f'Could not update album name for "{artist} - {album}": {_name_err}')
                                        try:
                                            conn.rollback()
                                        except Exception:
                                            pass
                        except Exception as e:
                            log_debug(f'Failed to fetch album type from MusicBrainz: {e}')
                            detected_album_type = current_album_type or 'album'
                            type_detection_source = 'fallback (Spotify or default)'

                # Guard: do not let MusicBrainz change a non-live album to live when
                # the album name gives no live indicators.  This prevents a studio album
                # with the same name as a live release from being misclassified.
                # Exception: if the track listing is a close match to the MusicBrainz live
                # release, accept the live classification even without live indicators.
                _detected_lower = (detected_album_type or '').lower()
                if ('+live' in _detected_lower or '(live)' in _detected_lower) and not is_live_or_alternate_album(album):
                    _track_listing_match = False
                    if discovered_release_group_mbid:
                        try:
                            from folder_matching_enhancements import get_musicbrainz_release_tracks
                            mb_tracks = get_musicbrainz_release_tracks(discovered_release_group_mbid, source='musicbrainz')
                            if mb_tracks:
                                _local_titles = set()
                                for _t in album_tracks:
                                    _t_title = (_t.get('title') or '').lower().strip()
                                    if _t_title:
                                        _normalized = unicodedata.normalize("NFKD", _t_title)
                                        _normalized = "".join(c for c in _normalized if not unicodedata.combining(c))
                                        _normalized = re.sub(r'[^a-z0-9]+', ' ', _normalized.lower()).strip()
                                        _local_titles.add(_normalized)
                                _matched = 0
                                for _mb_track in mb_tracks:
                                    _mb_title = (_mb_track.get('title') or '').lower().strip()
                                    if _mb_title:
                                        _normalized = unicodedata.normalize("NFKD", _mb_title)
                                        _normalized = "".join(c for c in _normalized if not unicodedata.combining(c))
                                        _normalized = re.sub(r'[^a-z0-9]+', ' ', _normalized.lower()).strip()
                                        if _normalized in _local_titles:
                                            _matched += 1
                                _match_ratio = _matched / len(mb_tracks) if mb_tracks else 0
                                if _match_ratio >= 0.7:
                                    _track_listing_match = True
                                    log_info(f'Accepting MusicBrainz live classification for "{artist} - {album}" — track listing matches ({_match_ratio:.0%})')
                        except Exception as _e:
                            log_debug(f'Could not verify track listing for live classification: {_e}')

                    if not _track_listing_match:
                        log_info(f'Rejecting MusicBrainz live classification for "{artist} - {album}" — album name does not contain live indicators')
                        detected_album_type = current_album_type or 'album'
                        type_detection_source = 'fallback (live rejected by name heuristic)'

                # Update ALL tracks in this album with the detected type.
                # Skip if detection fell back to "unknown" (API failure) to avoid
                # clobbering a correctly-identified type from a previous scan.
                if detected_album_type and detected_album_type != "unknown" and detected_album_type != current_album_type:
                    primary_release_type = normalize_primary_release_type(detected_album_type)
                    tracks_updated = 0
                    for track in album_tracks:
                        track_id = track["id"]
                        cursor.execute(f"""
                            UPDATE tracks
                            SET spotify_album_type = {placeholder},
                                releasetype = {placeholder},
                                musicbrainz_albumtype = {placeholder}
                            WHERE id = {placeholder}
                        """, (detected_album_type, primary_release_type, detected_album_type, track_id))
                        tracks_updated += 1
                        # Update the track dict for use in rest of scan
                        track["spotify_album_type"] = detected_album_type
                        track["releasetype"] = primary_release_type
                        track["musicbrainz_albumtype"] = detected_album_type

                    if tracks_updated > 0:
                        conn.commit()
                        log_info(f'Updated {tracks_updated} track(s) with album type "{detected_album_type}" (source: {type_detection_source})')
                else:
                    log_debug(f'Album type unchanged: "{detected_album_type or current_album_type}"')

                # Propagate the release-group MBID to any tracks in this album that are
                # missing it.  This ensures all tracks benefit from the MBID discovered
                # during the album type lookup (text-search path), not just the one track
                # that originally carried it.
                # Only propagate when we discovered a NEW MBID this scan — do not
                # propagate existing DB values that might be release-group MBIDs stored
                # in the wrong column.
                if _mbid_was_newly_discovered and (release_group_mbid or discovered_release_group_mbid):
                    try:
                        # Fetch file paths for tracks that are about to receive the album MBID,
                        # so we can write the tag to the actual audio files after the DB update.
                        cursor.execute(
                            f"""
                            SELECT file_path FROM tracks
                            WHERE COALESCE(NULLIF(album_artist, ''), artist) = {placeholder}
                              AND album = {placeholder}
                              AND file_path IS NOT NULL AND file_path <> ''
                              AND (
                                   musicbrainz_album_mbid IS NULL
                                   OR TRIM(CAST(musicbrainz_album_mbid AS TEXT)) = ''
                                   OR musicbrainz_releasegroupid IS NULL
                                   OR TRIM(CAST(musicbrainz_releasegroupid AS TEXT)) = ''
                              )
                            """,
                            (artist, album),
                        )
                        _fps_to_tag = [
                            (r['file_path'] if hasattr(r, 'keys') else r[0])
                            for r in cursor.fetchall()
                        ]

                        _set_parts = []
                        _set_params = []
                        # Only write release-level MBIDs when we successfully resolved a
                        # representative release; never store a release-group ID in the
                        # release-level column.
                        if release_group_mbid:
                            _set_parts.append(f"musicbrainz_album_mbid = {placeholder}")
                            _set_parts.append(f"musicbrainz_albumid    = {placeholder}")
                            _set_params.extend([release_group_mbid, release_group_mbid])
                        # Always store the release-group MBID in its dedicated column.
                        if discovered_release_group_mbid:
                            try:
                                cursor.execute("""
                                    SELECT column_name FROM information_schema.columns
                                    WHERE table_schema = 'public' AND table_name = 'tracks' AND column_name = 'musicbrainz_releasegroupid'
                                """)
                                if cursor.fetchone():
                                    _set_parts.append(f"musicbrainz_releasegroupid = {placeholder}")
                                    _set_params.append(discovered_release_group_mbid)
                            except Exception:
                                pass
                        if not _set_parts:
                            log_debug(f'No MBID fields to propagate for "{artist} - {album}"')
                            raise Exception("No MBID fields to propagate")
                        _set_params.extend([artist, album])
                        cursor.execute(f"""
                            UPDATE tracks
                            SET {', '.join(_set_parts)}
                            WHERE COALESCE(NULLIF(album_artist, ''), artist) = {placeholder}
                              AND album = {placeholder}
                              AND (
                                   musicbrainz_album_mbid IS NULL
                                   OR TRIM(CAST(musicbrainz_album_mbid AS TEXT)) = ''
                                   OR musicbrainz_releasegroupid IS NULL
                                   OR TRIM(CAST(musicbrainz_releasegroupid AS TEXT)) = ''
                              )
                        """, tuple(_set_params))
                        _mbid_rows = cursor.rowcount if cursor.rowcount and cursor.rowcount >= 0 else 0
                        if _mbid_rows > 0:
                            conn.commit()
                            _prop_mbid_str = release_group_mbid or discovered_release_group_mbid
                            log_info(f'Propagated album MBID {_prop_mbid_str} to {_mbid_rows} track(s) in "{artist} - {album}"')

                            # Also write the MBID into the physical audio files so that
                            # media servers (Navidrome, etc.) group all tracks under the
                            # correct album without needing a "Save" action.
                            try:
                                from helpers.tag_manager import write_tags_to_file as _write_album_mbid_tag
                                _files_written = 0
                                for _fp in _fps_to_tag:
                                    if _fp and os.path.exists(str(_fp)):
                                        try:
                                            _tags = {}
                                            if release_group_mbid:
                                                _tags["musicbrainz_album_mbid"] = release_group_mbid
                                                _tags["musicbrainz_albumid"] = release_group_mbid
                                            if discovered_release_group_mbid:
                                                _tags["musicbrainz_releasegroupid"] = discovered_release_group_mbid
                                            if _tags and _write_album_mbid_tag(str(_fp), _tags):
                                                _files_written += 1
                                        except Exception as _fp_tag_err:
                                            log_debug(
                                                f'Could not write album MBID to "{_fp}": {_fp_tag_err}'
                                            )
                                if _files_written:
                                    log_info(
                                        f'Wrote album MBID {_prop_mbid_str} to {_files_written} '
                                        f'audio file(s) in "{artist} - {album}"'
                                    )
                            except Exception as _tag_err:
                                log_debug(f'Could not write album MBID to files for "{artist} - {album}": {_tag_err}')
                    except Exception as _mbid_err:
                        log_debug(f'Could not propagate album MBID for "{artist} - {album}": {_mbid_err}')

                # Use the detected type for rest of scan; treat "unknown" (API
                # fallback) as absent so the prior confirmed value is preferred.
                _effective_detected = detected_album_type if detected_album_type != "unknown" else None
                album_type_from_field = _effective_detected or current_album_type or 'album'
                pre_detected_album_type = album_type_from_field

                # Update album_context_live based on MusicBrainz secondary type.
                # MusicBrainz is the authoritative source for live detection - it uses the
                # official "Live" secondary release group type, which is more reliable than
                # album name heuristics (e.g. avoids false positives like "13 Ways to Bleed on Stage").
                # This overrides whatever was set during initial import via name-based detection.
                if type_detection_source == "musicbrainz":
                    type_lower = (detected_album_type or '').lower()
                    mb_is_live = '+live' in type_lower or '(live)' in type_lower
                    mb_live_value = 1 if mb_is_live else 0
                    try:
                        cursor.execute(f"""
                            UPDATE tracks
                            SET album_context_live = {placeholder}
                            WHERE COALESCE(NULLIF(album_artist, ''), artist) = {placeholder} AND album = {placeholder}
                        """, (mb_live_value, artist, album))
                        conn.commit()
                        # Sync in-memory dicts so subsequent per-track processing sees the new value.
                        for _t in album_tracks:
                            _t['album_context_live'] = mb_live_value
                        if mb_is_live:
                            log_info(f'MusicBrainz confirmed live album: "{artist} - {album}" (type: {detected_album_type})')
                        else:
                            log_debug(f'MusicBrainz confirms non-live album: "{artist} - {album}" (type: {detected_album_type}), album_context_live reset to 0')
                    except Exception as e:
                        log_debug(f'Failed to update album_context_live for "{artist} - {album}": {e}')
                        try:
                            conn.rollback()
                        except Exception:
                            pass

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

                # MISSING TRACK DETECTION (NO AUTO-QUEUE)
                # After album type detection, check if any tracks are missing from MusicBrainz release
                # and surface them for manual download workflow
                try:
                    if release_group_mbid:
                        missing_count = detect_and_queue_missing_tracks(
                            artist=artist,
                            album=album,
                            album_tracks=album_tracks,
                            release_group_mbid=release_group_mbid,
                            conn=conn
                        )
                        if missing_count > 0:
                            log_info(f'📋 Detected {missing_count} missing track(s) (manual queue only)')
                    else:
                        log_debug(f'No MusicBrainz release ID available for missing track detection: "{artist} - {album}"')
                except Exception as e:
                    log_debug(f'Error during missing track detection for "{artist} - {album}": {e}')

                # ALBUM-LEVEL DISCOGS GENRE FETCH
                # For homogeneous album types (Single, EP, Album), fetch Discogs genres once at album level
                # For compilations/soundtracks/live albums, genres will be fetched per-track (different per track)
                album_discogs_genres = None
                album_discogs_genres_json = None

                # Determine if this is a "homogeneous" album type (all tracks share genres)
                is_homogeneous_album = True
                album_type_lower = album_type_from_field.lower()

                # Check for heterogeneous types (compilation, soundtrack, live, remix, spoken word)
                # Handles both '+type' (internal format) and '(type)' (MusicBrainz parentheses format)
                heterogeneous_markers = [
                    '+compilation', '(compilation)',
                    '+soundtrack', '(soundtrack)',
                    '+live', '(live)',
                    '+remix', '(remix)',
                    '+spokenword', '(spokenword)',
                ]
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
                            # Fallback: try a text search by artist + album title
                            try:
                                from api_clients.discogs import DiscogsClient as _DC
                                _dc_fb = _DC(discogs_token)
                                _fb_genres = _run_with_timeout(
                                    _dc_fb.get_genres,
                                    10,
                                    "Discogs album text-search timed out",
                                    album, artist
                                )
                                if _fb_genres:
                                    if isinstance(_fb_genres, list) and _fb_genres and isinstance(_fb_genres[0], str):
                                        album_discogs_genres = [{"name": g} for g in _fb_genres]
                                    else:
                                        album_discogs_genres = _fb_genres
                                    album_discogs_genres_json = json.dumps(album_discogs_genres)
                                    log_info(f'Discogs album text-search found {len(album_discogs_genres)} genre(s) for "{artist} - {album}"')
                            except Exception as _dc_fb_err:
                                log_debug(f'Discogs album text-search fallback failed: {_dc_fb_err}')
                    except Exception as e:
                        log_debug(f'Failed to fetch album-level Discogs genres: {e}')
                        log_info(f'Will fall back to per-track Discogs genre fetching for this album')
                elif is_homogeneous_album:
                    log_debug(f'Discogs not configured or disabled, skipping album-level genre fetch')

                # Detect if this is a live/unplugged album
                # When MusicBrainz is the detection source, trust its secondary type exclusively.
                # Fall back to name-based heuristics only when MusicBrainz data is unavailable.
                if type_detection_source == "musicbrainz":
                    album_type_lower = (album_type_from_field or '').lower()
                    is_live_album = '+live' in album_type_lower or '(live)' in album_type_lower
                else:
                    album_type_lower = (album_type_from_field or '').lower()
                    is_live_album = '+live' in album_type_lower or '(live)' in album_type_lower or is_live_or_alternate_album(album)

                # Determine the specific live sub-type (live vs acoustic) for genre tagging.
                album_live_genre = None  # Will be "Live" or "Acoustic" when set

                if is_live_album:
                    _live_type = _detect_live_album_type(album, album_type_from_field)
                    album_live_genre = "Acoustic" if _live_type == "acoustic" else "Live"

                    log_info(f'Detected {_live_type} album: "{album}"')
                    log_info(f'Track genre will be tagged as "{album_live_genre}" and title renamed with ({album_live_genre})')
                    log_debug(f'Live album detection: album="{album}", album_type="{album_type_from_field}"')

                    # Tag each track's genre with "Live" or "Acoustic" and rename the title.
                    # Tracks that already carry the flag + genre combination are skipped.
                    live_tracks_updated = 0
                    for track in album_tracks:
                        track_id = track["id"]
                        _track_title = track.get("title", "") or ""

                        # Skip tracks already confirmed: flag set AND genre already present.
                        _is_live_flag = int(track.get('is_live') or 0)
                        _is_acoustic_flag = int(track.get('is_acoustic') or 0)
                        _mb_genres_raw = track.get('musicbrainz_genres') or ''
                        try:
                            _current_mb_genres = json.loads(_mb_genres_raw) if _mb_genres_raw and _mb_genres_raw != 'null' else []
                        except (json.JSONDecodeError, TypeError):
                            _current_mb_genres = []

                        # Case-insensitive check so previously stored 'live'/'Live' both match.
                        _current_mb_genres_lower = [str(g).lower() for g in _current_mb_genres]
                        _already_tagged = (
                            (album_live_genre == "Live" and _is_live_flag and "live" in _current_mb_genres_lower) or
                            (album_live_genre == "Acoustic" and _is_acoustic_flag and "acoustic" in _current_mb_genres_lower)
                        )
                        if _already_tagged:
                            log_debug(f'Skipping live/acoustic tag for track "{_track_title}": already confirmed')
                            continue

                        # Add genre tag (only when the canonical capitalised form is not present).
                        if album_live_genre.lower() not in _current_mb_genres_lower:
                            _current_mb_genres.insert(0, album_live_genre)
                        _new_mb_genres = json.dumps(_current_mb_genres)

                        # Set the correct flag; the other flag is explicitly left at 0 so a
                        # track can never be simultaneously marked as both live and acoustic.
                        _is_live_val = 1 if album_live_genre == "Live" else 0
                        _is_acoustic_val = 1 if album_live_genre == "Acoustic" else 0

                        # Determine title suffix based on live type
                        _title_suffix = "Acoustic" if album_live_genre == "Acoustic" else "Live"

                        # Append suffix to title if the title does not already indicate a live/acoustic version
                        # and does not already end with the suffix we are about to add.
                        _title_renamed = False
                        _already_has_suffix = bool(re.search(rf'\({_title_suffix}[^)]*\)\s*$', _track_title, re.IGNORECASE))
                        if _track_title and not is_live_or_unplugged_track_title(_track_title) and not _already_has_suffix:
                            _new_title = f"{_track_title} ({_title_suffix})"
                            _title_renamed = True
                        else:
                            _new_title = _track_title

                        # Update main genres column to include Live/Acoustic alongside existing genres.
                        _genres_raw = track.get("genres") or ""
                        _genres_list = [g.strip() for g in _genres_raw.split(",") if g.strip()]
                        _genres_lower = [g.lower() for g in _genres_list]
                        _genre_added = False
                        if album_live_genre.lower() not in _genres_lower:
                            _genres_list.insert(0, album_live_genre)
                            _genre_added = True
                        _new_genres = ", ".join(_genres_list)

                        cursor.execute(f"""
                            UPDATE tracks
                            SET is_live = {placeholder}, is_acoustic = {placeholder},
                                musicbrainz_genres = {placeholder},
                                title = {placeholder},
                                genres = {placeholder},
                                album_context_live = {placeholder}
                            WHERE id = {placeholder}
                        """, (_is_live_val, _is_acoustic_val, _new_mb_genres, _new_title, _new_genres, 1, track_id))
                        live_tracks_updated += 1

                        # Update in-memory dict so subsequent per-track processing uses the new values.
                        track['is_live'] = _is_live_val
                        track['is_acoustic'] = _is_acoustic_val
                        track['musicbrainz_genres'] = _new_mb_genres
                        track['album_context_live'] = 1
                        track['title'] = _new_title
                        track['genres'] = _new_genres

                        # Write updated title and genres to audio file.
                        _fp = track.get('file_path')
                        if _fp and (_title_renamed or _genre_added):
                            if not os.path.isabs(_fp):
                                _music_root = os.environ.get("MUSIC_FOLDER") or os.environ.get("MUSIC_ROOT") or "/music"
                                _fp = os.path.join(_music_root, _fp)
                            if os.path.exists(_fp):
                                try:
                                    from helpers.tag_manager import update_file_tags
                                    _tags_to_write = {}
                                    if _title_renamed:
                                        _tags_to_write["title"] = _new_title
                                    if _genre_added:
                                        _tags_to_write["genres"] = _genres_list
                                    update_file_tags(_fp, _tags_to_write)
                                except Exception as _file_err:
                                    log_debug(f"Failed to write live tags to file for {track_id}: {_file_err}")

                        if _title_renamed:
                            log_debug(f'Tagged and renamed track "{_track_title}" -> "{_new_title}" as {album_live_genre}')
                        else:
                            log_debug(f'Tagged track "{_track_title}" as {album_live_genre}')

                    if live_tracks_updated > 0:
                        conn.commit()
                        log_info(f'Tagged {live_tracks_updated} track(s) as "{album_live_genre}" in album "{album}"')

                # Detect if this is a remix album based on album type
                # When MusicBrainz is the detection source, trust its secondary type exclusively.
                if type_detection_source == "musicbrainz":
                    album_type_lower = (album_type_from_field or '').lower()
                    is_remix_album = '+remix' in album_type_lower or '(remix)' in album_type_lower
                else:
                    album_type_lower = (album_type_from_field or '').lower()
                    is_remix_album = '+remix' in album_type_lower or '(remix)' in album_type_lower

                if is_remix_album:
                    log_info(f'Detected remix album: "{album}"')
                    log_info(f'Track genre will be tagged as "Remix" (no title rename)')
                    log_debug(f'Remix album detection: album="{album}", album_type="{album_type_from_field}"')

                    remix_tracks_updated = 0
                    for track in album_tracks:
                        track_id = track["id"]

                        # Skip tracks already confirmed: flag set AND genre already present.
                        _is_remix_flag = int(track.get('is_remix') or 0)
                        _mb_genres_raw = track.get('musicbrainz_genres') or ''
                        try:
                            _current_mb_genres = json.loads(_mb_genres_raw) if _mb_genres_raw and _mb_genres_raw != 'null' else []
                        except (json.JSONDecodeError, TypeError):
                            _current_mb_genres = []

                        _current_mb_genres_lower = [str(g).lower() for g in _current_mb_genres]
                        _already_tagged = _is_remix_flag and "remix" in _current_mb_genres_lower
                        if _already_tagged:
                            log_debug(f'Skipping remix tag for track "{track.get("title", "")}": already confirmed')
                            continue

                        if "remix" not in _current_mb_genres_lower:
                            _current_mb_genres.insert(0, "Remix")
                        _new_mb_genres = json.dumps(_current_mb_genres)

                        cursor.execute(f"""
                            UPDATE tracks
                            SET is_remix = {placeholder},
                                musicbrainz_genres = {placeholder}
                            WHERE id = {placeholder}
                        """, (1, _new_mb_genres, track_id))
                        remix_tracks_updated += 1

                        track['is_remix'] = 1
                        track['musicbrainz_genres'] = _new_mb_genres

                        log_debug(f'Tagged track "{track.get("title", "")}" as Remix')

                    if remix_tracks_updated > 0:
                        conn.commit()
                        log_info(f'Tagged {remix_tracks_updated} track(s) as "Remix" in album "{album}"')

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
                is_scanning_compilation_artist = artist.lower() in compilation_artists

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
                        # Ensure album_type_from_field reflects compilation so single-scanning uses it
                        if not is_compilation_type(album_type_from_field):
                            album_type_from_field = 'album+compilation'
                            log_debug(f'album_type_from_field updated to "album+compilation" for compilation album "{artist} - {album}"')

                # Check if this is a greatest hits album (even for regular artists)
                if album_type == "regular":
                    is_greatest_hits = detect_greatest_hits_album(album, artist, conn, album_tracks_list)
                    if is_greatest_hits:
                        album_type = "greatest_hits"
                        log_info(f'Album type: Greatest Hits - "{artist} - {album}"')
                        log_debug(f'Greatest hits album detected, will run single detection on all tracks')

                log_debug(f'Album type determined: {album_type} for "{artist} - {album}"')

                # Fetch and cache album art using fallback strategy for this album
                album_art_url = None
                if not singles_only:
                    # Reuse the already-loaded/validated token and avoid shadowing it.
                    album_art_discogs_token = discogs_token

                    # Only fetch artwork if it isn't already stored in the database.
                    # This avoids a network round-trip and a write on every scan run.
                    _has_existing_art = False
                    try:
                        _ensure_album_art_pg_schema(conn, cursor)
                        cursor.execute(
                            "SELECT 1 FROM album_art WHERE artist_name = %s AND album_name = %s",
                            (artist, album)
                        )
                        _has_existing_art = cursor.fetchone() is not None
                    except Exception:
                        pass

                    if _has_existing_art:
                        log_debug(f'[ALBUM_ART] Album art already in DB for {artist} - {album}, skipping fetch')
                    elif fetch_and_save_album_art_with_fallback(artist, album, conn, cursor, album_art_discogs_token):
                        log_info(f'[ALBUM_ART] Album art successfully downloaded and saved for {artist} - {album}')
                    else:
                        log_debug(f'[ALBUM_ART] Failed to obtain album art from any source for {artist} - {album}')

                # ListenBrainz popularity lookups are intentionally skipped during scan.
                # The source remains available for tags/genres and other non-score features.

                # Writer/MBID backfill: run BEFORE per-track tag/genre lookups so that the
                # recording MBID is available for ListenBrainz genre fetching in the same pass.
                # Condition covers both missing writer AND missing MBID so that tracks which
                # already have a writer but no MBID still get the MBID populated here.
                writer_updates = []
                mbid_updates = []
                suggested_mbid_updates = []  # (suggested_mbid, confidence, track_id) for 0.60–0.95 matches
                if metadata_enrichment_enabled and HAVE_MUSICBRAINZ:
                    log_info(f'Starting MusicBrainz writer/MBID backfill for album "{album}" ({len(album_tracks)} tracks)')
                    for track in album_tracks:
                        track_id = track["id"]
                        title = track["title"]
                        track_artist = track["artist"]

                        track_needs_writer = _writer_is_empty(row_get(track, 'writer'))
                        track_needs_mbid = not (row_get(track, 'mbid') or '').strip()
                        if track_needs_writer or track_needs_mbid:
                            log_debug(f'Writer or MBID missing for "{title}" - querying MusicBrainz')
                            try:
                                if mb_writer_client is None:
                                    mb_writer_client = MusicBrainzClient()
                                    log_debug(f'Initialized MusicBrainz client for writer backfill')

                                normalized_title = normalize_title_for_lookup(title)

                                # Retry with progressively simplified title variants because
                                # MusicBrainz recording search often fails for live/remaster suffixes.
                                title_candidates = []
                                for candidate in (
                                    normalized_title,
                                    strip_search_parentheses(normalized_title),
                                    strip_parentheses(normalized_title),
                                ):
                                    cleaned_candidate = (candidate or "").strip()
                                    if cleaned_candidate and cleaned_candidate not in title_candidates:
                                        title_candidates.append(cleaned_candidate)

                                if not title_candidates:
                                    title_candidates = [title]

                                artist_candidates = []
                                for artist_candidate in (track_artist, artist):
                                    cleaned_artist = (artist_candidate or "").strip()
                                    if cleaned_artist and cleaned_artist not in artist_candidates:
                                        artist_candidates.append(cleaned_artist)

                                mb_writer_names = []
                                found_recording_mbid = ""
                                found_recording_confidence = 0.0
                                for artist_candidate in artist_candidates:
                                    if mb_writer_names:
                                        break
                                    for title_candidate in title_candidates:
                                        log_debug(
                                            f'MusicBrainz writer lookup attempt: title="{title_candidate}", '
                                            f'artist="{artist_candidate}"'
                                        )
                                        mb_lookup_result = _run_with_timeout(
                                            mb_writer_client.get_composers_for_track,
                                            API_CALL_TIMEOUT,
                                            f"MusicBrainz writer lookup timed out after {API_CALL_TIMEOUT}s",
                                            title_candidate,
                                            artist_candidate
                                        )
                                        mb_writer_names, found_recording_mbid, found_recording_confidence = mb_lookup_result
                                        if mb_writer_names:
                                            break

                                log_debug(f'MusicBrainz returned {len(mb_writer_names) if mb_writer_names else 0} writer(s) for "{title}"')

                                # Save recording MBID based on confidence:
                                #   ≥0.95  → confirmed match, write directly to mbid
                                #   ≥0.60  → possible match, store as suggested_mbid for review
                                if found_recording_mbid and track_needs_mbid:
                                    if found_recording_confidence >= 0.95:
                                        mbid_updates.append((found_recording_mbid, track_id))
                                        track['mbid'] = found_recording_mbid
                                        log_info(f'✅ MusicBrainz recording MBID backfill (confidence={found_recording_confidence:.2f}): "{title}" -> {found_recording_mbid}')
                                    elif found_recording_confidence >= 0.60:
                                        suggested_mbid_updates.append((found_recording_mbid, round(found_recording_confidence, 2), track_id))
                                        log_info(f'ℹ️ MusicBrainz MBID suggestion stored (confidence={found_recording_confidence:.2f}): "{title}" -> {found_recording_mbid}')

                                # Only update writer if it was originally missing; don't overwrite existing data.
                                if mb_writer_names and track_needs_writer:
                                    log_debug(f'Raw MusicBrainz writer names: {mb_writer_names}')
                                    deduped_writers = []
                                    for name in mb_writer_names:
                                        cleaned = str(name).strip()
                                        if cleaned and cleaned not in deduped_writers:
                                            deduped_writers.append(cleaned)

                                    if deduped_writers:
                                        writer_json = json.dumps(deduped_writers)
                                        writer_updates.append((writer_json, track_id))
                                        track['writer'] = writer_json
                                        log_info(f'✅ MusicBrainz writer backfill: "{title}" -> {deduped_writers}')
                                    else:
                                        log_debug(f'MusicBrainz writer lookup returned only empty values for "{title}"')
                                elif track_needs_writer:
                                    log_info(f'❌ No MusicBrainz writer credits found for "{title}"')
                            except TimeoutError as e:
                                log_info(f'⏱️ MusicBrainz writer lookup timed out for "{title}": {e}')
                            except Exception as e:
                                log_info(f'❌ Failed MusicBrainz writer backfill for "{title}": {e}')
                                import traceback
                                log_debug(f'Writer backfill error traceback: {traceback.format_exc()}')
                        else:
                            log_debug(f'Writer and MBID already populated for "{title}", skipping MusicBrainz lookup')

                # Commit writer updates immediately, before tag/genre fetching so MBID is in DB.
                if writer_updates:
                    log_info(f'💾 Committing {len(writer_updates)} writer credit update(s) for album "{album}"')
                    try:
                        _execute_tracks_batch_with_retry(
                            conn,
                            cursor,
                            f"UPDATE tracks SET writer = {placeholder} WHERE id = {placeholder}",
                            sorted(writer_updates, key=lambda row: str(row[1])),
                            f"writer batch update for album {album}",
                        )
                        conn.commit()
                        log_info(f"✅ Successfully committed {len(writer_updates)} writer credit update(s) for album '{album}'")
                        log_debug(f"Committed {len(writer_updates)} writer credit update(s) for album '{album}'")
                    except Exception as e:
                        log_info(f"❌ Failed to batch update writer credits: {e}")
                        log_debug(f"Warning: Failed to batch update writer credits: {e}")
                        try:
                            conn.rollback()
                        except Exception:
                            pass

                        # Retry once after rollback in case a previous non-critical SQL error
                        # (e.g. album art upsert) left the transaction in aborted state.
                        try:
                            _execute_tracks_batch_with_retry(
                                conn,
                                cursor,
                                f"UPDATE tracks SET writer = {placeholder} WHERE id = {placeholder}",
                                sorted(writer_updates, key=lambda row: str(row[1])),
                                f"writer batch retry for album {album}",
                                max_retries=3,
                            )
                            conn.commit()
                            log_info(f"✅ Writer credit retry succeeded for album '{album}' ({len(writer_updates)} updates)")
                            log_debug(f"Writer retry committed {len(writer_updates)} updates for album '{album}'")
                        except Exception as retry_error:
                            log_info(f"❌ Writer credit retry failed for album '{album}': {retry_error}")
                            log_debug(f"Writer retry failed for album '{album}': {retry_error}")
                            try:
                                conn.rollback()
                            except Exception:
                                pass
                else:
                    log_info(f'ℹ️ No writer updates needed for album "{album}" (all tracks already have writer data or MusicBrainz unavailable)')

                # Commit recording MBID backfills discovered during the writer lookup pass.
                if mbid_updates:
                    log_info(f'💾 Committing {len(mbid_updates)} recording MBID backfill(s) for album "{album}"')
                    try:
                        _execute_tracks_batch_with_retry(
                            conn,
                            cursor,
                            f"UPDATE tracks SET mbid = {placeholder} WHERE id = {placeholder} AND (mbid IS NULL OR TRIM(mbid) = '')",
                            sorted(mbid_updates, key=lambda row: str(row[1])),
                            f"mbid batch update for album {album}",
                        )
                        conn.commit()
                        log_info(f"✅ Successfully committed {len(mbid_updates)} recording MBID backfill(s) for album '{album}'")
                    except Exception as e:
                        log_info(f"❌ Failed to batch update recording MBIDs: {e}")
                        try:
                            conn.rollback()
                        except Exception:
                            pass

                # Store lower-confidence MBID suggestions (0.60–0.94) for user review.
                if suggested_mbid_updates:
                    log_info(f'💾 Committing {len(suggested_mbid_updates)} suggested MBID(s) for album "{album}"')
                    try:
                        _execute_tracks_batch_with_retry(
                            conn,
                            cursor,
                            f"UPDATE tracks SET suggested_mbid = {placeholder}, suggested_mbid_confidence = {placeholder} "
                            f"WHERE id = {placeholder} AND (mbid IS NULL OR TRIM(mbid) = '') "
                            f"AND (suggested_mbid IS NULL OR TRIM(suggested_mbid) = '')",
                            sorted(suggested_mbid_updates, key=lambda row: str(row[2])),
                            f"suggested_mbid batch update for album {album}",
                        )
                        conn.commit()
                        log_info(f"✅ Stored {len(suggested_mbid_updates)} MBID suggestion(s) for album '{album}'")
                    except Exception as e:
                        log_info(f"❌ Failed to store suggested MBIDs for album '{album}': {e}")
                        try:
                            conn.rollback()
                        except Exception:
                            pass

                # ---------------------------------------------------------------------
                # Collect Last.fm + ListenBrainz data for all album tracks for z-score normalization
                # ---------------------------------------------------------------------
                album_lastfm_data = {}          # track_id -> {"listeners": int, "playcount": int}
                album_listenbrainz_data = {}    # track_id -> {"listeners": int}
                album_tags_data = {}            # track_id -> {"lastfm_tags": [...], "listenbrainz_genres": [...], "discogs_genres": [...], "musicbrainz_genres": [...]}

                if not singles_only:
                    log_info(f'Pre-fetching Last.fm + ListenBrainz data for album "{album}"')

                    # ------------------------------------------------------------
                    # STEP 1: ListenBrainz batch fetch ONCE per album
                    # ------------------------------------------------------------
                    try:
                        lb_batch = get_listenbrainz_batch_for_tracks(album_tracks)
                        log_debug(f"[LB] Batch fetched {len(lb_batch)} entries for album '{album}'")
                    except Exception as e:
                        log_debug(f"[LB] Batch fetch failed for album '{album}': {e}")
                        lb_batch = {}

                    # ------------------------------------------------------------
                    # STEP 2: Prefetch Last.fm + map ListenBrainz per track
                    # ------------------------------------------------------------
                    for track in album_tracks:
                        track_id = track["id"]
                        title = track.get("title", "")
                        track_artist = track.get("artist", "")

                        # -------------------------
                        # LAST.FM PREFETCH
                        # -------------------------
                        listeners = 0
                        playcount = 0

                        cached_lastfm = row_get(track, "lastfm_track_playcount", 0)
                        if cached_lastfm > 0:
                            listeners = cached_lastfm
                            playcount = 0
                            log_debug(f'Using cached Last.fm listeners for "{title}": {cached_lastfm}')
                        else:
                            try:
                                rate_limiter = get_rate_limiter()
                                can_proceed, reason = rate_limiter.check_lastfm_limit()

                                if not can_proceed:
                                    log_debug(f'Rate limit hit for "{title}": {reason}, waiting...')
                                    if rate_limiter.wait_if_needed_lastfm(max_wait_seconds=2.0):
                                        can_proceed = True
                                    else:
                                        log_debug(f'Rate limit still active for "{title}" after wait, skipping Last.fm prefetch')

                                if can_proceed:
                                    lastfm_info = _run_with_timeout(
                                        get_lastfm_track_info,
                                        API_CALL_TIMEOUT,
                                        f"Last.fm lookup timed out after {API_CALL_TIMEOUT}s",
                                        track_artist,
                                        normalize_title_for_lookup(title),
                                    )
                                    rate_limiter.record_lastfm_request()

                                    if lastfm_info:
                                        listeners = lastfm_info.get("listeners", 0) or 0
                                        playcount = lastfm_info.get("track_play", 0) or 0
                                        log_debug(f'Fetched Last.fm data for "{title}": listeners={listeners}, playcount={playcount}')
                            except Exception as e:
                                log_debug(f'Failed to fetch Last.fm data for "{title}": {e}')

                        album_lastfm_data[track_id] = {
                            "listeners": listeners,
                            "playcount": playcount,
                        }

                        # -------------------------
                        # LISTENBRAINZ PREFETCH MAP
                        # -------------------------
                        recording_mbid = (
                            row_get(track, "recording_mbid")
                            or row_get(track, "musicbrainz_recording_mbid")
                            or row_get(track, "mbid")
                        )

                        lb_listens = 0
                        if recording_mbid:
                            lb_entry = lb_batch.get(recording_mbid, {})
                            lb_listens = lb_entry.get("total_listen_count") or 0

                        album_listenbrainz_data[track_id] = {
                            "listeners": lb_listens,
                        }

                        log_debug(f'[LB] "{title}" ({recording_mbid}): listens={lb_listens}')

                    # ------------------------------------------------------------
                    # STEP 3: Prefetch summary
                    # ------------------------------------------------------------
                    fetched_lastfm_listeners = [d["listeners"] for d in album_lastfm_data.values() if d["listeners"] > 0]
                    fetched_lastfm_tracks = len(fetched_lastfm_listeners)
                    zero_lastfm_tracks = len([d for d in album_lastfm_data.values() if d["listeners"] == 0])

                    fetched_lb_listeners = [d["listeners"] for d in album_listenbrainz_data.values() if d["listeners"] > 0]
                    fetched_lb_tracks = len(fetched_lb_listeners)
                    zero_lb_tracks = len([d for d in album_listenbrainz_data.values() if d["listeners"] == 0])

                    log_info(
                        f'Pre-fetch complete for album "{album}": '
                        f'{fetched_lastfm_tracks} tracks with Last.fm listener data, {zero_lastfm_tracks} with zero/unavailable; '
                        f'{fetched_lb_tracks} tracks with ListenBrainz listens, {zero_lb_tracks} with zero/unavailable'
                    )

                    if fetched_lastfm_listeners:
                        log_debug(
                            f'Album Last.fm listener stats: min={min(fetched_lastfm_listeners)}, '
                            f'max={max(fetched_lastfm_listeners)}, '
                            f'avg={sum(fetched_lastfm_listeners) / len(fetched_lastfm_listeners):.0f}'
                        )

                    if fetched_lb_listeners:
                        log_debug(
                            f'Album ListenBrainz listen stats: min={min(fetched_lb_listeners)}, '
                            f'max={max(fetched_lb_listeners)}, '
                            f'avg={sum(fetched_lb_listeners) / len(fetched_lb_listeners):.0f}'
                        )

                    # ---------------------------------------------------------------------
                    # STEP 4: Batch tag/genre client initialisation
                    # ---------------------------------------------------------------------
                    lastfm_client = None
                    discogs_client = None
                    lb_tag_client = None

                    try:
                        # Last.fm client for tag lookups
                        lastfm_config = get_lastfm_config(config)
                        if lastfm_config.get("enabled") and lastfm_config.get("api_key"):
                            api_key = lastfm_config.get("api_key")
                            if api_key in ["your_lastfm_api_key", "YOUR_API_KEY", "<your_api_key>", ""]:
                                log_info("⚠️ Last.fm API key not configured (placeholder value detected)")
                                log_info("   Set a real API key in config.yaml under api_integrations.lastfm.api_key")
                            else:
                                from api_clients.lastfm import LastFmClient
                                lastfm_client = LastFmClient(api_key)
                                log_info("✓ Last.fm client initialized for tag fetching (API key configured)")
                        else:
                            if not lastfm_config.get("enabled"):
                                log_info("⚠️ Last.fm is disabled in config.yaml")
                            else:
                                log_info("⚠️ Last.fm API key missing from config.yaml")

                        # Discogs client for genre lookups
                        discogs_config = config.get("api_integrations", {}).get("discogs", {})
                        if discogs_config.get("enabled") and discogs_config.get("token"):
                            from api_clients.discogs import DiscogsClient
                            discogs_client = DiscogsClient(discogs_config.get("token"))
                            log_debug("Discogs client initialized for batch genre fetching")
                        else:
                            log_debug("Discogs client not configured or disabled")

                        # ListenBrainz client for recording tags
                        try:
                            from api_clients.audiodb_and_listenbrainz import ListenBrainzUserClient
                            lb_tag_client = ListenBrainzUserClient("")
                            log_debug("ListenBrainz tag client initialized for batch genre fetching")
                        except Exception as e:
                            log_debug(f"ListenBrainz tag client unavailable: {e}")
                            lb_tag_client = None

                    except Exception as e:
                        log_debug(f"Error initializing API clients for batch fetch: {e}")
                        log_info(f"Continuing with partial API capabilities for album '{album}'")

                    # ---------------------------------------------------------------------
                    # STEP 5: Batch tag / genre fetch
                    # ---------------------------------------------------------------------
                    try:
                        if lastfm_client or discogs_client or lb_tag_client:
                            log_info(f'Fetching tags/genres for {len(album_tracks)} track(s) in album "{album}"')
                        else:
                            log_debug("No API clients available for batch tag fetch, will use existing values if needed")

                        for track in (album_tracks if metadata_enrichment_enabled else []):
                            track_id = track["id"]
                            title = track["title"]
                            track_artist = track["artist"]
                            track_recording_mbid = (
                                row_get(track, "recording_mbid")
                                or row_get(track, "musicbrainz_recording_mbid")
                                or row_get(track, "mbid")
                            )
                            discogs_release_id = row_get(track, "discogs_release_id")

                            is_cover_song_tags, normalized_title_tags = detect_cover_and_normalize_title(title)
                            api_lookup_title_tags = normalized_title_tags

                            track_tags = {
                                "lastfm_tags": [],
                                "listenbrainz_genres": [],
                                "discogs_genres": [],
                                "musicbrainz_genres": [],
                            }

                            # -------------------------
                            # Last.fm tags
                            # -------------------------
                            if lastfm_client:
                                try:
                                    rate_limiter = get_rate_limiter()
                                    can_proceed, reason = rate_limiter.check_lastfm_limit()
                                    if not can_proceed:
                                        log_debug(f'Rate limit hit for Last.fm tags ({title}): {reason}, waiting...')
                                        if rate_limiter.wait_if_needed_lastfm(max_wait_seconds=2.0):
                                            can_proceed = True
                                        else:
                                            log_debug(f'Rate limit still active for Last.fm tags ({title}) after wait, skipping')

                                    if can_proceed:
                                        lastfm_tags = _run_with_timeout(
                                            lastfm_client.get_track_tags,
                                            5,
                                            "Last.fm tags lookup timed out",
                                            track_artist,
                                            api_lookup_title_tags,
                                            limit=10,
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

                            # -------------------------
                            # ListenBrainz genres/tags
                            # -------------------------
                            existing_lb_genres = row_get(track, "listenbrainz_genres")
                            if track_recording_mbid and not existing_lb_genres and lb_tag_client:
                                try:
                                    lb_genres = _run_with_timeout(
                                        lb_tag_client.get_recording_tags,
                                        5,
                                        "ListenBrainz genres lookup timed out",
                                        track_recording_mbid,
                                    )
                                    if lb_genres:
                                        track_tags["listenbrainz_genres"] = [
                                            {"name": t.get("tag", t.get("name", "")), "count": t.get("count", 0)}
                                            for t in lb_genres
                                            if t.get("tag") or t.get("name")
                                        ]
                                        log_debug(f'Fetched {len(track_tags["listenbrainz_genres"])} ListenBrainz genres for "{title}"')
                                except Exception as e:
                                    log_debug(f'Failed to fetch ListenBrainz genres for "{title}": {e}')

                            # -------------------------
                            # Discogs genres
                            # -------------------------
                            existing_discogs_genres = row_get(track, "discogs_genres")
                            discogs_genres_populated = False
                            if existing_discogs_genres:
                                try:
                                    dg_parsed = json.loads(existing_discogs_genres) if isinstance(existing_discogs_genres, str) else existing_discogs_genres
                                    discogs_genres_populated = bool(dg_parsed)
                                except (ValueError, TypeError):
                                    discogs_genres_populated = bool(existing_discogs_genres)

                            if album_discogs_genres:
                                track_tags["discogs_genres"] = album_discogs_genres
                                log_debug(f'Using album-level Discogs genres for "{title}" ({len(album_discogs_genres)} genres)')
                            elif discogs_genres_populated:
                                log_debug(f'Skipping Discogs genre fetch for "{title}": discogs_genres already populated')
                            elif discogs_client:
                                try:
                                    discogs_genres = None
                                    if discogs_release_id:
                                        discogs_genres = _run_with_timeout(
                                            discogs_client.get_release_genres_by_id,
                                            5,
                                            "Discogs genres by ID lookup timed out",
                                            discogs_release_id,
                                        )
                                    else:
                                        discogs_genres = _run_with_timeout(
                                            discogs_client.get_genres,
                                            5,
                                            "Discogs genres search lookup timed out",
                                            title,
                                            track_artist,
                                        )
                                        if isinstance(discogs_genres, list) and discogs_genres and isinstance(discogs_genres[0], str):
                                            discogs_genres = [{"name": g} for g in discogs_genres]

                                    if discogs_genres:
                                        track_tags["discogs_genres"] = discogs_genres
                                        log_debug(f'Fetched {len(discogs_genres)} Discogs genres per-track for "{title}" (heterogeneous album)')
                                except Exception as e:
                                    log_debug(f'Failed to fetch Discogs genres for "{title}": {e}')

                            if not track_tags.get("discogs_genres") and album_discogs_genres and is_homogeneous_album:
                                track_tags["discogs_genres"] = album_discogs_genres
                                log_debug(f'Applied album-level Discogs genres to "{title}" (no track-level result)')

                            # -------------------------
                            # MusicBrainz genres
                            # -------------------------
                            existing_mb_genres = row_get(track, "musicbrainz_genres")
                            mb_genres_populated = False
                            if existing_mb_genres:
                                try:
                                    mb_parsed = json.loads(existing_mb_genres) if isinstance(existing_mb_genres, str) else existing_mb_genres
                                    mb_genres_populated = bool(mb_parsed)
                                except (ValueError, TypeError):
                                    mb_genres_populated = bool(existing_mb_genres)

                            if not mb_genres_populated and HAVE_MUSICBRAINZ:
                                try:
                                    from api_clients.musicbrainz import MusicBrainzClient as _MBC
                                    mb_genre_client = _MBC()
                                    mb_genre_list = _run_with_timeout(
                                        mb_genre_client.get_genres,
                                        API_CALL_TIMEOUT,
                                        "MusicBrainz genre lookup timed out",
                                        track_artist,
                                        api_lookup_title_tags,
                                    )
                                    if mb_genre_list:
                                        track_tags["musicbrainz_genres"] = [
                                            {"name": g, "count": 0} if isinstance(g, str) else g
                                            for g in mb_genre_list
                                        ]
                                        log_debug(f'Fetched {len(mb_genre_list)} MusicBrainz genres for "{title}"')
                                except Exception as mb_g_err:
                                    log_debug(f'MusicBrainz genre fetch failed for "{title}": {mb_g_err}')

                            album_tags_data[track_id] = track_tags

                        if album_tags_data:
                            log_info(f'Tag/genre fetch complete for album "{album}": {len(album_tags_data)} track(s) processed')

                    except Exception as e:
                        log_debug(f'Error during tag/genre batch fetch for album "{album}": {e}')
                        log_info(f'Continuing with existing tag/genre data for this album')

                # ---------------------------------------------------------------------
                # STEP 6: Popularity scoring loop
                # ---------------------------------------------------------------------
                if not singles_only and not skip_popularity_for_album and not metadata_only:
                    track_updates = []

                    total_tracks = len(album_tracks)
                    tracks_processed = 0
                    milestone_25 = int(total_tracks * 0.25)
                    milestone_50 = int(total_tracks * 0.50)
                    milestone_75 = int(total_tracks * 0.75)
                    milestones_logged = set()

                    for track in album_tracks:
                        track_id = track["id"]
                        title = track["title"]
                        track_artist = track["artist"]

                        # Skip singles - they have separate prominence logic
                        if row_get(track, "is_single", 0):
                            log_debug(f'Skipping popularity scoring for single: "{title}" (already marked as is_single=1)')
                            continue

                        is_cover_song, normalized_title = detect_cover_and_normalize_title(title)
                        if is_cover_song:
                            log_debug(f'Cover song detected: "{title}" -> normalized to "{normalized_title}" for API lookups')

                        api_lookup_title = strip_search_parentheses(normalized_title)

                        log_info(f'🎵 Processing track: "{title}" (Track ID: {track_id})')
                        log_debug(f'Track details - id: {track_id}, title: {title}, album: {album}, artist: {track_artist}')

                        # ------------------------------------------------------------
                        # Full cache / mature-track freeze handling
                        # ------------------------------------------------------------
                        use_full_cache = False
                        if not (FORCE_RESCAN or force):
                            if should_freeze_mature_track_popularity(track, min_age_years=mature_track_freeze_cutoff_years):
                                cached_popularity = row_get(track, "popularity_score", 0)
                                if cached_popularity > 0:
                                    use_full_cache = True
                                    cached_spotify_score = row_get(track, "spotify_score", row_get(track, "spotify_popularity", 0))
                                    cached_lastfm_ratio = row_get(track, "lastfm_ratio", 0)
                                    cached_lastfm_listeners = row_get(track, "lastfm_track_playcount", 0)

                                    log_info(
                                        f'Keeping existing popularity for older track with completed Last.fm data: {title} '
                                        f'(year: {row_get(track, "year")}, Last.fm listeners: {cached_lastfm_listeners})'
                                    )

                                    if not row_get(track, "popularity_frozen", False):
                                        try:
                                            cursor.execute(
                                                f"UPDATE tracks SET popularity_frozen = TRUE, popularity_frozen_at = CURRENT_TIMESTAMP WHERE id = {placeholder}",
                                                (track_id,),
                                            )
                                            conn.commit()
                                        except Exception as freeze_err:
                                            log_debug(f"Could not persist freeze state for track {track_id}: {freeze_err}")
                                            try:
                                                conn.rollback()
                                            except Exception:
                                                pass

                                    track_updates.append((
                                        cached_popularity,
                                        cached_spotify_score,
                                        cached_lastfm_ratio,
                                        cached_lastfm_listeners,
                                        None,
                                        None,
                                        None,
                                        None,
                                        None,
                                        album_art_url,
                                        track_id,
                                    ))
                                    scanned_count += 1
                                    album_scanned += 1
                                    tracks_processed += 1

                                    if tracks_processed == milestone_25 and 25 not in milestones_logged:
                                        log_unified(f"Popularity Scan - 25% completed - {tracks_processed}/{total_tracks} songs")
                                        milestones_logged.add(25)
                                    elif tracks_processed == milestone_50 and 50 not in milestones_logged:
                                        log_unified(f"Popularity Scan - 50% completed - {tracks_processed}/{total_tracks} songs")
                                        milestones_logged.add(50)
                                    elif tracks_processed == milestone_75 and 75 not in milestones_logged:
                                        log_unified(f"Popularity Scan - 75% completed - {tracks_processed}/{total_tracks} songs")
                                        milestones_logged.add(75)

                                    continue

                            if should_use_cached_score(track, "popularity_score", "last_spotify_lookup"):
                                cached_popularity = row_get(track, "popularity_score", 0)
                                if cached_popularity > 0:
                                    use_full_cache = True
                                    cached_spotify_score = row_get(track, "spotify_score", row_get(track, "spotify_popularity", 0))
                                    cached_lastfm_ratio = row_get(track, "lastfm_ratio", 0)
                                    cached_lastfm_listeners = row_get(track, "lastfm_track_playcount", 0)

                                    log_info(f'Using complete cached popularity score for: {title} (score: {cached_popularity:.1f})')

                                    track_updates.append((
                                        cached_popularity,
                                        cached_spotify_score,
                                        cached_lastfm_ratio,
                                        cached_lastfm_listeners,
                                        None,
                                        None,
                                        None,
                                        None,
                                        None,
                                        album_art_url,
                                        track_id,
                                    ))
                                    scanned_count += 1
                                    album_scanned += 1
                                    tracks_processed += 1

                                    if tracks_processed == milestone_25 and 25 not in milestones_logged:
                                        log_unified(f"Popularity Scan - 25% completed - {tracks_processed}/{total_tracks} songs")
                                        milestones_logged.add(25)
                                    elif tracks_processed == milestone_50 and 50 not in milestones_logged:
                                        log_unified(f"Popularity Scan - 50% completed - {tracks_processed}/{total_tracks} songs")
                                        milestones_logged.add(50)
                                    elif tracks_processed == milestone_75 and 75 not in milestones_logged:
                                        log_unified(f"Popularity Scan - 75% completed - {tracks_processed}/{total_tracks} songs")
                                        milestones_logged.add(75)

                                    continue

                        # ------------------------------------------------------------
                        # Pull pre-fetched Last.fm data
                        # ------------------------------------------------------------
                        spotify_score = 0
                        spotify_release_date = None
                        lastfm_info = {}

                        lastfm_score = 0
                        lastfm_listeners = album_lastfm_data.get(track_id, {}).get("listeners", 0) or 0
                        lastfm_playcount = album_lastfm_data.get(track_id, {}).get("playcount", 0) or 0

                        if lastfm_listeners > 0:
                            log_debug(f'Last.fm raw data for "{title}": listeners={lastfm_listeners}, playcount={lastfm_playcount}')

                            album_listeners_list = [d["listeners"] for d in album_lastfm_data.values() if d["listeners"] > 0]
                            album_playcounts_list = [d["playcount"] for d in album_lastfm_data.values() if d["playcount"] > 0]

                            if album_listeners_list and album_playcounts_list:
                                lastfm_score = calculate_lastfm_zscore_popularity(
                                    lastfm_listeners,
                                    lastfm_playcount,
                                    album_listeners_list,
                                    album_playcounts_list,
                                )
                                log_info(
                                    f'Last.fm z-score popularity: {lastfm_score:.1f} '
                                    f'(listeners={lastfm_listeners}, playcount={lastfm_playcount}, album_tracks={len(album_listeners_list)})'
                                )
                            else:
                                lastfm_score = calculate_lastfm_popularity_score(lastfm_listeners)
                                log_info(f'Last.fm listeners (fallback): {lastfm_listeners} (score: {lastfm_score:.1f})')
                        else:
                            log_info(f'No Last.fm listeners data found for: {title}')

                        # ------------------------------------------------------------
                        # Use pre-fetched ListenBrainz data (NO per-track API call)
                        # ------------------------------------------------------------
                        lb_listens = album_listenbrainz_data.get(track_id, {}).get("listeners", 0) or 0
                        listenbrainz_score = 0
                        if lb_listens > 0:
                            listenbrainz_score = calculate_listenbrainz_popularity_score(lb_listens)
                            log_debug(f'ListenBrainz popularity for "{title}": {lb_listens} listens, score: {listenbrainz_score:.1f}')
                        else:
                            recording_mbid = (
                                row_get(track, "recording_mbid")
                                or row_get(track, "musicbrainz_recording_mbid")
                                or row_get(track, "mbid")
                            )
                            if recording_mbid:
                                log_debug(f'ListenBrainz returned 0 listens for "{title}" (MBID: {recording_mbid})')
                            else:
                                log_debug(f'No ListenBrainz MBID available for "{title}"')

                        # ------------------------------------------------------------
                        # Age score
                        # ------------------------------------------------------------
                        age_score = 0
                        local_year = row_get(track, "year", None)
                        if local_year and not spotify_release_date:
                            spotify_release_date = str(local_year)

                        if spotify_release_date:
                            try:
                                base_score = spotify_score if spotify_score > 0 else 1
                                age_score, days_since = score_by_age(base_score, spotify_release_date)
                                log_debug(
                                    f'Age score calculated: {age_score:.1f} '
                                    f'(release date: {spotify_release_date}, days since: {days_since})'
                                )
                            except Exception as e:
                                log_debug(f"Age score calculation failed for '{title}': {e}")

                        # ------------------------------------------------------------
                        # Genre / tag values from batch fetch
                        # ------------------------------------------------------------
                        spotify_genres_json = row_get(track, "spotify_artist_genres") or row_get(track, "spotify_genres")
                        lastfm_tags_json = row_get(track, "lastfm_tags")
                        listenbrainz_genres_json = row_get(track, "listenbrainz_genres")
                        discogs_genres_json = row_get(track, "discogs_genres")
                        musicbrainz_genres_json = row_get(track, "musicbrainz_genres")

                        if metadata_enrichment_enabled and track_id in album_tags_data:
                            tags_dict = album_tags_data[track_id]
                            if tags_dict.get("lastfm_tags"):
                                lastfm_tags_json = json.dumps(tags_dict["lastfm_tags"])
                            if tags_dict.get("listenbrainz_genres"):
                                listenbrainz_genres_json = json.dumps(tags_dict["listenbrainz_genres"])
                            if tags_dict.get("discogs_genres"):
                                discogs_genres_json = json.dumps(tags_dict["discogs_genres"])
                            if tags_dict.get("musicbrainz_genres"):
                                musicbrainz_genres_json = json.dumps(tags_dict["musicbrainz_genres"])

                        # ------------------------------------------------------------
                        # Dynamic weighting logic
                        # ------------------------------------------------------------
                        dynamic_lastfm_weight = LASTFM_WEIGHT
                        listeners_for_weighting = int(lastfm_listeners or 0)

                        if artist_lastfm_context and artist_lastfm_context.get("track_count", 0) > 0 and listeners_for_weighting > 0:
                            dynamic_lastfm_weight = get_dynamic_lastfm_weight(
                                artist_lastfm_context,
                                listeners_for_weighting,
                                LASTFM_WEIGHT,
                            )
                            if dynamic_lastfm_weight != LASTFM_WEIGHT:
                                log_info(
                                    f"Dynamic weight adjustment for artist context: "
                                    f"Last.fm {LASTFM_WEIGHT:.2f}→{dynamic_lastfm_weight:.2f}"
                                )

                        album_context_live = int(row_get(track, "album_context_live") or 0)
                        is_live_track = int(row_get(track, "is_live") or 0)

                        if (album_context_live or is_live_track) and lastfm_score > 0:
                            live_album_weight_reduction = 0.50
                            dynamic_lastfm_weight = dynamic_lastfm_weight * (1.0 - live_album_weight_reduction)
                            log_info(
                                f'Live album penalty applied: Last.fm weight reduced by {live_album_weight_reduction * 100:.0f}% '
                                f'(new weight: {dynamic_lastfm_weight:.2f})'
                            )

                        # ------------------------------------------------------------
                        # Final weighted popularity calculation
                        # ------------------------------------------------------------
                        scores = []
                        weights = []

                        if lastfm_score > 0:
                            scores.append(lastfm_score)
                            weights.append(dynamic_lastfm_weight)
                            log_debug(f'Including Last.fm score: {lastfm_score:.1f} (weight: {dynamic_lastfm_weight:.2f})')

                        if listenbrainz_score > 0:
                            # supplementary if Last.fm exists, primary if Last.fm absent
                            lb_weight = 0.40 if lastfm_score > 0 else dynamic_lastfm_weight
                            scores.append(listenbrainz_score)
                            weights.append(lb_weight)
                            log_debug(f'Including ListenBrainz score: {listenbrainz_score:.1f} (weight: {lb_weight:.2f})')

                        if age_score > 0:
                            scores.append(age_score)
                            weights.append(AGE_WEIGHT)
                            log_debug(f'Including age score: {age_score:.1f} (weight: {AGE_WEIGHT:.2f})')

                        if scores and weights:
                            total_weight = sum(weights)
                            popularity_score = sum(s * w for s, w in zip(scores, weights)) / total_weight

                            track_updates.append((
                                popularity_score,
                                spotify_score,
                                lastfm_score,
                                lastfm_listeners,
                                spotify_genres_json,
                                lastfm_tags_json,
                                listenbrainz_genres_json,
                                discogs_genres_json,
                                musicbrainz_genres_json,
                                album_art_url,
                                track_id,
                            ))

                            scanned_count += 1
                            album_scanned += 1

                            log_info(f'Track scanned successfully: "{title}" (weighted: {popularity_score:.1f})')
                            log_debug(
                                f'Weighted popularity calculation - '
                                f'lastfm: {lastfm_score:.1f}, '
                                f'listenbrainz: {listenbrainz_score:.1f}, '
                                f'age: {age_score:.1f}, '
                                f'weighted: {popularity_score:.1f}'
                            )
                        else:
                            log_info(f'No popularity score found for {artist} - {title}')
                            log_debug('No data sources available for scoring')

                        # ------------------------------------------------------------
                        # Milestone progress logging
                        # ------------------------------------------------------------
                        tracks_processed += 1
                        if tracks_processed == milestone_25 and 25 not in milestones_logged:
                            log_unified(f"Popularity Scan - 25% completed - {tracks_processed}/{total_tracks} songs")
                            milestones_logged.add(25)
                        elif tracks_processed == milestone_50 and 50 not in milestones_logged:
                            log_unified(f"Popularity Scan - 50% completed - {tracks_processed}/{total_tracks} songs")
                            milestones_logged.add(50)
                        elif tracks_processed == milestone_75 and 75 not in milestones_logged:
                            log_unified(f"Popularity Scan - 75% completed - {tracks_processed}/{total_tracks} songs")
                            milestones_logged.add(75)

                else:
                    if skip_popularity_for_album:
                        log_info(f"Popularity already scanned for album '{album}'; running singles detection / metadata only")
                    else:
                        log_info(f"Singles-only or metadata-only mode for album '{album}': popularity scoring skipped")
                    track_updates = []  

                # Batch update all popularity scores and genre sources for this album in one commit (skipped in singles_only mode)
                if track_updates and not singles_only and not metadata_only:
                    track_rows_by_id = {track.get('id'): track for track in album_tracks}
                    updated_track_updates = []

                    for update_tuple in track_updates:
                        (
                            popularity_score,
                            spotify_score,
                            lastfm_ratio,
                            lastfm_track_playcount,
                            spotify_genres,
                            lastfm_tags,
                            listenbrainz_genres,
                            discogs_genres,
                            musicbrainz_genres,
                            album_art_url,
                            track_id,
                        ) = update_tuple

                        current_track_row = track_rows_by_id.get(track_id, {})
                        current_title = current_track_row.get("title", "")

                        # ── Step 1: Merge fetched tags ────────────────────────────────────────
                        if track_id in album_tags_data:
                            tags_data = album_tags_data[track_id]
                            if tags_data.get("lastfm_tags"):
                                lastfm_tags = json.dumps(tags_data["lastfm_tags"])
                            if tags_data.get("listenbrainz_genres"):
                                listenbrainz_genres = json.dumps(tags_data["listenbrainz_genres"])
                            if tags_data.get("discogs_genres"):
                                discogs_genres = json.dumps(tags_data["discogs_genres"])
                            if tags_data.get("musicbrainz_genres"):
                                musicbrainz_genres = json.dumps(tags_data["musicbrainz_genres"])

                        # ── Step 2: Genre injections (all happen before change-detection) ─────

                        # Cover genre
                        current_is_cover_song, _ = detect_cover_and_normalize_title(current_title)
                        if current_is_cover_song:
                            try:
                                mb_genres_list = json.loads(musicbrainz_genres) if musicbrainz_genres and musicbrainz_genres != "null" else []
                                if "Cover" not in mb_genres_list:
                                    mb_genres_list.insert(0, "Cover")
                                    musicbrainz_genres = json.dumps(mb_genres_list)
                                    log_debug(f'Added "Cover" genre to track: {current_title}')
                            except (json.JSONDecodeError, TypeError):
                                musicbrainz_genres = json.dumps(["Cover"])
                                log_debug(f'Initialized genres with "Cover" for track: {current_title}')

                        # Live / Acoustic genre
                        if album_live_genre:
                            try:
                                mb_genres_list = json.loads(musicbrainz_genres) if musicbrainz_genres and musicbrainz_genres != "null" else []
                                if album_live_genre not in mb_genres_list:
                                    mb_genres_list.insert(0, album_live_genre)
                                    musicbrainz_genres = json.dumps(mb_genres_list)
                                    log_debug(f'Ensured "{album_live_genre}" genre in batch update for track: {current_title}')
                            except (json.JSONDecodeError, TypeError):
                                musicbrainz_genres = json.dumps([album_live_genre])

                        # Christmas genre
                        _xmas_keywords = [
                            "christmas", "xmas", "yuletide", "holiday season", "jingle bells",
                            "silent night", "deck the halls", "winter wonderland", "feliz navidad",
                            "rudolph", "santa claus", "sleigh bells",
                        ]
                        _title_lower = str(current_title or "").lower()
                        _album_lower = str(album or "").lower()
                        if any(w in _title_lower or w in _album_lower for w in _xmas_keywords):
                            try:
                                mb_genres_list = json.loads(musicbrainz_genres) if musicbrainz_genres and musicbrainz_genres != "null" else []
                                if "Christmas" not in mb_genres_list:
                                    mb_genres_list.insert(0, "Christmas")
                                    musicbrainz_genres = json.dumps(mb_genres_list)
                                    log_debug(f'Added "Christmas" genre to track: {current_title}')
                            except (json.JSONDecodeError, TypeError):
                                musicbrainz_genres = json.dumps(["Christmas"])

                        # ── Step 3: Skip if nothing changed ──────────────────────────────────
                        proposed_values = {
                            "popularity_score": popularity_score,
                            "spotify_score": spotify_score,
                            "lastfm_ratio": lastfm_ratio,
                            "lastfm_track_playcount": lastfm_track_playcount,
                            "spotify_genres": spotify_genres,
                            "lastfm_tags": lastfm_tags,
                            "listenbrainz_genres": listenbrainz_genres,
                            "discogs_genres": discogs_genres,
                            "musicbrainz_genres": musicbrainz_genres,
                            "cover_art_url": album_art_url,
                        }

                        if not popularity_values_changed(current_track_row, proposed_values):
                            log_debug(f"Skipping no-op popularity update for track {track_id}: no changes detected")
                            continue

                        # ── Step 4: Single append ─────────────────────────────────────────────
                        updated_track_updates.append((
                            popularity_score,
                            spotify_score,
                            lastfm_ratio,
                            lastfm_track_playcount,
                            spotify_genres,
                            lastfm_tags,
                            listenbrainz_genres,
                            discogs_genres,
                            musicbrainz_genres,
                            album_art_url,
                            track_id,
                        ))

                    # ── Commit ────────────────────────────────────────────────────────────────
                    if updated_track_updates:
                        try:
                            _execute_tracks_batch_with_retry(
                                conn,
                                cursor,
                                f"UPDATE tracks SET "
                                f"popularity_score = {placeholder}, "
                                f"spotify_score = {placeholder}, "
                                f"lastfm_ratio = {placeholder}, "
                                f"lastfm_track_playcount = {placeholder}, "
                                f"spotify_genres = {placeholder}, "
                                f"lastfm_tags = {placeholder}, "
                                f"listenbrainz_genres = {placeholder}, "
                                f"discogs_genres = {placeholder}, "
                                f"musicbrainz_genres = {placeholder}, "
                                f"cover_art_url = {placeholder} "
                                f"WHERE id = {placeholder}",
                                sorted(updated_track_updates, key=lambda row: str(row[-1])),
                                f"popularity batch update for album {album}",
                            )
                            conn.commit()

                                # Sync popularity values back into in-memory track dicts so that the
                                # singles detection phase immediately sees the updated scores.
                            updated_popularity_by_id = {
                                row[10]: {
                                    "popularity_score": row[0],
                                    "spotify_score":    row[1],
                                    "lastfm_ratio":     row[2],
                                    "lastfm_track_playcount": row[3],
                                }
                                for row in updated_track_updates
                            }
                            for track in album_tracks:
                                if track.get("id") in updated_popularity_by_id:
                                    track.update(updated_popularity_by_id[track["id"]])

                            log_debug(
                                f"Batch committed {len(updated_track_updates)} popularity updates "
                                f"for album '{album}' with merged tag data"
                            )

                        except Exception as e:
                            log_debug(f"Error batch updating popularity scores: {e}")
                            try:
                                conn.rollback()
                                log_debug("Rolled back failed transaction")
                            except Exception:
                                pass
                            raise

                        else:
                            log_debug(f"Skipped batch update for album '{album}': no popularity-related fields changed")

                        if album_art_url:
                            log_info(f"[ALBUM_ART] Album art URL cached for {album}: {album_art_url}")
                        else:
                            log_debug("[ALBUM_ART] Album art will be fetched on-demand from Navidrome or Apple Music sources")
                            # Update artist progress tracking after completing all albums for this artist
                            # Note: Progress is saved once per artist (not per track) to balance granularity
                            # with I/O efficiency. Original code saved after every track which could result
                            # in thousands of writes for large libraries. Per-artist updates provide adequate
                            # progress visibility while reducing file I/O by orders of magnitude.
                            # If scan is interrupted, it can resume from the last completed artist.
                            processed_artists += 1
                            save_popularity_progress(
                                processed_artists,
                                total_artists,
                                current_artist=artist,
                                progress_file=progress_file_path,
                                scan_type=caller_scan_type or progress_scan_type,
                                # Record this artist as the last *completed* checkpoint so that
                                # if the process is killed before the next artist's in-progress
                                # marker is written, resume will start from here rather than
                                # looping back to the same stuck artist.
                                last_completed_artist=artist,
                            )
                            log_debug(f"Progress saved - {processed_artists}/{total_artists} artists processed (current: {artist})")

                            log_debug("Committing final changes to database")
                            conn.commit()

                # ---------------------------------------------------------------------
                # STEP 7: Singles detection & star rating
                # ---------------------------------------------------------------------
                if not metadata_only and not popularity_only:
                    try:
                        album_median, album_stddev, _, album_count = calculate_album_stats(conn, artist, album)
                        artist_mean, artist_stddev, artist_count = calculate_artist_stats(conn, artist)
                    except Exception as e:
                        log_debug(f"Could not compute album/artist stats for star rating: {e}")
                        album_median = album_stddev = artist_mean = artist_stddev = 0.0
                        album_count = artist_count = 0

                    album_is_underperforming = False
                    if album_median > 0 and artist_mean > 0:
                        album_is_underperforming = album_median < (artist_mean * UNDERPERFORMING_THRESHOLD)

                    # Check which tracks still need single detection
                    track_ids_in_album = [t["id"] for t in album_tracks]
                    id_placeholders = ", ".join([placeholder] * len(track_ids_in_album))
                    assessed_map = {}
                    try:
                        cursor.execute(
                            f"SELECT id, single_detection_last_updated FROM tracks WHERE id IN ({id_placeholders})",
                            track_ids_in_album,
                        )
                        for row in cursor.fetchall():
                            assessed_map[row_get(row, "id")] = row_get(row, "single_detection_last_updated")
                    except Exception as e:
                        log_debug(f"Could not fetch single detection status: {e}")

                    any_detection_run = False
                    total_album_tracks = len(album_tracks)
                    detection_processed = 0
                    detection_milestone_25 = int(total_album_tracks * 0.25) if total_album_tracks > 0 else 0
                    detection_milestone_50 = int(total_album_tracks * 0.50) if total_album_tracks > 0 else 0
                    detection_milestone_75 = int(total_album_tracks * 0.75) if total_album_tracks > 0 else 0
                    detection_milestones_logged = set()

                    for track in album_tracks:
                        track_id = track["id"]
                        title = track["title"]
                        track_artist = track["artist"]

                        needs_detection = force or singles_only or singles_with_missing_popularity
                        if not needs_detection:
                            if not assessed_map.get(track_id):
                                needs_detection = True

                        if not needs_detection:
                            continue

                        any_detection_run = True
                        detection_processed += 1

                        if detection_processed == detection_milestone_25 and 25 not in detection_milestones_logged:
                            log_info(f"Single Detection - 25% completed - {detection_processed}/{total_album_tracks} tracks")
                            detection_milestones_logged.add(25)
                        elif detection_processed == detection_milestone_50 and 50 not in detection_milestones_logged:
                            log_info(f"Single Detection - 50% completed - {detection_processed}/{total_album_tracks} tracks")
                            detection_milestones_logged.add(50)
                        elif detection_processed == detection_milestone_75 and 75 not in detection_milestones_logged:
                            log_info(f"Single Detection - 75% completed - {detection_processed}/{total_album_tracks} tracks")
                            detection_milestones_logged.add(75)

                        try:
                            detection_result = detect_single_for_track(
                                title=title,
                                artist=track_artist,
                                album_track_count=len(album_tracks),
                                verbose=verbose,
                                discogs_token=discogs_token,
                                track_id=track_id,
                                album=album,
                                isrc=row_get(track, "isrc"),
                                duration=row_get(track, "duration"),
                                popularity=row_get(track, "popularity_score", 0) or 0,
                                album_type=album_type_from_field,
                                use_advanced_detection=True,
                                album_is_underperforming=album_is_underperforming,
                                artist_median_popularity=artist_mean,
                                existing_conn=conn,
                                persist_result=True,
                                mb_cached_singles=mb_artist_singles_normalized,
                            )
                            track["is_single"] = 1 if detection_result.get("is_single") else 0
                            track["single_confidence"] = detection_result.get("confidence", "low")
                            track["single_sources"] = json.dumps(detection_result.get("sources", []))
                        except Exception as e:
                            log_debug(f"Single detection failed for '{title}': {e}")
                            track["is_single"] = 0
                            track["single_confidence"] = "low"
                            track["single_sources"] = "[]"

                    if any_detection_run:
                        log_info(f"🔍 Singles detection completed for '{artist} - {album}'")

                    # Compute star ratings for every track (recomputed each scan)
                    for track in album_tracks:
                        track_id = track["id"]
                        title = track["title"]
                        popularity_score = row_get(track, "popularity_score", 0) or 0

                        album_z = 0.0
                        artist_z = 0.0
                        if album_stddev > 0:
                            album_z = (popularity_score - album_median) / album_stddev
                        if artist_stddev > 0 and artist_count >= 5:
                            artist_z = (popularity_score - artist_mean) / artist_stddev

                        stars = 0
                        is_single = row_get(track, "is_single", 0)
                        single_confidence = row_get(track, "single_confidence", "low") or "low"

                        if single_confidence == "user":
                            stars = 5
                        elif is_single and single_confidence == "high":
                            top_pct_threshold = STANDOUT_CONFIG.get("star_5_single", {}).get("artist_pct", 0.25)
                            is_top_catalog = False
                            if artist_count > 0 and popularity_score > 0:
                                try:
                                    cursor.execute(
                                        f"SELECT COUNT(*) as total, SUM(CASE WHEN popularity_score >= {placeholder} THEN 1 ELSE 0 END) as above FROM tracks WHERE artist = {placeholder} AND popularity_score > 0",
                                        (popularity_score, artist),
                                    )
                                    row_stats = cursor.fetchone()
                                    total_cat = row_get(row_stats, "total", 0) or 0
                                    above_cat = row_get(row_stats, "above", 0) or 0
                                    if total_cat > 0:
                                        is_top_catalog = (above_cat / total_cat) <= top_pct_threshold
                                except Exception as e:
                                    log_debug(f"Could not compute artist percentile for '{title}': {e}")
                            stars = 5 if is_top_catalog else 4
                        elif (album_z >= STANDOUT_CONFIG.get("popularity_5star_z_threshold", 2.0)
                              and not row_get(track, "is_live", 0)
                              and not row_get(track, "album_context_live", 0)):
                            stars = 5
                        else:
                            if album_z >= STANDOUT_CONFIG["star_5"]["album_z"] and artist_z >= STANDOUT_CONFIG["star_5"]["artist_z"]:
                                stars = 5
                            elif album_z >= STANDOUT_CONFIG["star_4"]["album_z"] and artist_z >= STANDOUT_CONFIG["star_4"]["artist_z"]:
                                stars = 4
                            elif album_z >= STANDOUT_CONFIG["star_3"]["album_z"]:
                                stars = 3
                            elif STANDOUT_CONFIG["star_2"].get("album_mean") and popularity_score >= album_median:
                                stars = 2
                            else:
                                stars = 1

                        track["stars"] = stars
                        track["album_z"] = album_z
                        track["artist_z"] = artist_z

                        try:
                            cursor.execute(
                                f"UPDATE tracks SET stars = {placeholder} WHERE id = {placeholder}",
                                (stars, track_id),
                            )
                        except Exception as e:
                            log_debug(f"Failed to persist stars for '{title}': {e}")

                    try:
                        conn.commit()
                    except Exception as e:
                        log_debug(f"Failed to commit singles/stars for '{album}': {e}")
                        try:
                            conn.rollback()
                        except Exception:
                            pass

                    # Sync ratings to Navidrome when enabled
                    if _is_sync_ratings_to_all_users_enabled():
                        for track in album_tracks:
                            try:
                                sync_track_rating_to_navidrome(track["id"], track.get("stars", 0))
                            except Exception as e:
                                log_debug(f"Failed to sync rating for {track['id']}: {e}")

                    # Summary logging
                    star_counts = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
                    for t in album_tracks:
                        s = t.get("stars", 0) or 0
                        if 1 <= s <= 5:
                            star_counts[s] += 1
                    log_info(
                        f"Star Ratings - Album '{album}' by {artist}: "
                        f"5★: {star_counts[5]}, 4★: {star_counts[4]}, 3★: {star_counts[3]}, "
                        f"2★: {star_counts[2]}, 1★: {star_counts[1]}"
                    )
                    singles_detected = [t for t in album_tracks if t.get("is_single")]
                    if singles_detected:
                        log_info(f"Singles Detection - Detected {len(singles_detected)} single(s) in '{album}'")

                    # Detailed per-track single detection scan logging
                    if album_tracks:
                        detected_singles = []
                        popular_songs = []
                        rest_of_album = []
                        for t in album_tracks:
                            stars = t.get("stars", 0) or 0
                            is_single = t.get("is_single", 0)
                            single_confidence = t.get("single_confidence", "low") or "low"
                            title = t.get("title", "")
                            track_artist = t.get("artist", "")
                            album_z = t.get("album_z", 0.0)
                            artist_z = t.get("artist_z", 0.0)
                            sources_str = t.get("single_sources", "")
                            try:
                                sources = json.loads(sources_str) if sources_str else []
                            except Exception:
                                sources = []
                            reasons = []
                            if is_single and single_confidence == "high":
                                if sources:
                                    reasons.append(", ".join(sources))
                                if album_z:
                                    reasons.append(f"album-z-score: {album_z:.2f}")
                                reason_str = " (" + "; ".join(reasons) + ")" if reasons else ""
                                detected_singles.append((title, stars, track_artist, reason_str))
                            elif stars == 5:
                                if album_z:
                                    reasons.append(f"album-z-score: {album_z:.2f}")
                                reason_str = " (" + "; ".join(reasons) + ")" if reasons else ""
                                popular_songs.append((title, stars, track_artist, reason_str))
                            else:
                                if album_z:
                                    reasons.append(f"album-z-score: {album_z:.2f}")
                                reason_str = " (" + "; ".join(reasons) + ")" if reasons else ""
                                rest_of_album.append((title, stars, track_artist, reason_str))

                        if detected_singles:
                            log_unified(f"Single Detection Scan - ===== {album} - Detected Singles =====")
                            for title, stars, track_artist, reason in detected_singles:
                                star_str = "★" * stars + "☆" * (5 - stars)
                                log_unified(f"Single Detection Scan - {star_str:<5} {track_artist} - {title}{reason}")
                        if popular_songs:
                            log_unified(f"Single Detection Scan - ===== {album} - Popular Songs (Not Detected as Single) =====")
                            for title, stars, track_artist, reason in popular_songs:
                                star_str = "★" * stars + "☆" * (5 - stars)
                                log_unified(f"Single Detection Scan - {star_str:<5} {track_artist} - {title}{reason}")
                        if rest_of_album:
                            if detected_singles or popular_songs:
                                log_unified(f"Single Detection Scan - ===== {album} - Rest of Album =====")
                            else:
                                log_unified(f"Single Detection Scan - ===== {album} - All Tracks =====")
                            for title, stars, track_artist, reason in rest_of_album:
                                star_str = "★" * stars + "☆" * (5 - stars)
                                log_unified(f"Single Detection Scan - {star_str:<5} {track_artist} - {title}{reason}")

        # PostgreSQL commit above is sufficient; no manual checkpoint required.

        log_unified(f"Popularity Scan - Complete: {scanned_count} tracks updated, {skipped_count} albums skipped")
        log_info(f"Popularity scan completed: {scanned_count} tracks updated, {skipped_count} albums skipped (already scanned)")
        log_debug(f"Scan statistics - scanned: {scanned_count}, skipped: {skipped_count}, total_artists: {total_artists}")

        # Write final progress state (marks scan as completed).
        # Skip this when caller_scan_type is set — in that case the calling scan
        # (e.g. combined_scan) owns the progress file lifecycle and will write its
        # own final state. Writing is_running=False here would cause a brief window
        # where the dashboard incorrectly shows the parent scan as finished.
        if not caller_scan_type:
            try:
                progress_data = {
                    "is_running": False,
                    "scan_type": progress_scan_type,
                    "processed_artists": total_artists,
                    "total_artists": total_artists,
                    "percent_complete": 100,
                    "current_artist": None  # Clear current artist when scan completes
                }
                with open(progress_file_path, 'w') as f:
                    json.dump(progress_data, f)
                log_debug(f"Final progress state written to {progress_file_path}")
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


def detect_covers_for_artist(artist_name: str, conn: object) -> int:
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
        placeholder = "%s"

        # 1. Get all tracks for this artist with composers
        cursor.execute(f"""
            SELECT id, title, composer, artist
            FROM tracks
            WHERE artist = {placeholder} AND composer IS NOT NULL AND composer != ''
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
            cursor.execute(f"""
                SELECT artist FROM tracks
                WHERE title = {placeholder} AND composer = {placeholder} AND artist != {placeholder} AND composer IS NOT NULL
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
    Create/refresh 'Essential {artist}' smart playlist using Navidrome's 0-5 rating scale.

    Logic:
      - Case A: if artist has >= 10 five-star tracks, build a pure 5-star essentials playlist.
      - Case B: if total tracks >= 100, build top 10% essentials sorted by rating.
      - Otherwise: delete any existing playlist (requirements not met).

    Args:
        artist_name: Name of the artist
        tracks: List of track dictionaries with id, artist, album, title, stars
    """
    # Skip playlist creation for compilation/various artists entries
    if artist_name.lower() == "various artists":
        return

    total_tracks = len(tracks)
    # Count a track as five-star if SPTNR's own rating OR the rating stored from
    # Navidrome (which may include user-set manual ratings) is 5.  This ensures
    # popularity-based 5★ tracks AND user-rated 5★ tracks both contribute to the
    # essential-playlist threshold, regardless of how they earned the rating.
    five_star_tracks = [
        t for t in tracks
        if max((t.get("stars") or 0), (t.get("navidrome_rating") or 0)) == 5
    ]
    playlist_name = f"Essential {artist_name}"

    # CASE A - 10+ five-star tracks -> purely 5-star essentials (always update)
    if len(five_star_tracks) >= 10:
        _delete_nsp_file(playlist_name)
        # Navidrome NSP format requires one field per criterion object.
        # Split artist and rating into separate "all" entries (AND logic) so
        # Navidrome evaluates both conditions correctly.
        playlist_data = {
            "name": playlist_name,
            "comment": "Auto-generated by SPTNR",
            "all": [
                {"is": {"artist": artist_name}},
                {"is": {"rating": 5}}
            ],
            "sort": "random"
        }
        _create_nsp_file(playlist_name, playlist_data)
        log_basic(f"Essential playlist created for '{artist_name}' (5-star essentials)")
        return

    # CASE B - 100+ total tracks -> top 10% by rating (always update)
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

    # Requirements not met - delete any existing playlist
    log_basic(
        f"No Essential playlist created for '{artist_name}' "
        f"(total={total_tracks}, five-star={len(five_star_tracks)})"
    )
    _delete_nsp_file(playlist_name)


def refresh_all_playlists_from_db():
    """
    Refresh all smart playlists for all artists from DB cache (no track rescans).
    This function pulls distinct artists that have cached tracks and updates their playlists.
    """
    log_basic("Refreshing smart playlists for all artists from DB cache (no track rescans)...")

    # Pull distinct artists that have cached tracks
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        sql_placeholder = "%s"
        cursor.execute("SELECT DISTINCT COALESCE(NULLIF(album_artist, ''), artist) AS artist_name FROM tracks")
        artists = [row['artist_name'] for row in cursor.fetchall()]

        if not artists:
            log_basic("No cached tracks in DB. Skipping playlist refresh.")
            return

        for name in artists:
            cursor.execute(
                f"""SELECT stars, navidrome_rating
                   FROM tracks
                   WHERE COALESCE(NULLIF(album_artist, ''), artist) = {sql_placeholder}
                   LIMIT 10000""",
                (name,)
            )
            rows = cursor.fetchall()

            if not rows:
                log_basic(f"No cached tracks found for '{name}', skipping.")
                continue

            tracks = [
                {
                    "stars": int(r['stars']) if r['stars'] else 0,
                    "navidrome_rating": int(r['navidrome_rating']) if r['navidrome_rating'] else 0,
                }
                for r in rows
            ]
            create_or_update_playlist_for_artist(name, tracks)
            log_basic(f"Playlist refreshed for '{name}' ({len(tracks)} tracks)")
    except Exception as e:
        log_basic(f"Error refreshing playlists: {e}")
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

    # Print startup banner with configuration
    log_info("\n" + "="*80)
    log_info("🎵 SPTNR POPULARITY SCANNER")
    log_info("="*80)
    log_info(f"📂 LOG FILES:")
    log_info(f"   - Unified: {UNIFIED_LOG_PATH}")
    log_info(f"   - Info: {INFO_LOG_PATH}")
    log_info(f"   - Debug: {DEBUG_LOG_PATH}")
    log_info(f"⚙️ SCAN PARAMETERS:")
    log_info(f"   - Verbose: {args.verbose}")
    log_info(f"   - Force rescan: {args.force}")
    log_info("="*80 + "\n")

    popularity_scan(verbose=args.verbose, force=args.force)
