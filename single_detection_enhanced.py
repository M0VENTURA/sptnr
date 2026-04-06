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
from statistics import mean, stdev, median
from datetime import datetime

# Import centralized logging functions
# Use centralized logging to ensure API activity appears in unified_scan.log, info.log, and debug.log
# instead of Python's default logging system which doesn't route to these files
from helpers.logging_config import log_unified, log_info, log_debug
from helpers.helpers import strip_cover_attribution
from database_abstraction import is_postgres_connection

logger = logging.getLogger(__name__)


# ============================================================================
# Artist Stats Caching (Performance Improvement)
# ============================================================================
# Cache artist stats to avoid recalculating for each track
# This improves performance from O(n×m×k) to O(n) where k = tracks per artist
_artist_stats_cache = {}
_album_stats_cache = {}


def get_cached_artist_stats(conn, artist: str) -> Tuple[float, float, int]:
    """
    Get artist stats from cache, or calculate and cache if not present.
    
    Args:
        conn: Database connection
        artist: Artist name
        
    Returns:
        Tuple of (mean, stddev, count) from cache
    """
    if artist not in _artist_stats_cache:
        _artist_stats_cache[artist] = calculate_artist_stats(conn, artist)
    return _artist_stats_cache[artist]


def clear_artist_stats_cache():
    """Clear cached detection stats. Call between albums/scan runs."""
    global _artist_stats_cache, _album_stats_cache
    _artist_stats_cache.clear()
    _album_stats_cache.clear()


def get_cached_album_stats(conn, artist: str, album: str) -> Tuple[float, float, float, int]:
    """
    Get album stats from cache, or calculate and cache if not present.

    Args:
        conn: Database connection
        artist: Artist name
        album: Album title

    Returns:
        Tuple of (mean, stddev, median, count)
    """
    cache_key = (artist, album)
    if cache_key not in _album_stats_cache:
        _album_stats_cache[cache_key] = calculate_album_stats(conn, artist, album)
    return _album_stats_cache[cache_key]


# ============================================================================
# Input Validation & Sanitization
# ============================================================================

def validate_track_data(
    track_id: str,
    title: str,
    artist: str,
    album: str,
    popularity: float
) -> Tuple[bool, Optional[str]]:
    """
    Validate required track data before processing.
    
    Args:
        track_id: Track ID
        title: Track title
        artist: Artist name
        album: Album name
        popularity: Track popularity (0-100)
        
    Returns:
        Tuple of (is_valid, error_message)
        - (True, None) if valid
        - (False, error_msg) if invalid
    """
    # Validate required fields
    if not track_id or not isinstance(track_id, str):
        return False, "Invalid track_id: must be non-empty string"
    
    if not title or not isinstance(title, str):
        return False, f"Invalid title for track {track_id}: must be non-empty string"
    
    if not artist or not isinstance(artist, str):
        return False, f"Invalid artist for track {track_id}: must be non-empty string"
    
    if not album or not isinstance(album, str):
        return False, f"Invalid album for track {track_id}: must be non-empty string"
    
    # Validate popularity score
    if not isinstance(popularity, (int, float)):
        return False, f"Invalid popularity for {title}: must be numeric"
    
    # Allow tiny floating-point drift (e.g. 100.00000000000001).
    epsilon = 1e-6
    if popularity < -epsilon or popularity > 100 + epsilon:
        return False, f"Invalid popularity for {title}: must be 0-100, got {popularity}"
    
    return True, None


def validate_artist_stats(
    artist: str,
    mean_val: float,
    stddev_val: float,
    track_count: int
) -> Tuple[bool, Optional[str]]:
    """
    Validate artist statistics before use in calculations.
    
    Args:
        artist: Artist name
        mean_val: Artist mean popularity
        stddev_val: Artist standard deviation
        track_count: Number of tracks in artist catalog
        
    Returns:
        Tuple of (is_valid, error_message)
    """
    if not artist or not isinstance(artist, str):
        return False, "Invalid artist name"
    
    if not isinstance(mean_val, (int, float)) or mean_val < 0:
        return False, f"Invalid mean popularity for {artist}: {mean_val}"
    
    if not isinstance(stddev_val, (int, float)) or stddev_val < 0:
        return False, f"Invalid stddev for {artist}: {stddev_val}"
    
    if not isinstance(track_count, int) or track_count < 0:
        return False, f"Invalid track count for {artist}: {track_count}"
    
    # Warn if track count too low for reliable statistics
    if track_count < 3:
        log_debug(f"[VALIDATION] Warning: {artist} has only {track_count} tracks, z-scores may be unreliable")
    
    return True, None


# ============================================================================
# Dynamic Z-Score Threshold Calculation
# ============================================================================

def get_dynamic_z_threshold(
    track_count: int,
    release_year: Optional[int] = None,
    is_compilation: bool = False
) -> float:
    """
    Calculate dynamic z-score threshold based on catalog size and release date.
    
    Rationale:
    - Larger catalogs: stricter threshold (more data = more confident detection)
    - Smaller catalogs: relaxed threshold (fewer data points = less reliable)
    - Pre-2000 releases: relaxed threshold (sparse Last.fm data)
    - Compilations: stricter threshold (higher baseline popularity)
    
    Args:
        track_count: Number of tracks in artist catalog
        release_year: Optional release year of track
        is_compilation: Whether this is a compilation album
        
    Returns:
        Dynamic z-score threshold (typically 1.5-2.5)
    """
    # Base threshold depends on catalog size
    if track_count < 5:
        # Very small catalog: very relaxed threshold
        threshold = 1.5
    elif track_count < 10:
        # Small catalog: relaxed threshold
        threshold = 1.8
    elif track_count < 50:
        # Medium catalog: normal threshold
        threshold = 2.0
    elif track_count < 200:
        # Large catalog: stricter threshold
        threshold = 1.9
    else:
        # Very large catalog: very strict threshold
        threshold = 1.8
    
    # Age adjustment: pre-2000 releases are less reliable (sparse Last.fm data)
    if release_year and release_year < 2000:
        # Reduce threshold by 0.2-0.3 for pre-2000 releases
        years_before_2000 = 2000 - release_year
        age_reduction = min(0.3, years_before_2000 * 0.02)  # ~2% per year, max 0.3
        threshold = max(1.2, threshold - age_reduction)
    
    # Compilation adjustment: stricter threshold (popularity spread usually lower)
    if is_compilation:
        threshold = min(threshold + 0.2, 2.5)  # Compilations need higher standout score
    
    log_debug(f"[THRESHOLD] Dynamic z-threshold: {threshold:.2f} (tracks={track_count}, year={release_year}, compilation={is_compilation})")
    
    return threshold


# ============================================================================
# Configuration Loading
# ============================================================================

def get_source_confidence_settings(config_data: Optional[Dict] = None) -> Dict[str, str]:
    """
    Load source confidence level settings from config.
    
    Args:
        config_data: Optional config dict (if not provided, will be loaded from config file)
        
    Returns:
        Dict mapping source names to confidence levels ('low', 'medium', 'high')
        Default: all sources are 'medium' except Discogs and radio/single edit, which are 'high'
    """
    defaults = {
        'discogs': 'high',
        'musicbrainz': 'medium',
        'discogs_video': 'medium',
        'lastfm': 'medium',
        'radio_edit': 'high'
    }
    
    if not config_data:
        try:
            import os
            from helpers.config_loader import load_config
            config_data = load_config()
        except Exception as e:
            log_debug(f"Unable to load config for source confidence settings: {e}")
            return defaults
    
    # Extract source confidence settings from config features
    features = config_data.get('features', {})
    settings = {}
    
    source_map = {
        'source_discogs_confidence': 'discogs',
        'source_musicbrainz_confidence': 'musicbrainz',
        'source_discogs_video_confidence': 'discogs_video',
        'source_lastfm_confidence': 'lastfm',
        'source_radio_edit_confidence': 'radio_edit'
    }
    
    for config_key, source_name in source_map.items():
        config_value = features.get(config_key, defaults.get(source_name, 'medium'))
        if isinstance(config_value, str) and config_value.lower() in ('low', 'medium', 'high'):
            settings[source_name] = config_value.lower()
        else:
            settings[source_name] = defaults.get(source_name, 'medium')
    
    return settings


def check_high_confidence_dynamic(
    discogs_confirmed: bool,
    musicbrainz_confirmed: bool,
    discogs_video_confirmed: bool,
    lastfm_confirmed: bool,
    radio_edit_found: bool,
    source_confidence_settings: Dict[str, str],
    musicbrainz_compilation_confirmed: bool = False
) -> bool:
    """
    Check if HIGH confidence has been achieved based on current confirmed sources.
    Called dynamically after each source confirmation.
    
    Args:
        discogs_confirmed: Whether Discogs confirmed
        musicbrainz_confirmed: Whether MusicBrainz confirmed
        discogs_video_confirmed: Whether Discogs Video confirmed
        lastfm_confirmed: Whether Last.fm confirmed
        radio_edit_found: Whether Radio Edit found
        source_confidence_settings: Dict of source names to confidence levels
        musicbrainz_compilation_confirmed: Whether MusicBrainz VA compilation confirmed
        
    Returns:
        True if HIGH confidence achieved, False otherwise
    """
    # Check if any HIGH-confidence source confirmed
    if discogs_confirmed and source_confidence_settings.get('discogs') == 'high':
        log_debug(f"[DETECT] AUTO-STOP: Discogs confirmed with HIGH confidence setting")
        return True
    
    if musicbrainz_confirmed and source_confidence_settings.get('musicbrainz') == 'high':
        log_debug(f"[DETECT] AUTO-STOP: MusicBrainz confirmed with HIGH confidence setting")
        return True
    
    if discogs_video_confirmed and source_confidence_settings.get('discogs_video') == 'high':
        log_debug(f"[DETECT] AUTO-STOP: Discogs Video confirmed with HIGH confidence setting")
        return True
    
    if lastfm_confirmed and source_confidence_settings.get('lastfm') == 'high':
        log_debug(f"[DETECT] AUTO-STOP: Last.fm confirmed with HIGH confidence setting")
        return True
    
    if radio_edit_found and source_confidence_settings.get('radio_edit') == 'high':
        log_debug(f"[DETECT] AUTO-STOP: Radio Edit found with HIGH confidence setting")
        return True
    
    # Check if 2+ medium sources confirmed (rule: 2 medium = high)
    medium_sources_confirmed = sum([
        1 for confirmed, setting in [
            (discogs_confirmed, source_confidence_settings.get('discogs')),
            (musicbrainz_confirmed, source_confidence_settings.get('musicbrainz')),
            (musicbrainz_compilation_confirmed, 'medium'),
            (discogs_video_confirmed, source_confidence_settings.get('discogs_video')),
            (lastfm_confirmed, source_confidence_settings.get('lastfm')),
            (radio_edit_found, source_confidence_settings.get('radio_edit'))
        ]
        if confirmed and setting in ('medium', 'high')
    ])
    
    if medium_sources_confirmed >= 2:
        log_debug(f"[DETECT] AUTO-STOP: {medium_sources_confirmed} medium/high sources confirmed (HIGH confidence)")
        return True
    
    return False


def has_single_or_radio_edit_marker(title: str) -> bool:
    """Return True when title contains canonical single/radio edit markers."""
    if not title or not isinstance(title, str):
        return False
    return bool(
        re.search(
            r"\b(?:radio\s+(?:edit|mix|version)|single\s+(?:version|edit|mix))\b",
            title,
            re.IGNORECASE,
        )
    )


# ============================================================================
# Live/Acoustic Detection for Genre Tagging
# ============================================================================

def detect_live_or_acoustic_recording(
    title: str,
    album: str,
    genres: str = "",
    spotify_results: Optional[List[Dict]] = None
) -> List[str]:
    """
    Detect if a track is a live or acoustic recording based on title, album, or genres.
    Returns a list of applicable tags: ['Live'], ['Acoustic'], ['Unplugged'], etc.
    
    Used during import to automatically tag tracks with live/acoustic versions.
    
    Args:
        title: Track title
        album: Album name
        genres: Comma-separated string of current genres
        spotify_results: Optional Spotify search results for additional context
        
    Returns:
        List of detected tags (e.g., ['Live'], ['Acoustic', 'Unplugged'])
    """
    tags = []
    
    # Combine all text for pattern matching
    all_text = f"{title} {album} {genres}".lower()
    
    # Patterns for live performance
    live_patterns = [
        r'\blive\b',
        r'\bconcert\b',
        r'\bperforman[cs]e\b',
        r'\bfrom\s+(?:the\s+)?(?:live\s+)?(?:concert|show|session)',
        r'(?:live\s+)?(?:at|from)\s+(?:.*?)?(?:festival|arena|hall|theater|theatre|venue|hotel|studio|sessions?)',
        r'\(live',
        r'\[live'
    ]
    
    for pattern in live_patterns:
        if re.search(pattern, all_text):
            tags.append('Live')
            break
    
    # Patterns for acoustic
    acoustic_patterns = [
        r'\bacoustic\b',
        r'\bunplugged\b',
        r'\bstripped\b',
        r'\bac\b',  # Common abbreviation
    ]
    
    for pattern in acoustic_patterns:
        if re.search(pattern, all_text):
            if 'Acoustic' not in tags:
                tags.append('Acoustic')
            if 'Unplugged' in all_text and 'Unplugged' not in tags:
                tags.append('Unplugged')
            break
    
    # Check Spotify results for explicit metadata
    if spotify_results:
        for result in spotify_results:
            name_lower = result.get('name', '').lower()
            if 'live' in name_lower and 'Live' not in tags:
                tags.append('Live')
            if ('acoustic' in name_lower or 'unplugged' in name_lower) and 'Acoustic' not in tags:
                tags.append('Acoustic')
            # Check if album is marked as live
            album_data = result.get('album', {})
            if album_data and 'live' in album_data.get('name', '').lower() and 'Live' not in tags:
                tags.append('Live')
    
    return tags


def should_exclude_from_single_detection(genres: str, is_live_release: bool = False) -> bool:
    """
    Determine if a track should be excluded from single detection based on its genre tags.
    
    Rules:
    - If track has 'live' or 'acoustic' genre tags AND the release is NOT marked as live
    - Then exclude from single detection
    
    Args:
        genres: Comma-separated string of genres for the track
        is_live_release: Whether the album itself is a live release
        
    Returns:
        True if track should be excluded from single detection, False otherwise
    """
    if not genres or is_live_release:
        return False
    
    genre_list = [g.strip() for g in genres.split(',')]
    genre_list_lower = [g.lower() for g in genre_list]
    
    exclude_tags = ['live', 'acoustic', 'unplugged']
    
    for tag in exclude_tags:
        if tag in genre_list_lower:
            return True
    
    return False


# ============================================================================
# Stage 6: Strict Version Matching Rules
# ============================================================================

# Constants for safe title normalization for known release variants
STRIPPABLE_SUFFIXES = [
    "radio edit",
    "single edit",
    "edit",
    "single version",
    "radio version",
    "radio mix",
]

SEPARATORS = [" - ", " (", " ["]

# Import improved matching utilities for better normalization and matching
try:
    from matching_utils import (
        normalize_title as normalize_title_advanced,
        normalize_artist,
        normalize_string,
        calculate_duration_similarity,
        calculate_track_similarity,
        is_fuzzy_match,
        ROMAN_NUMERAL_PATTERN,
        PUNCTUATION_SUFFIX_PATTERN
    )
    MATCHING_UTILS_AVAILABLE = True
except ImportError:
    log_debug("matching_utils not available, using legacy normalization")
    MATCHING_UTILS_AVAILABLE = False
    # Fallback patterns if matching_utils not available
    ROMAN_NUMERAL_PATTERN = r'\s+(I{1,3}|IV|V|VI{0,3}|IX|X{1,3}|XI{0,3}|XIV|XV|XVI{0,3}|XIX|XX)\s*$'
    PUNCTUATION_SUFFIX_PATTERN = r'([!+?]+)\s*$'


def strip_release_variant_suffix(title: str) -> str:
    """
    Strip known release variant suffixes from title for metadata matching.
    
    Only removes suffixes that:
    1. Match the STRIPPABLE_SUFFIXES list (Radio Edit, Single Version, etc.)
    2. Are preceded by one of the SEPARATORS (e.g., " - ", " (", " [")
    3. Appear at the END of the title
    
    This ensures legitimate song titles like "8-bit Version" or "Acoustic Version"
    are NOT affected, since "Version" would not be preceded by a recognized separator.
    
    Args:
        title: Original track title
        
    Returns:
        Title with release variant suffix stripped (if applicable)
        
    Examples:
        "Spin (Radio Edit)" → "Spin"
        "Track - Single Version" → "Track"
        "Acoustic Version" → "Acoustic Version" (NOT stripped, no separator)
        "8-bit Version" → "8-bit Version" (NOT stripped, no separator)
        "Live Version (Radio Mix)" → "Live Version" (only radio mix portion stripped)
    """
    if not title:
        return title
    
    # Check each separator and strippable suffix combo
    for separator in SEPARATORS:
        for suffix in STRIPPABLE_SUFFIXES:
            # Build the pattern: separator + suffix + optional whitespace at end
            # Example: " (radio edit)" or " - single version"
            pattern = re.escape(separator) + r'\s*' + re.escape(suffix) + r'\s*$'
            
            # Search case-insensitively
            match = re.search(pattern, title, re.IGNORECASE)
            if match:
                # Remove the matched suffix (separator + suffix)
                return title[:match.start()].rstrip()
    
    # No strippable suffix found, return original
    return title


def normalize_title_strict(title: str) -> str:
    """
    Normalize title per problem statement Stage 6.
    
    Now enhanced with Unicode normalization and accent removal when matching_utils available.
    Applies safe stripping of known release variants BEFORE normalization.
    
    IMPORTANT: Preserves title suffixes like "!", "+", "?", and Roman numerals (I, II, III, etc.)
    to ensure different songs are not matched as the same track.
    
    Processing order:
    1. Strip known release variant suffixes (Radio Edit, Single Version, etc.)
    2. Apply full normalization (punctuation, accents, case, etc.)
    3. Preserve special suffixes (!, +, ?, Roman numerals)
    
    - lowercase
    - remove accents (Unicode NFD decomposition)
    - remove punctuation (except trailing !, +, ?)
    - remove bracketed suffixes
    - collapse whitespace
    - strip leading articles (a, an, the)
    - preserve Roman numerals at end
    """
    # Step 1: Strip known release variant suffixes FIRST (before other normalization)
    # This ensures "Spin (Radio Edit)" matches "Spin" in metadata
    title = strip_release_variant_suffix(title)
    
    if MATCHING_UTILS_AVAILABLE:
        # Use advanced normalization with Unicode accent removal
        return normalize_title_advanced(title)
    
    # Legacy normalization (fallback)
    # Preserve trailing punctuation suffixes (!, +, ?) before normalization
    preserved_suffix = ""
    suffix_match = re.search(PUNCTUATION_SUFFIX_PATTERN, title)
    if suffix_match:
        preserved_suffix = suffix_match.group(1)
        title = title[:suffix_match.start()]
    
    # Preserve Roman numerals at the end (I, II, III, IV, V, etc.) before normalization
    roman_suffix = ""
    roman_match = re.search(ROMAN_NUMERAL_PATTERN, title, re.IGNORECASE)
    if roman_match:
        roman_suffix = " " + roman_match.group(1).lower()  # Preserve as lowercase
        title = title[:roman_match.start()]
    
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
    # Strip leading articles (a, an, the)
    normalized = re.sub(r'^(?:a|an|the)\s+', '', normalized)
    # Collapse whitespace
    normalized = re.sub(r'\s+', ' ', normalized)
    
    # Re-attach preserved suffixes
    if roman_suffix:
        normalized = normalized + roman_suffix
    if preserved_suffix:
        normalized = normalized + preserved_suffix
    
    return normalized


# Keywords that indicate a non-canonical modifier INCOMPATIBLE with the remastered bypass.
# Tracks containing these modifiers (after stripping remastered) are NOT treated as the
# original studio release and will not bypass z-score gating.
_REMASTER_INCOMPATIBLE_MODIFIERS = [
    r'\blive\b', r'\bunplugged\b', r'\bacoustic\b', r'\borchestral\b',
    r'\bsymphonic\b', r'\bremix\b', r'\bdemo\b', r'\binstrumental\b',
    r'\bkaraoke\b',
]


def strip_remaster_suffix(title: str) -> str:
    """Strip remastered/remaster markers from a track title to get the base title.

    Handles patterns such as:
      - "Higher (remastered 2024)"         → "Higher"
      - "Higher (radio edit / remastered 2024)" → "Higher (radio edit)"
      - "Song - Remastered 2024"           → "Song"
    """
    result = title
    # Remove standalone "(remastered [year])" parenthetical
    result = re.sub(r'\s*\(\s*remaster(?:ed)?(?:\s+\d{4})?\s*\)', '', result, flags=re.IGNORECASE)
    # Remove "/ remastered [year]" or "- remastered [year]" inside parentheticals
    result = re.sub(r'\s*[/\-]\s*remaster(?:ed)?(?:\s+\d{4})?', '', result, flags=re.IGNORECASE)
    # Remove trailing "- Remastered [year]" (dash-separated title suffix)
    result = re.sub(r'\s*-\s*remaster(?:ed)?(?:\s+\d{4})?\s*$', '', result, flags=re.IGNORECASE)
    # Clean up empty parentheses left over after stripping
    result = re.sub(r'\(\s*\)', '', result)
    return result.strip()


def is_remastered_only_variant(title: str) -> bool:
    """Return True when the track title is a remastered version with no other
    non-canonical modifiers (live, acoustic, remix, etc.).

    Such tracks should be scanned exactly like the original release — their lower
    Spotify popularity merely reflects that listeners stream the original, not that
    the song is not a single.

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
    # If the remaining base title still has non-canonical (incompatible) modifiers
    # this is NOT a simple remastered variant.
    base_lower = base.lower()
    for pattern in _REMASTER_INCOMPATIBLE_MODIFIERS:
        if re.search(pattern, base_lower):
            return False
    return True



def is_non_canonical_version_strict(title: str) -> bool:
    """
    Check if title contains non-canonical version markers per Stage 6.
    Reject: remix, acoustic, live, unplugged, orchestral, symphonic,
            demo, instrumental, edit, extended, version, alt, alternate, mix

    EXCEPTION: Allow (radio edit) and (single) in brackets/parentheses
    as these are canonical single versions that should not be excluded.

    REMASTER HANDLING: Remastered versions are treated as the original release
    (same song, improved audio quality).  All remaster markers are stripped
    before the non-canonical check so that titles like:
      - "Higher (remastered 2024)"       → treated as "Higher"         ✓
      - "Higher (radio edit / remastered 2024)" → treated as "Higher (radio edit)" ✓
      - "Roadhouse Blues (live / remastered 2024)" → still rejected (has 'live') ✓
    """
    title_lower = title.lower()

    # PRE-PROCESSING: Strip all remaster/remastered markers before checking
    # non-canonical patterns.  Remastered versions are the same song as the
    # original; only the audio quality differs, making them valid single candidates.
    temp_title = title_lower
    # Remove standalone "(remastered [year])" parenthetical, e.g. "(remastered 2024)"
    temp_title = re.sub(r'\(\s*remaster(?:ed)?(?:\s+\d{4})?\s*\)', '', temp_title)
    # Remove "/ remastered [year]" or "- remastered [year]" inside/after parentheticals,
    # e.g. "radio edit / remastered 2024" → "radio edit"
    temp_title = re.sub(r'[/\-]\s*remaster(?:ed)?(?:\s+\d{4})?', '', temp_title)
    # Remove any remaining standalone remaster keywords not already covered above
    # e.g. "- Remastered 2024" at the end of the title (dash-separated suffix form)
    temp_title = re.sub(r'\bremaster(?:ed)?(?:\s+\d{4})?\b', '', temp_title)
    # Clean up empty parentheses left over after stripping, e.g. "()"
    temp_title = re.sub(r'\(\s*\)', '', temp_title)
    temp_title = temp_title.strip()

    # Check for additional allowed parenthetical tags that should NOT cause rejection
    # These are canonical single versions.
    # Use \s* inside to handle trailing whitespace left after remaster-stripping,
    # e.g. "(radio edit )" becomes canonical after stripping "/ remastered 2024".
    allowed_parenthetical_tags = [
        r'\(radio\s+edit\s*\)',
        r'\(single\s*\)',
    ]

    for allowed_pattern in allowed_parenthetical_tags:
        temp_title = re.sub(allowed_pattern, '', temp_title)

    # Now check for non-canonical version markers in the modified title
    patterns = [
        r'\bremix\b', r'\bacoustic\b', r'\blive\b',
        r'\bunplugged\b', r'\borchestral\b', r'\bsymphonic\b',
        r'\bdemo\b', r'\binstrumental\b', r'\bedit\b', r'\bextended\b',
        r'\bversion\b', r'\balt\b', r'\balternate\b', r'\bmix\b'
    ]
    return any(re.search(p, temp_title) for p in patterns)


def duration_matches_strict(duration1: Optional[float], duration2: Optional[float]) -> bool:
    """
    Duration must match within ±2 seconds per Stage 6.
    
    Enhanced to use advanced duration similarity calculation when available.
    """
    if duration1 is None or duration2 is None:
        return True  # Can't verify
    
    if MATCHING_UTILS_AVAILABLE:
        # Use advanced duration similarity (0.90-1.0 within 3 seconds)
        similarity = calculate_duration_similarity(duration1, duration2)
        return similarity >= 0.90
    
    # Legacy: strict ±2 seconds
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

# Keywords for detecting special edition/deluxe/expanded albums
# Tracks from these albums should not be marked as singles by Discogs alone
SPECIAL_EDITION_KEYWORDS = [
    "deluxe",
    "expanded",
    "reissue",
    "anniversary",
    "bonus",
    "special edition",
    "extended edition",
    "tour edition",
    "limited edition",
    "collector's edition",
    "remastered"
]

# Base keywords for pattern matching (used in colon+edition detection)
# These are the core qualifiers that indicate a special edition
SPECIAL_EDITION_BASE_KEYWORDS = [
    "deluxe",
    "special",
    "expanded",
    "limited",
    "collector",
    "tour",
    "bonus"
]


def is_compilation_album(album_type: Optional[str], album_title: str, track_count: int) -> bool:
    """
    Detect if an album is a compilation or greatest hits album.
    
    Compilation detection criteria:
    - If album_type == "compilation" (from MusicBrainz/Spotify)
    - OR album title contains compilation keywords
    
    Note: Track count is not used for compilation detection, as regular studio
    albums can have >12 tracks. True compilations should be tagged as such in
    metadata or have "greatest hits"/"best of" in the title.
    
    Args:
        album_type: Spotify/MusicBrainz album type (if available)
        album_title: Album title
        track_count: Number of tracks in the album (unused but kept for compatibility)
        
    Returns:
        True if album is a compilation
    """
    # Check album type
    if album_type and 'compilation' in album_type.lower():
        return True
    
    # Check album title for keywords
    album_lower = album_title.lower()
    for keyword in COMPILATION_KEYWORDS:
        if keyword in album_lower:
            return True
    
    # No compilation indicators found
    return False


def is_special_edition_album(album_title: str) -> bool:
    """
    Detect if an album is a special edition, deluxe, or expanded release.
    
    These albums often contain bonus tracks or alternate versions that were not
    released as singles. Single detection should be more conservative for these.
    
    Detection criteria:
    1. Contains special edition keywords (deluxe, expanded, reissue, etc.)
    2. Album title has format "Original: Bonus Edition" (colon followed by edition)
    
    Args:
        album_title: Album title
        
    Returns:
        True if album appears to be a special edition
    """
    album_lower = album_title.lower()
    
    # Check for special edition keywords
    for keyword in SPECIAL_EDITION_KEYWORDS:
        if keyword in album_lower:
            return True
    
    # Check for pattern "Original Album: Something Edition"
    # This catches cases like "Viva la Vida: Prospekt's March Edition"
    # but not "Greatest Hits: First Edition" or "Live: Studio Edition"
    if ':' in album_title:
        # Split by colon and check if "edition" appears in the part after the colon
        parts = album_title.split(':', 1)
        if len(parts) > 1:
            after_colon = parts[1].lower()
            # "edition" should be in the text after the colon
            if 'edition' in after_colon:
                # Make sure it's not a common album type like "First Edition" or "Studio Edition"
                # by checking if it has qualifying words before "edition"
                words_before_edition = after_colon.split('edition')[0].strip()
                # Check if this is likely a special edition:
                # 1. Multiple words before "edition" (e.g., "Prospekt's March Edition")
                # 2. OR contains a qualifier keyword (e.g., "Deluxe Edition", "Special Edition")
                # This avoids false positives like "First Edition" or "Studio Edition"
                if len(words_before_edition.split()) > 1 or any(kw in after_colon for kw in SPECIAL_EDITION_BASE_KEYWORDS):
                    return True
    
    # No special edition indicators found
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


def calculate_album_stats(conn, artist: str, album: str) -> Tuple[float, float, float, int]:
    """
    Calculate album popularity statistics for pre-filter.
    
    Returns:
        Tuple of (mean, stddev, median, count)
    """
    placeholder = "%s"
    try:
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT popularity_score
            FROM tracks
            WHERE COALESCE(NULLIF(album_artist, ''), artist) = {placeholder} AND album = {placeholder} AND popularity_score > 0
        """, (artist, album))
        popularities = [row['popularity_score'] for row in cursor.fetchall()]
    except Exception as e:
        logger.warning(f"[SINGLE_DETECT] calculate_album_stats DB error for {artist!r}/{album!r}: {e} — rolling back")
        try:
            conn.rollback()
        except Exception:
            pass
        return 0.0, 0.0, 0.0, 0

    if len(popularities) < 2:
        return 0.0, 0.0, 0.0, len(popularities)
    
    album_mean = mean(popularities)
    album_stddev = stdev(popularities)
    album_median = median(popularities)
    
    return album_mean, album_stddev, album_median, len(popularities)


def calculate_artist_stats(conn, artist: str) -> Tuple[float, float, int]:
    """
    Calculate artist-level popularity statistics across entire catalogue.
    
    Filters out live/remix/alternate versions to ensure statistics reflect
    the core catalog and are not skewed by bonus tracks or alternate versions.
    
    Returns:
        Tuple of (mean, stddev, count)
    """
    placeholder = "%s"
    try:
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT popularity_score, title, album
            FROM tracks
            WHERE artist = {placeholder} AND popularity_score > 0
        """, (artist,))
        rows = cursor.fetchall()
    except Exception as e:
        logger.warning(f"[SINGLE_DETECT] calculate_artist_stats DB error for {artist!r}: {e} — rolling back")
        try:
            conn.rollback()
        except Exception:
            pass
        return 0.0, 0.0, 0

    # Filter out live/remix/alternate tracks before calculating statistics
    # Use word boundary matching to avoid false positives
    popularities = []
    for row in rows:
        popularity_score = row['popularity_score']
        title = (row['title']) or ""
        album = (row['album']) or ""
        
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
    placeholder = "%s"
    cursor = conn.cursor()
    cursor.execute(f"""
        SELECT spotify_version_count
        FROM tracks
        WHERE COALESCE(NULLIF(album_artist, ''), artist) = {placeholder} AND album = {placeholder} AND spotify_version_count IS NOT NULL
    """, (artist, album))
    
    version_counts = [row['spotify_version_count'] for row in cursor.fetchall()]
    
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
    album_median: float,
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
    - popularity >= (album_median - 0.5 * album_stddev) [allows underperforming singles]
    
    Uses median instead of mean for robustness against outliers (very popular singles).
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
    
    # Threshold check - use median MINUS 0.5*stddev to capture underperforming singles
    # Median is more robust than mean - not pulled up by very popular singles
    # Singles often have lower popularity than album tracks but are still worth checking
    threshold = album_median - (0.5 * album_stddev)
    if popularity >= threshold:
        return True
    
    return False


# ============================================================================
# Stage 5: Popularity-Based Inference
# ============================================================================

def calculate_z_score_strict(popularity: float, pop_median: float, pop_mad_scaled: float) -> float:
    """
    Calculate z-score for a track using median + MAD (Median Absolute Deviation).
    
    This provides robust statistical measurement less susceptible to outliers than mean+stddev.
    
    Args:
        popularity: Track popularity score
        pop_median: Median popularity (album or artist level)
        pop_mad_scaled: MAD scaled by 1.4826 to be comparable to stddev (album or artist level)
        
    Returns:
        Z-score value (0 if MAD is 0)
    """
    if pop_mad_scaled == 0:
        return 0.0
    return (popularity - pop_median) / pop_mad_scaled


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
    - album_z >= 0.5 OR artist_z >= 1.4
    
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
        
        # MEDIUM: album_z >= 0.5 OR artist_z >= 1.0 (per ARTIST_LEVEL_ZSCORE_IMPLEMENTATION.md)
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
    musicbrainz_confirmed: bool,
    album_z: float,
    artist_z: float,
    spotify_version_count: int,  # retained for version-count standout heuristic
    album_is_underperforming: bool = False,
    is_artist_level_standout: bool = False,
    discogs_video_confirmed: bool = False,
    lastfm_single_confirmed: bool = False,
    musicbrainz_video_confirmed: bool = False,
    musicbrainz_compilation_confirmed: bool = False,
    popularity: float = 0.0,
    album_mean: float = 0.0,
    has_metadata: bool = False,
    radio_edit_found: bool = False,
    is_remastered_only: bool = False
) -> str:
    """
    Final single status based on source detection and z-score analysis.
    
    UNIFIED Z-SCORE LOGIC (same evidence required for all z > 0 ranges):
    - Z-score >= 1: Requires 2 MEDIUM confidence sources OR 1 HIGH confidence source
    - Z-score 0-1: Requires 2 MEDIUM confidence sources OR 1 HIGH confidence source
    - Z-score < 0: Single detection skipped (handled earlier in pipeline)
    
    Z-score is a gate only — it does NOT lower the metadata evidence bar.
    A high z-score with only 1 medium source (e.g. just MusicBrainz) is NOT enough.
    
    REMASTER EXCEPTION:
    - Remastered-only variants bypass the z<=0 gate and are evaluated as if z is in the
      0-1 range (require 1 HIGH or 2 MEDIUM sources).  Their low popularity is expected
      and reflects the streaming preference for the original, not the absence of single status.
    
    HIGH-CONFIDENCE SOURCES:
    - Discogs confirms exact track version as a single
    - popularity >= album_mean + 6 AND has_metadata
    
    MEDIUM-CONFIDENCE SOURCES:
    - MusicBrainz confirms (strict, release_group.type == "Single")
    - MusicBrainz video relationship
    - MusicBrainz compilation appearances (3+ Various Artists albums)
    - Discogs music video confirms
    - Last.fm single confirmation (album has 1-3 tracks)
    - Radio Edit found in Spotify search results
    
    Args:
        discogs_confirmed: Whether Discogs confirms this is a single
        musicbrainz_confirmed: Whether MusicBrainz confirms this is a single
        musicbrainz_video_confirmed: Whether MB URL-relation video found
        musicbrainz_compilation_confirmed: Whether MB VA compilation appearances found
        album_z: Album-level z-score
        artist_z: Artist-level z-score
        spotify_version_count: Number of Spotify versions found
        album_is_underperforming: Whether album is underperforming vs artist median
        is_artist_level_standout: Whether track exceeds artist median popularity
        discogs_video_confirmed: Whether Discogs confirms this has a music video
        lastfm_single_confirmed: Whether Last.fm confirms album has 1-3 tracks (single indicator)
        popularity: Track popularity score
        album_mean: Album mean popularity
        has_metadata: Whether track has any metadata sources
        radio_edit_found: Whether a Radio Edit version was found in Spotify search results
        is_remastered_only: Whether the track is a remastered-only variant (bypasses z<=0 gate)
        
    Returns:
        Confidence level: 'high', 'medium', 'low', or 'none'
    """
    # Determine the z-score threshold based on artist z-score
    # Higher z-score = lower source requirements
    max_z = max(album_z, artist_z)
    
    # Count high-confidence and medium-confidence sources
    high_confidence_count = 0
    medium_confidence_count = 0
    
    # HIGH-CONFIDENCE SOURCES:
    # Discogs confirms exact track version as single
    if discogs_confirmed:
        high_confidence_count += 1
        log_debug(f"[CONFIDENCE] +1 high: Discogs confirmed")
    
    # popularity >= album_mean + 6 AND has_metadata
    if has_metadata and popularity >= (album_mean + 6):
        high_confidence_count += 1
        log_debug(f"[CONFIDENCE] +1 high: Popularity check (pop={popularity:.1f} >= album_mean+6={album_mean+6:.1f})")
    
    # MEDIUM-CONFIDENCE SOURCES:
    # MusicBrainz confirms
    if musicbrainz_confirmed:
        medium_confidence_count += 1
        log_debug(f"[CONFIDENCE] +1 medium: MusicBrainz confirmed")
    
    # MusicBrainz video relationship
    if musicbrainz_video_confirmed:
        medium_confidence_count += 1
        log_debug(f"[CONFIDENCE] +1 medium: MusicBrainz video relationship confirmed")

    # MusicBrainz Various Artists compilation appearances
    if musicbrainz_compilation_confirmed:
        medium_confidence_count += 1
        log_debug(f"[CONFIDENCE] +1 medium: MusicBrainz compilation appearances confirmed")

    # Discogs music video confirms
    if discogs_video_confirmed:
        medium_confidence_count += 1
        log_debug(f"[CONFIDENCE] +1 medium: Discogs video confirmed")
    
    # Last.fm album track count (1-3 tracks = single indicator)
    if lastfm_single_confirmed:
        medium_confidence_count += 1
        log_debug(f"[CONFIDENCE] +1 medium: Last.fm single confirmed")
    
    # Radio Edit found in Spotify search results (single indicator)
    if radio_edit_found:
        medium_confidence_count += 1
        log_debug(f"[CONFIDENCE] +1 medium: Radio Edit found")
    
    log_debug(f"[CONFIDENCE] Source counts: high={high_confidence_count}, medium={medium_confidence_count}, max_z={max_z:.2f}")
    log_debug(f"[CONFIDENCE] Metadata flags: discogs={discogs_confirmed}, mb={musicbrainz_confirmed}, video={discogs_video_confirmed}, lastfm={lastfm_single_confirmed}, radio_edit={radio_edit_found}")
    log_debug(f"[CONFIDENCE] MB extended flags: mb_video={musicbrainz_video_confirmed}, mb_compilation={musicbrainz_compilation_confirmed}")
    
    # DETERMINE FINAL STATUS BASED ON Z-SCORE:
    
    # Z-score >= 1: Requires 2 medium sources OR 1 high source — SAME rule as z 0-1.
    # Z-score is a popularity metric, NOT a confidence indicator — it must not
    # substitute for metadata evidence.  A high z-score with only 1 medium source
    # (e.g. just MusicBrainz or just Last.fm) is insufficient — 2 sources required.
    if max_z >= 1.0:
        if high_confidence_count >= 1 or medium_confidence_count >= 2:
            log_debug(f"[CONFIDENCE] → RETURNING 'high' (z>=1.0: has high={high_confidence_count}, medium={medium_confidence_count})")
            return 'high'
        # 1 medium source with z >= 1 is NOT enough — fall through to 'none'
        log_debug(f"[CONFIDENCE] → RETURNING 'none' (z>=1.0: only {medium_confidence_count} medium sources, 2 required)")
        return 'none'

    # Z-score 0-1 (strictly greater than 0): Need 1 high OR 2 medium sources
    elif 0.0 < max_z < 1.0:
        if high_confidence_count >= 1:
            log_debug(f"[CONFIDENCE] → RETURNING 'high' (z 0-1: has {high_confidence_count} high source)")
            return 'high'
        elif medium_confidence_count >= 2:
            log_debug(f"[CONFIDENCE] → RETURNING 'medium' (z 0-1: has {medium_confidence_count} medium sources)")
            return 'medium'

    # z <= 0: normally reject, BUT remastered-only variants get a chance via the
    # same "0-1" threshold (1 high OR 2 medium sources) because their low popularity
    # is caused by listeners preferring the original release, not by the track not being
    # a single.
    elif max_z <= 0.0:
        if is_remastered_only:
            if high_confidence_count >= 1:
                log_debug(f"[CONFIDENCE] → RETURNING 'high' (remastered-only z<=0 bypass: has {high_confidence_count} high source)")
                return 'high'
            elif medium_confidence_count >= 2:
                log_debug(f"[CONFIDENCE] → RETURNING 'medium' (remastered-only z<=0 bypass: has {medium_confidence_count} medium sources)")
                return 'medium'
        log_debug(
            f"[CONFIDENCE] → RETURNING 'none' (z<=0 gate: max_z={max_z:.2f}, "
            f"requires z>0 before confidence sources can qualify)"
        )
        return 'none'
    
    # No confidence achieved
    log_debug(f"[CONFIDENCE] → RETURNING 'none' (insufficient sources for z-score threshold: max_z={max_z:.2f}, high={high_confidence_count}, medium={medium_confidence_count})")
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
    duration: Optional[float] = None,
    artist_mbid: Optional[str] = None,
    album: str = ""
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
            if discogs_client.is_single(title, artist, album_context={'duration': duration, 'album_name': album}):
                return True
        except Exception:
            pass  # Fail gracefully
    
    # Check MusicBrainz
    if musicbrainz_client and hasattr(musicbrainz_client, 'enabled') and musicbrainz_client.enabled:
        try:
            if musicbrainz_client.is_single(title, artist, artist_mbid=artist_mbid):
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
    duration: Optional[float] = None,
    artist_mbid: Optional[str] = None,
    album: str = ""
) -> bool:
    """
    Check if there's metadata for the EXACT live version of this track.
    
    For live tracks, we need metadata that confirms the live version specifically,
    not just the studio version.
    """
    # For now, use same logic as has_explicit_metadata
    # In a more sophisticated implementation, we would check if the metadata
    # specifically mentions "live" in the release title
    return check_has_explicit_metadata(title, spotify_results, discogs_client, musicbrainz_client, artist, duration, artist_mbid, album)


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
    lastfm_client=None,
    verbose: bool = False,
    album_type: Optional[str] = None,
    album_is_underperforming: bool = False,
    artist_median_popularity: float = 0.0,
    mb_cached_singles: Optional[set] = None,
) -> Dict:
    """
    Enhanced single detection implementing the exact algorithm from problem statement.
    
    Pipeline:
    1. PREPROCESSING - Calculate album/artist stats, exclude trailing parenthesis tracks
    2. ARTIST-LEVEL SANITY FILTER - Skip if popularity < artist_mean AND no explicit metadata
    3. HIGH CONFIDENCE DETECTION - Popularity standout OR Discogs
    4. MEDIUM CONFIDENCE DETECTION - Z-score+metadata, Spotify, MusicBrainz, Discogs video, Last.fm album track count, version count
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
        lastfm_client: Last.fm API client (optional)
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
    
    # VALIDATION: Check input data integrity
    is_valid, validation_error = validate_track_data(track_id, title, artist, album, popularity)
    if not is_valid:
        log_debug(f"[VALIDATION] Track validation failed: {validation_error}")
        log_info(f"   ⚠ Skipping {title}: {validation_error}")
        return result
    
    # Load source confidence settings from config
    source_confidence_settings = get_source_confidence_settings()
    log_debug(f"[CONFIG] Source confidence settings: {source_confidence_settings}")
    
    # Log entry for this track detection
    log_debug(f"[DETECT] Starting single detection for: '{title}' by {artist} (album: {album}, pop: {popularity:.1f})")

    # Get ARTIST-level statistics (across entire catalogue for comparison)
    # This identifies true standouts in the artist's body of work
    # Use cached version to improve performance (O(n) instead of O(n×m×k))
    artist_mean, artist_stddev, artist_track_count = get_cached_artist_stats(conn, artist)
    log_debug(f"[ARTIST_STATS] Mean: {artist_mean:.1f}, StdDev: {artist_stddev:.1f}, Tracks: {artist_track_count}")

    # VALIDATION: Check artist stats integrity
    is_valid, validation_error = validate_artist_stats(artist, artist_mean, artist_stddev, artist_track_count)
    if not is_valid:
        log_debug(f"[VALIDATION] Artist stats validation failed: {validation_error}")
        # Continue with 0 values rather than failing
        artist_mean, artist_stddev, artist_track_count = 0.0, 0.0, 0
    
    # Get album statistics for album-level filtering (two-stage approach)
    album_mean, album_stddev, album_median, album_track_count = get_cached_album_stats(conn, artist, album)
    log_debug(f"[ALBUM_STATS] Mean: {album_mean:.1f}, Median: {album_median:.1f}, StdDev: {album_stddev:.1f}, Tracks: {album_track_count}")
    
    # VALIDATION: Check album stats integrity
    is_valid, validation_error = validate_artist_stats(album, album_mean, album_stddev, album_track_count)
    if not is_valid:
        log_debug(f"[VALIDATION] Album stats validation failed: {validation_error}")
        # Continue with 0 values rather than failing
        album_mean, album_stddev, album_median, album_track_count = 0.0, 0.0, 0.0, 0

    # Create cursor for queries
    placeholder = "%s"
    cursor = conn.cursor()
    
    # Get album popularities list for pre-filter
    cursor.execute(f"""
        SELECT popularity_score
        FROM tracks
        WHERE COALESCE(NULLIF(album_artist, ''), artist) = {placeholder} AND album = {placeholder} AND popularity_score > 0
        ORDER BY popularity_score DESC
    """, (artist, album))
    album_pops_rows = cursor.fetchall()
    album_popularities = [row['popularity_score'] for row in album_pops_rows] if album_pops_rows else []

    # Get all artist popularities for context
    cursor = conn.cursor()
    cursor.execute(f"""
        SELECT id, title, popularity_score, album
        FROM tracks
        WHERE artist = {placeholder} AND popularity_score > 0
        ORDER BY popularity_score DESC
    """, (artist,))
    artist_rows = cursor.fetchall()
    artist_popularities = [row['popularity_score'] for row in artist_rows]

    # --- Prefer canonical (non-alternate) version for single detection ---
    # If both canonical and alternate (e.g., acoustic) versions exist for the same base title,
    # only allow the canonical version to be marked as the single.
    import re
    def base_title(t):
        # Remove trailing parenthesis and common alternate markers
        return re.sub(r'\s*\([^)]*\)$', '', t).strip().lower()

    current_base = base_title(title)
    # Find all tracks with the same base title in artist's catalogue
    same_base_tracks = [row for row in artist_rows if base_title(row['title']) == current_base]
    if len(same_base_tracks) > 1:
        # Prefer canonical (non-alternate) version
        def is_alternate(t):
            alt_keywords = ["acoustic", "live", "remix", "edit", "mix", "orchestral", "unplugged", "demo", "instrumental", "karaoke"]
            t_low = t.lower()
            return any(alt in t_low for alt in alt_keywords)
        # If a canonical version exists, only allow it to be marked as single
        canonical_tracks = [row for row in same_base_tracks if not is_alternate(row['title'])]
        if canonical_tracks:
            # If this is an alternate version, do not mark as single
            if is_alternate(title):
                log_debug(f"[ALT FILTER] Skipping alternate version '{title}' in favor of canonical version for single detection.")
                return result

    # Detect if this is a compilation album
    is_compilation = is_compilation_album(album_type, album, album_track_count)

    if is_compilation and verbose:
        log_debug(f"[DEBUG] Compilation detected — checking all tracks for singles.")

    # Detect if this is a special edition album
    is_special_edition = is_special_edition_album(album)

    if is_special_edition and verbose:
        log_debug(f"[DEBUG] Special edition album detected: {album}")

    # Strip cover attribution from title for external API searches.
    # Tracks with "(bandname Cover)" or "[bandname Cover]" in the title should
    # search without the cover suffix so the original recording's single status
    # is found correctly.  The original `title` is kept for logging and DB queries.
    lookup_title = strip_cover_attribution(title)
    if lookup_title != title:
        log_debug(f"[COVER] Cover attribution stripped for API lookups: '{title}' -> '{lookup_title}'")

    # Check if album type is marked as 'single' and track title matches album name (high confidence source)
    album_type_is_single = False
    if album_type and album_type.lower() == 'single':
        # Only treat as high-confidence source if track title matches the album name
        from single_detector import normalize_title_strict
        if normalize_title_strict(title) == normalize_title_strict(album):
            album_type_is_single = True
            log_debug(f"[ALBUM_TYPE] Album marked as single type with matching title — will be treated as high-confidence source")
            log_info(f"   Album type is 'single' and title matches — treating as high-confidence indicator for {title}")
        else:
            log_debug(f"[ALBUM_TYPE] Album marked as single type but title does not match ('{title}' != '{album}') — ignoring")

    # Count Spotify versions
    spotify_version_count = count_spotify_versions(spotify_results or [], lookup_title, duration, isrc)
    result['spotify_version_count'] = spotify_version_count
    
    # STAGE 1: Initialize Discogs check (will be gated by z-score in STAGE 2+)
    # Discogs check is now moved after z-score calculation to only check tracks with positive z-score
    # This saves API quota and improves performance without missing confirmed singles
    # (compilations/greatest hits with low z-scores are still checked)
    discogs_confirmed = False
    
    # STAGE 2: Z-Score Filter (Efficiency Gate for remaining metadata checks)
    # Calculate z-scores using median+MAD to determine if track shows standout characteristics
    # Skip remaining expensive API calls if both album and artist z-scores are low AND Discogs didn't confirm
    
    # Album z-score using median + MAD
    if album_median > 0 and album_popularities:
        # Calculate MAD for album
        from statistics import median as stat_median
        album_abs_devs = [abs(s - album_median) for s in album_popularities]
        album_mad = stat_median(album_abs_devs) if album_abs_devs else 0
        album_mad_scaled = album_mad * 1.4826  # Scale to be comparable to stddev
        album_spread = max(album_mad_scaled, 10.0)  # MIN_SPREAD floor
        album_z = (popularity - album_median) / album_spread if album_spread > 0 else 0
        log_debug(f"[ZSCORE] Album: median={album_median:.1f}, MAD={album_mad:.1f}, MAD_scaled={album_mad_scaled:.1f}, pop={popularity:.1f}, z={album_z:.2f}")
    else:
        album_z = 0.0
    
    # Artist z-score using median + MAD
    try:
        if artist_popularities:
            from statistics import median as stat_median
            artist_median = stat_median(artist_popularities)
            artist_abs_devs = [abs(s - artist_median) for s in artist_popularities]
            artist_mad = stat_median(artist_abs_devs) if artist_abs_devs else 0
            artist_mad_scaled = artist_mad * 1.4826  # Scale to be comparable to stddev
            artist_spread = max(artist_mad_scaled, 10.0)  # MIN_SPREAD floor
            artist_z = (popularity - artist_median) / artist_spread if artist_spread > 0 else 0
            log_debug(f"[ZSCORE] Artist: median={artist_median:.1f}, MAD={artist_mad:.1f}, MAD_scaled={artist_mad_scaled:.1f}, pop={popularity:.1f}, z={artist_z:.2f}")
        else:
            artist_z = 0.0
    except Exception as e:
        log_debug(f"[ZSCORE] Could not calculate artist z-score: {e}")
        artist_z = 0.0
    
    # For compilation/greatest-hits albums, album-level distributions are often
    # heterogeneous and less meaningful. Reuse artist-level z-score as the
    # primary baseline so compilation tracks are evaluated against the artist's
    # full catalogue rather than the mixed album cohort.
    if is_compilation:
        album_z = artist_z
        log_debug(f"[ZSCORE] Compilation baseline override: using artist-wide z-score for album_z (album_z={album_z:.2f})")

    result['z_score'] = album_z
    result['album_z_score'] = album_z
    result['artist_z_score'] = artist_z
    
    # Z-Score Gate: Skip single detection if artist_z < 0
    # LOGIC:
    # - z < 0: Skip detection (always), EXCEPT for compilations (every track must be checked)
    #          ALSO EXCEPT for remastered-only variants (see note below)
    # - z 0-1: Require 2 medium OR 1 high confidence sources
    # - z >= 1: Same requirement as z 0-1 — 2 medium OR 1 high confidence sources
    #           (z-score does NOT lower the evidence bar)
    # - z > 2 with NO sources: Mark as "Popular" with 5★ rating (not as single)
    #
    # REMASTER BYPASS: Remastered tracks inherently have lower popularity than the original
    # studio version because listeners stream the original.  Their negative z-score therefore
    # does NOT indicate they are not singles — it just reflects the streaming preference for
    # the original release.  We bypass the z-score gate for remastered-only variants so that
    # e.g. "Higher (remastered 2024)" can still be detected as a single via API sources.
    is_remastered_only = is_remastered_only_variant(title)
    if artist_z < 0.0 and not is_compilation and not is_remastered_only:
        log_debug(f"[ZSCORE] ✗ Artist z-score below 0 (artist_z={artist_z:.2f}, album_z={album_z:.2f})")
        log_info(f"   ⓘ Skipping single detection for {title}: artist z-score below 0")
        if verbose:
            log_debug(f"Z-score filter: Skipping {title} (artist_z < 0)")
        return result
    if artist_z < 0.0 and is_remastered_only:
        log_debug(f"[ZSCORE] Bypassing z-score gate for remastered-only variant '{title}' (artist_z={artist_z:.2f})")

    
    log_debug(f"[ZSCORE] ✓ Track qualifies for metadata checks (album_z={album_z:.2f}, artist_z={artist_z:.2f})")
    if verbose:
        log_debug(f"Z-score filter: {title} qualifies for detailed single detection (z-threshold varies by score)")
    
    # STAGE 2b: Album Type Check (HIGH CONFIDENCE)
    # If the album is marked as type='single' and the track title matches the album name,
    # this is definitive evidence of a single release.
    if album_type_is_single:
        result['single_sources'].append('album_type')
        result['single_sources_used'].append('album_type')
        log_debug(f"[ALBUM_TYPE] ✓ CONFIRMED as single via album type")
        log_info(f"   ✓ Album type confirms single: {title}")
        
        # Check if HIGH confidence reached
        if check_high_confidence_dynamic(
                True, False, False, False, False,
                source_confidence_settings
        ):
            # HIGH confidence achieved from album_type
            log_debug(f"[DETECT] HIGH confidence from album_type source")
            result['single_status'] = 'high'
            result['single_confidence'] = 'high'
            result['is_single'] = True
            result['single_confidence_score'] = 1.0
            return result

    # STAGE 3: MusicBrainz (Secondary Source - checked before Spotify per new ordering)
        # STAGE 3: MusicBrainz (Secondary Source)
    # STAGE 2a: Discogs Check (NOW GATED BY Z-SCORE)
    # Only check Discogs if track shows standout characteristics OR is on a compilation
    # OR is a remastered-only variant (whose low popularity reflects the original's dominance,
    # not the absence of single status).
    # This conserves API quota while still catching confirmed singles.
    discogs_confirmed = False
    if artist_z > 0.0 or is_compilation or is_remastered_only:
        if discogs_client and hasattr(discogs_client, 'enabled') and discogs_client.enabled:
            try:
                log_debug(f"[DISCOGS] Querying Discogs API for single: {title} by {artist} (z-score gate passed: artist_z={artist_z:.2f})")
                log_info(f"   Checking Discogs for single: {title}")
                log_debug(f"   Discogs API: Searching for single '{lookup_title}' by '{artist}'")
                
                # Use existing is_single method
                discogs_confirmed = discogs_client.is_single(lookup_title, artist, album_context={
                    'duration': duration,
                    'is_special_edition': is_special_edition,
                    'album_name': album
                })
                if discogs_confirmed:
                    result['single_sources'].append('discogs')
                    result['single_sources_used'].append('discogs')
                    log_debug(f"[DISCOGS] ✓ CONFIRMED as single")
                    log_info(f"   ✓ Discogs confirms single: {title}")
                    log_debug(f"   Discogs result: Single confirmed for '{title}'")
                    
                    # Check if HIGH confidence reached after this confirmation
                    if check_high_confidence_dynamic(
                            discogs_confirmed, False, False, False, False,
                        source_confidence_settings
                    ):
                        # HIGH confidence achieved - can skip remaining sources
                        log_debug(f"[DETECT] Stopping early with HIGH confidence from Discogs")
                        result['single_status'] = 'high'
                        result['single_confidence'] = 'high'
                        result['is_single'] = True
                        result['single_confidence_score'] = 1.0
                        return result
                else:
                    log_debug(f"[DISCOGS] ✗ NOT confirmed as single by Discogs")
                    log_info(f"   ⓘ Discogs does not confirm single: {title}")
                    log_debug(f"   Discogs result: No single found for '{title}'")
            except Exception as e:
                log_debug(f"[DISCOGS] ERROR during lookup: {type(e).__name__}: {str(e)}")
                log_info(f"   ⚠ Discogs single check failed for {title}: {e}")
                log_debug(f"   Discogs API error: {type(e).__name__}: {str(e)}")
        else:
            if verbose:
                if not discogs_client:
                    log_debug(f"[DISCOGS] Client not available (module import failed)")
                    log_info(f"   ⓘ Discogs client not available")
                elif not getattr(discogs_client, 'enabled', True):
                    log_debug(f"[DISCOGS] Client disabled in configuration")
                    log_info(f"   ⓘ Discogs client is disabled")
    else:
        log_debug(f"[DISCOGS] SKIPPED - Z-score too low (artist_z={artist_z:.2f}) and album is not a compilation")
        log_info(f"   ⓘ Skipping Discogs check for {title}: z-score too low")
    
    # Declare all source variables first
    musicbrainz_confirmed = False
    musicbrainz_compilation_confirmed = False
    radio_edit_found = False
    discogs_video_confirmed = False
    lastfm_single_confirmed = False
    artist_mbid = None  # Initialize for use in video/compilation checks

    # Track canonical single/radio edit markers from title and Spotify result names.
    # This source is configurable through source_radio_edit_confidence.
    if has_single_or_radio_edit_marker(title):
        radio_edit_found = True
    elif spotify_results:
        for spotify_result in spotify_results:
            candidate_name = spotify_result.get('name', '') if isinstance(spotify_result, dict) else ''
            if has_single_or_radio_edit_marker(candidate_name):
                radio_edit_found = True
                break

    if radio_edit_found:
        result['single_sources'].append('radio_edit')
        result['single_sources_used'].append('radio_edit')
        log_debug(f"[RADIO_EDIT] ✓ Found single/radio-edit marker for '{title}'")

        if check_high_confidence_dynamic(
            discogs_confirmed,
            False,
            False,
            False,
            True,
            source_confidence_settings,
        ):
            log_debug(f"[DETECT] Stopping early with HIGH confidence from single/radio-edit marker")
            result['single_status'] = 'high'
            result['single_confidence'] = 'high'
            result['is_single'] = True
            result['single_confidence_score'] = 1.0
            return result
    
    if musicbrainz_client and hasattr(musicbrainz_client, 'enabled') and musicbrainz_client.enabled:
        # OPTIMIZATION: Use z-scores from STAGE 2 to decide if we need expensive API calls
        # Reuse album_z and artist_z already calculated (saves redundant computation)
        early_z_check_needed = not discogs_confirmed
        
        if early_z_check_needed:
            # Only skip MusicBrainz if we already have HIGH confidence (z-scores indicate album/artist standout)
            # Medium confidence is not sufficient - allow MusicBrainz to run and add source confirmation
            dynamic_threshold = get_dynamic_z_threshold(artist_track_count, release_year=None, is_compilation=is_compilation)
            if album_z >= dynamic_threshold or artist_z >= dynamic_threshold:
                log_debug(f"[MUSICBRAINZ] SKIPPED - Already have HIGH confidence from z-score (album_z={album_z:.2f}, artist_z={artist_z:.2f})")
                musicbrainz_confirmed = False
            else:
                # Need to call MusicBrainz
                try:
                    # Log MusicBrainz checks to info log only (not unified)
                    log_debug(f"[MUSICBRAINZ] Querying MusicBrainz API for single: {title} by {artist}")
                    log_info(f"   Checking MusicBrainz for single: {title}")
                    log_debug(f"   MusicBrainz API: Searching for single '{title}' by '{artist}'")
                    
                    # Get artist MBID if available (for more accurate lookup)
                    # Check both beets_artist_mbid (from Beets import) and musicbrainz_artist_id (from scan)
                    artist_mbid = None
                    try:
                        cursor = conn.cursor()
                        cursor.execute(
                            f"SELECT COALESCE(musicbrainz_artist_id, lastfm_artist_mbid) as artist_mbid FROM tracks WHERE artist = {placeholder} AND (musicbrainz_artist_id IS NOT NULL OR lastfm_artist_mbid IS NOT NULL) LIMIT 1", 
                            (artist,)
                        )
                        row = cursor.fetchone()
                        if row:
                            artist_mbid = row['artist_mbid']
                            log_debug(f"[MUSICBRAINZ] Found artist MBID for '{artist}': {artist_mbid}")
                    except Exception as e:
                        log_debug(f"[MUSICBRAINZ] Could not fetch artist MBID: {e}")
                    
                    # Use pre-loaded MB singles cache when available to avoid an API call.
                    # mb_cached_singles contains lowercase-normalised titles from missing_releases
                    # (singles NOT yet in the user's library).  A title match means MB confirms.
                    if mb_cached_singles is not None:
                        _mb_lookup_norm = lookup_title.lower().strip()
                        if _mb_lookup_norm in mb_cached_singles:
                            musicbrainz_confirmed = True
                            log_debug(f"[MUSICBRAINZ] ✓ CONFIRMED from missing_releases cache (no API call): {title}")
                            log_info(f"   ✓ MusicBrainz confirms single (cached): {title}")
                        else:
                            # Title absent from missing_releases: may already be in library or not exist.
                            # Fall through to the API for a definitive answer.
                            musicbrainz_confirmed = musicbrainz_client.is_single(lookup_title, artist, artist_mbid=artist_mbid)
                    else:
                        # Use is_single method with artist MBID (preferred) and fallback to name-based search
                        musicbrainz_confirmed = musicbrainz_client.is_single(lookup_title, artist, artist_mbid=artist_mbid)
                    if musicbrainz_confirmed:
                        result['single_sources'].append('musicbrainz')
                        result['single_sources_used'].append('musicbrainz')
                        log_debug(f"[MUSICBRAINZ] ✓ CONFIRMED as single")
                        log_info(f"   ✓ MusicBrainz confirms single: {title}")
                        log_debug(f"   MusicBrainz result: Single confirmed for '{title}'")
                        
                        # Check if HIGH confidence reached after this confirmation
                        if check_high_confidence_dynamic(
                                discogs_confirmed, musicbrainz_confirmed, 
                            False, False, False,
                            source_confidence_settings
                        ):
                            # HIGH confidence achieved - can skip remaining sources
                            log_debug(f"[DETECT] Stopping early with HIGH confidence after MusicBrainz confirmation")
                            result['single_status'] = 'high'
                            result['single_confidence'] = 'high'
                            result['is_single'] = True
                            result['single_confidence_score'] = 1.0
                            album_z = calculate_z_score_strict(popularity, album_mean, album_stddev)
                            result['z_score'] = album_z
                            result['album_z_score'] = album_z
                            artist_mean, artist_stddev, _ = get_cached_artist_stats(conn, artist)
                            artist_z = calculate_z_score_strict(popularity, artist_mean, artist_stddev)
                            result['artist_z_score'] = artist_z
                            return result
                    else:
                        log_debug(f"[MUSICBRAINZ] ✗ NOT confirmed as single by MusicBrainz")
                        log_info(f"   ⓘ MusicBrainz does not confirm single: {title}")
                        log_debug(f"   MusicBrainz result: No single found for '{title}'")
                except Exception as e:
                    # Log SSL and connection errors more gracefully
                    error_type = type(e).__name__
                    if 'SSL' in error_type or 'ssl' in str(e).lower():
                        log_debug(f"[MUSICBRAINZ] SSL ERROR: {error_type}")
                        log_info(f"   ⚠ MusicBrainz SSL connection error for {title}: {error_type}")
                        log_debug(f"   MusicBrainz API SSL error: {error_type}: {str(e)}")
                    elif 'timeout' in str(e).lower() or 'Timeout' in error_type:
                        log_debug(f"[MUSICBRAINZ] TIMEOUT ERROR: {error_type}")
                        log_info(f"   ⏱ MusicBrainz check timed out for {title}: {error_type}")
                        log_debug(f"   MusicBrainz API timeout: {error_type}: {str(e)}")
                    else:
                        log_debug(f"[MUSICBRAINZ] ERROR during lookup: {error_type}: {str(e)}")
                        log_info(f"   ⚠ MusicBrainz single check failed for {title}: {e}")
                        log_debug(f"   MusicBrainz API error: {type(e).__name__}: {str(e)}")
                    musicbrainz_confirmed = False
        else:
            musicbrainz_confirmed = False
    else:
        # Only log client availability messages in verbose mode to reduce log noise
        if verbose:
            if not musicbrainz_client:
                log_debug(f"[MUSICBRAINZ] Client not available (module import failed)")
                log_info(f"   ⓘ MusicBrainz client not available")
                log_debug(f"   MusicBrainz: Client not available (module import failed)")
            elif not getattr(musicbrainz_client, 'enabled', True):
                log_debug(f"[MUSICBRAINZ] Client disabled in configuration")
                log_info(f"   ⓘ MusicBrainz client is disabled")
                log_debug(f"   MusicBrainz: Client is disabled in configuration")
        musicbrainz_confirmed = False
    
    # STAGE 3B intentionally skipped: MusicBrainz video-relationship lookup removed
    # to reduce single-detection API latency.
    
    # STAGE 3C: MusicBrainz Various Artists Compilation Check (MEDIUM CONFIDENCE)
    # Check if the track appears on multiple Various Artists compilations
    if musicbrainz_client and hasattr(musicbrainz_client, 'enabled') and musicbrainz_client.enabled:
        if hasattr(musicbrainz_client, 'appears_on_various_artists'):
            try:
                log_debug(f"[MUSICBRAINZ] Checking for Various Artists appearances: {title} by {artist}")
                log_info(f"   Checking MusicBrainz for compilation appearances: {title}")
                
                appears_on_va = musicbrainz_client.appears_on_various_artists(lookup_title, artist)
                if appears_on_va:
                    result['single_sources'].append('musicbrainz_compilation')
                    result['single_sources_used'].append('musicbrainz_compilation')
                    musicbrainz_compilation_confirmed = True
                    log_debug(f"[MUSICBRAINZ] ✓ CONFIRMED - Track appears on multiple VA compilations (single indicator)")
                    log_info(f"   ✓ MusicBrainz compilation appearances found: {title}")
                    
                    # Check if HIGH confidence reached after this confirmation
                    if check_high_confidence_dynamic(
                        discogs_confirmed, musicbrainz_confirmed,
                        discogs_video_confirmed, lastfm_single_confirmed, radio_edit_found,
                        source_confidence_settings,
                        musicbrainz_compilation_confirmed
                    ):
                        log_debug(f"[DETECT] Stopping early with HIGH confidence after MusicBrainz compilation confirmation")
                        result['single_status'] = 'high'
                        result['single_confidence'] = 'high'
                        result['is_single'] = True
                        result['single_confidence_score'] = 1.0
                        album_z = calculate_z_score_strict(popularity, album_mean, album_stddev)
                        result['z_score'] = album_z
                        result['album_z_score'] = album_z
                        artist_mean, artist_stddev, _ = get_cached_artist_stats(conn, artist)
                        artist_z = calculate_z_score_strict(popularity, artist_mean, artist_stddev)
                        result['artist_z_score'] = artist_z
                        return result
                else:
                    log_debug(f"[MUSICBRAINZ] ✗ No compilation appearances found")
            except Exception as e:
                log_debug(f"[MUSICBRAINZ] ERROR during compilation check: {type(e).__name__}: {str(e)}")
                log_info(f"   ⚠ MusicBrainz compilation check failed for {title}: {e}")
    
    # STAGE 4: Last.fm Single Check (MEDIUM CONFIDENCE)
    # Check if the track exists as a single/album on Last.fm (by track title)
    # Also check album track count for traditional single detection
    
    # Check if we should skip Last.fm based on current confidence
    if not check_high_confidence_dynamic(
        discogs_confirmed, musicbrainz_confirmed, 
        False, False, False,
        source_confidence_settings,
        musicbrainz_compilation_confirmed
    ):
        if lastfm_client:
            try:
                # First, check if track exists as a single on Last.fm (by track title)
                log_debug(f"[LASTFM] Checking if track exists as single: {title} by {artist}")
                track_is_single = lastfm_client.check_track_as_single(artist, lookup_title)
                
                if track_is_single:
                    result['single_sources'].append('lastfm')
                    result['single_sources_used'].append('lastfm')
                    lastfm_single_confirmed = True
                    log_debug(f"[LASTFM] ✓ CONFIRMED - Last.FM (Single) - Track '{title}' exists as single/album on Last.fm")
                    
                    # Check if HIGH confidence reached after this confirmation
                    if check_high_confidence_dynamic(
                        discogs_confirmed, musicbrainz_confirmed, 
                        False, lastfm_single_confirmed, False,
                        source_confidence_settings,
                        musicbrainz_compilation_confirmed
                    ):
                        # HIGH confidence achieved
                        log_debug(f"[DETECT] HIGH confidence achieved from Last.fm confirmation")
                        result['single_status'] = 'high'
                        result['single_confidence'] = 'high'
                        result['is_single'] = True
                        result['single_confidence_score'] = 1.0
                        album_z = calculate_z_score_strict(popularity, album_mean, album_stddev)
                        result['z_score'] = album_z
                        result['album_z_score'] = album_z
                        artist_mean, artist_stddev, _ = get_cached_artist_stats(conn, artist)
                        artist_z = calculate_z_score_strict(popularity, artist_mean, artist_stddev)
                        result['artist_z_score'] = artist_z
                        return result
                else:
                    # Fallback to album track count check
                    log_debug(f"[LASTFM] Track not found as single, checking album track count: {album} by {artist}")
                    album_track_count_lastfm = lastfm_client.get_album_track_count(artist, album)
                    
                    # Check if the album has a title track (song name matching release name)
                    has_title_track = False
                    if 4 <= album_track_count_lastfm <= 6:
                        # Only check for title track if we're in the 4-6 range
                        has_title_track = lastfm_client.has_title_track(artist, album)
                        if has_title_track:
                            log_debug(f"[LASTFM] Album has title track (song name matches release name)")
                    
                    # Singles on Last.fm typically have 1-3 tracks
                    # Or up to 6 tracks if the song name matches the release name (singles released as EPs)
                    if 1 <= album_track_count_lastfm <= 3:
                        result['single_sources'].append('lastfm')
                        result['single_sources_used'].append('lastfm')
                        lastfm_single_confirmed = True
                        log_debug(f"[LASTFM] ✓ CONFIRMED - Last.FM (Album Type) - Album has {album_track_count_lastfm} track(s) (single indicator)")
                    elif 4 <= album_track_count_lastfm <= 6 and has_title_track:
                        result['single_sources'].append('lastfm')
                        result['single_sources_used'].append('lastfm')
                        lastfm_single_confirmed = True
                        log_debug(f"[LASTFM] ✓ CONFIRMED - Last.FM (Album Type) - Album has {album_track_count_lastfm} track(s) with title track (single EP indicator)")
                    else:
                        log_debug(f"[LASTFM] Track count: {album_track_count_lastfm} (not in single range)")
                        lastfm_single_confirmed = False
                    
                    # Check if HIGH confidence reached after album count confirmation
                    if lastfm_single_confirmed and check_high_confidence_dynamic(
                        discogs_confirmed, musicbrainz_confirmed, 
                        False, lastfm_single_confirmed, False,
                        source_confidence_settings,
                        musicbrainz_compilation_confirmed
                    ):
                        # HIGH confidence achieved
                        log_debug(f"[DETECT] HIGH confidence achieved from Last.fm album track count confirmation")
                        result['single_status'] = 'high'
                        result['single_confidence'] = 'high'
                        result['is_single'] = True
                        result['single_confidence_score'] = 1.0
                        album_z = calculate_z_score_strict(popularity, album_mean, album_stddev)
                        result['z_score'] = album_z
                        result['album_z_score'] = album_z
                        artist_mean, artist_stddev, _ = get_cached_artist_stats(conn, artist)
                        artist_z = calculate_z_score_strict(popularity, artist_mean, artist_stddev)
                        result['artist_z_score'] = artist_z
                        return result
            except Exception as e:
                log_debug(f"[LASTFM] Error checking single: {e}")
                lastfm_single_confirmed = False
        else:
            log_debug(f"[LASTFM] Client not available")
    else:
        log_debug(f"[LASTFM] SKIPPED - HIGH confidence already achieved")
        lastfm_single_confirmed = False
    
    # STAGE 4.5: Discogs Music Video Check (MEDIUM CONFIDENCE)
    
    # Check if we should skip Video checks based on current confidence
    current_confidence_high = check_high_confidence_dynamic(
        discogs_confirmed, musicbrainz_confirmed, 
        False, lastfm_single_confirmed, False,
        source_confidence_settings,
        musicbrainz_compilation_confirmed
    )
    
    if not current_confidence_high:
        if discogs_client and hasattr(discogs_client, 'enabled') and discogs_client.enabled:
            if hasattr(discogs_client, 'has_official_video'):
                try:
                    # Log Discogs video checks to info log only (not unified)
                    log_debug(f"[DISCOGS_VIDEO] Querying Discogs for music video: {title} by {artist}")
                    log_info(f"   Checking Discogs for music video: {title}")
                    log_debug(f"   Discogs API: Searching for music video '{lookup_title}' by '{artist}'")
                    
                    # Check for official music video
                    discogs_video_confirmed = discogs_client.has_official_video(lookup_title, artist)
                    if discogs_video_confirmed:
                        result['single_sources'].append('discogs_video')
                        result['single_sources_used'].append('discogs_video')
                        log_debug(f"[DISCOGS_VIDEO] ✓ CONFIRMED - Official video found")
                        log_info(f"   ✓ Discogs confirms music video: {title}")
                        log_debug(f"   Discogs result: Music video confirmed for '{title}'")
                        
                        # Check if HIGH confidence reached after this confirmation
                        if check_high_confidence_dynamic(
                            discogs_confirmed, musicbrainz_confirmed, 
                            discogs_video_confirmed, lastfm_single_confirmed, False,
                            source_confidence_settings,
                            musicbrainz_compilation_confirmed
                        ):
                            # HIGH confidence achieved - can skip Spotify
                            log_debug(f"[DETECT] Stopping early with HIGH confidence after Discogs Video confirmation")
                            result['single_status'] = 'high'
                            result['single_confidence'] = 'high'
                            result['is_single'] = True
                            result['single_confidence_score'] = 1.0
                            album_z = calculate_z_score_strict(popularity, album_mean, album_stddev)
                            result['z_score'] = album_z
                            result['album_z_score'] = album_z
                            artist_mean, artist_stddev, _ = get_cached_artist_stats(conn, artist)
                            artist_z = calculate_z_score_strict(popularity, artist_mean, artist_stddev)
                            result['artist_z_score'] = artist_z
                            return result
                    else:
                        log_debug(f"[DISCOGS_VIDEO] ✗ NOT confirmed - No official video found")
                        log_info(f"   ⓘ Discogs does not confirm music video: {title}")
                        log_debug(f"   Discogs result: No music video found for '{title}'")
                except Exception as e:
                    log_debug(f"[DISCOGS_VIDEO] ERROR during lookup: {type(e).__name__}: {str(e)}")
                    log_info(f"   ⚠ Discogs video check failed for {title}: {e}")
                    log_debug(f"   Discogs API error: {type(e).__name__}: {str(e)}")
                    discogs_video_confirmed = False
        elif verbose:
            log_debug(f"[DISCOGS_VIDEO] has_official_video method not available")
            log_info(f"   ⓘ Discogs video method not available")
            log_debug(f"   Discogs: has_official_video method not available")
    else:
        # Only log client availability messages in verbose mode to reduce log noise
        if verbose:
            if not discogs_client:
                log_debug(f"[DISCOGS_VIDEO] Client not available")
                log_info(f"   ⓘ Discogs video client not available")
                log_debug(f"   Discogs: Video client not available")
            elif not getattr(discogs_client, 'enabled', True):
                log_debug(f"[DISCOGS_VIDEO] Client disabled")
                log_info(f"   ⓘ Discogs client is disabled")
                log_debug(f"   Discogs: Client is disabled in configuration")

    # Preserve first-pass medium-source confirmations and disable the duplicated
    # second metadata pipeline below to avoid duplicate external calls/sources.
    primary_musicbrainz_confirmed = musicbrainz_confirmed
    primary_musicbrainz_compilation_confirmed = musicbrainz_compilation_confirmed
    primary_discogs_video_confirmed = discogs_video_confirmed
    primary_lastfm_single_confirmed = lastfm_single_confirmed
    musicbrainz_client = None
    discogs_client = None
    lastfm_client = None
    
    # STAGE 4: MusicBrainz (Tertiary Source)
    musicbrainz_confirmed = False
    artist_mbid = None  # Initialize for use in video/compilation checks
    if musicbrainz_client and hasattr(musicbrainz_client, 'enabled') and musicbrainz_client.enabled:
        # OPTIMIZATION: Calculate z-scores early to decide if we need expensive API calls
        # If we already have medium-confidence from z-score, skip MusicBrainz (saves ~1-2 seconds)
        early_z_check_needed = not discogs_confirmed
        
        if early_z_check_needed:
            # Quick z-score check to see if we can skip MusicBrainz
            temp_album_z = calculate_z_score_strict(popularity, album_mean, album_stddev)
            # Get artist stats for quick artist_z check
            temp_artist_mean, temp_artist_stddev, _ = get_cached_artist_stats(conn, artist)
            temp_artist_z = calculate_z_score_strict(popularity, temp_artist_mean, temp_artist_stddev)
            
            # Only skip MusicBrainz if we already have HIGH confidence (z-scores indicate album/artist standout)
            # Medium confidence is not sufficient - allow MusicBrainz to run and add source confirmation
            dynamic_threshold = get_dynamic_z_threshold(artist_track_count, release_year=None, is_compilation=is_compilation)
            if temp_album_z >= dynamic_threshold or temp_artist_z >= dynamic_threshold:
                log_debug(f"[MUSICBRAINZ] SKIPPED - Already have HIGH confidence from z-score (album_z={temp_album_z:.2f}, artist_z={temp_artist_z:.2f})")
                musicbrainz_confirmed = False
            else:
                # Need to call MusicBrainz
                try:
                    # Log MusicBrainz checks to info log only (not unified)
                    log_debug(f"[MUSICBRAINZ] Querying MusicBrainz API for single: {title} by {artist}")
                    log_info(f"   Checking MusicBrainz for single: {title}")
                    log_debug(f"   MusicBrainz API: Searching for single '{title}' by '{artist}'")
                    
                    # Get artist MBID if available (for more accurate lookup)
                    # Check both beets_artist_mbid (from Beets import) and musicbrainz_artist_id (from scan)
                    artist_mbid = None
                    try:
                        cursor = conn.cursor()
                        cursor.execute(
                            f"SELECT COALESCE(musicbrainz_artist_id, lastfm_artist_mbid) as artist_mbid FROM tracks WHERE artist = {placeholder} AND (musicbrainz_artist_id IS NOT NULL OR lastfm_artist_mbid IS NOT NULL) LIMIT 1", 
                            (artist,)
                        )
                        row = cursor.fetchone()
                        if row:
                            artist_mbid = row['artist_mbid']
                            log_debug(f"[MUSICBRAINZ] Found artist MBID for '{artist}': {artist_mbid}")
                    except Exception as e:
                        log_debug(f"[MUSICBRAINZ] Could not fetch artist MBID: {e}")
                    
                    # Use is_single method with artist MBID (preferred) and fallback to name-based search
                    musicbrainz_confirmed = musicbrainz_client.is_single(lookup_title, artist, artist_mbid=artist_mbid)
                    if musicbrainz_confirmed:
                        result['single_sources'].append('musicbrainz')
                        result['single_sources_used'].append('musicbrainz')
                        log_debug(f"[MUSICBRAINZ] ✓ CONFIRMED as single")
                        log_info(f"   ✓ MusicBrainz confirms single: {title}")
                        log_debug(f"   MusicBrainz result: Single confirmed for '{title}'")
                        
                        # Check if HIGH confidence reached after this confirmation
                        if check_high_confidence_dynamic(
                            discogs_confirmed, musicbrainz_confirmed, 
                            False, False, radio_edit_found,
                            source_confidence_settings
                        ):
                            # HIGH confidence achieved - can skip remaining sources
                            log_debug(f"[DETECT] Stopping early with HIGH confidence after MusicBrainz confirmation")
                            result['single_status'] = 'high'
                            result['single_confidence'] = 'high'
                            result['is_single'] = True
                            result['single_confidence_score'] = 1.0
                            album_z = calculate_z_score_strict(popularity, album_mean, album_stddev)
                            result['z_score'] = album_z
                            result['album_z_score'] = album_z
                            artist_mean, artist_stddev, _ = get_cached_artist_stats(conn, artist)
                            artist_z = calculate_z_score_strict(popularity, artist_mean, artist_stddev)
                            result['artist_z_score'] = artist_z
                            return result
                    else:
                        log_debug(f"[MUSICBRAINZ] ✗ NOT confirmed as single by MusicBrainz")
                        log_info(f"   ⓘ MusicBrainz does not confirm single: {title}")
                        log_debug(f"   MusicBrainz result: No single found for '{title}'")
                except Exception as e:
                    # Log SSL and connection errors more gracefully
                    error_type = type(e).__name__
                    if 'SSL' in error_type or 'ssl' in str(e).lower():
                        log_debug(f"[MUSICBRAINZ] SSL ERROR: {error_type}")
                        log_info(f"   ⚠ MusicBrainz SSL connection error for {title}: {error_type}")
                        log_debug(f"   MusicBrainz API SSL error: {error_type}: {str(e)}")
                    elif 'timeout' in str(e).lower() or 'Timeout' in error_type:
                        log_debug(f"[MUSICBRAINZ] TIMEOUT ERROR: {error_type}")
                        log_info(f"   ⏱ MusicBrainz check timed out for {title}: {error_type}")
                        log_debug(f"   MusicBrainz API timeout: {error_type}: {str(e)}")
                    else:
                        log_debug(f"[MUSICBRAINZ] ERROR during lookup: {error_type}: {str(e)}")
                        log_info(f"   ⚠ MusicBrainz single check failed for {title}: {e}")
                        log_debug(f"   MusicBrainz API error: {type(e).__name__}: {str(e)}")
                    musicbrainz_confirmed = False
        else:
            musicbrainz_confirmed = False
    else:
        # Only log client availability messages in verbose mode to reduce log noise
        if verbose:
            if not musicbrainz_client:
                log_debug(f"[MUSICBRAINZ] Client not available (module import failed)")
                log_info(f"   ⓘ MusicBrainz client not available")
                log_debug(f"   MusicBrainz: Client not available (module import failed)")
            elif not getattr(musicbrainz_client, 'enabled', True):
                log_debug(f"[MUSICBRAINZ] Client disabled in configuration")
                log_info(f"   ⓘ MusicBrainz client is disabled")
                log_debug(f"   MusicBrainz: Client is disabled in configuration")
        musicbrainz_confirmed = False
    
    # STAGE 3B intentionally skipped: MusicBrainz video-relationship lookup removed
    # to reduce single-detection API latency.
    
    # STAGE 3C: MusicBrainz Various Artists Compilation Check (MEDIUM CONFIDENCE)
    # Check if the track appears on multiple Various Artists compilations
    if musicbrainz_client and hasattr(musicbrainz_client, 'enabled') and musicbrainz_client.enabled:
        if hasattr(musicbrainz_client, 'appears_on_various_artists'):
            try:
                log_debug(f"[MUSICBRAINZ] Checking for Various Artists appearances: {title} by {artist}")
                log_info(f"   Checking MusicBrainz for compilation appearances: {title}")
                
                appears_on_va = musicbrainz_client.appears_on_various_artists(lookup_title, artist)
                if appears_on_va:
                    result['single_sources'].append('musicbrainz_compilation')
                    result['single_sources_used'].append('musicbrainz_compilation')
                    log_debug(f"[MUSICBRAINZ] ✓ CONFIRMED - Track appears on multiple VA compilations (single indicator)")
                    log_info(f"   ✓ MusicBrainz compilation appearances found: {title}")
                else:
                    log_debug(f"[MUSICBRAINZ] ✗ No compilation appearances found")
            except Exception as e:
                log_debug(f"[MUSICBRAINZ] ERROR during compilation check: {type(e).__name__}: {str(e)}")
                log_info(f"   ⚠ MusicBrainz compilation check failed for {title}: {e}")
    
    # STAGE 4.5: Discogs Music Video Check (MEDIUM CONFIDENCE)
    discogs_video_confirmed = False
    # Check if we should skip Video/Last.fm checks based on current confidence
    current_confidence_high = check_high_confidence_dynamic(
        discogs_confirmed, musicbrainz_confirmed, 
        False, False, radio_edit_found,
        source_confidence_settings
    )
    
    if not current_confidence_high:
        if discogs_client and hasattr(discogs_client, 'enabled') and discogs_client.enabled:
            if hasattr(discogs_client, 'has_official_video'):
                try:
                    # Log Discogs video checks to info log only (not unified)
                    log_debug(f"[DISCOGS_VIDEO] Querying Discogs for music video: {title} by {artist}")
                    log_info(f"   Checking Discogs for music video: {title}")
                    log_debug(f"   Discogs API: Searching for music video '{lookup_title}' by '{artist}'")
                    
                    # Check for official music video
                    discogs_video_confirmed = discogs_client.has_official_video(lookup_title, artist)
                    if discogs_video_confirmed:
                        result['single_sources'].append('discogs_video')
                        result['single_sources_used'].append('discogs_video')
                        log_debug(f"[DISCOGS_VIDEO] ✓ CONFIRMED - Official video found")
                        log_info(f"   ✓ Discogs confirms music video: {title}")
                        log_debug(f"   Discogs result: Music video confirmed for '{title}'")
                        
                        # Check if HIGH confidence reached after this confirmation
                        if check_high_confidence_dynamic(
                            discogs_confirmed, musicbrainz_confirmed, 
                            discogs_video_confirmed, False, radio_edit_found,
                            source_confidence_settings
                        ):
                            # HIGH confidence achieved - can skip Last.fm
                            log_debug(f"[DETECT] Stopping early with HIGH confidence after Discogs Video confirmation")
                            result['single_status'] = 'high'
                            result['single_confidence'] = 'high'
                            result['is_single'] = True
                            result['single_confidence_score'] = 1.0
                            album_z = calculate_z_score_strict(popularity, album_mean, album_stddev)
                            result['z_score'] = album_z
                            result['album_z_score'] = album_z
                            artist_mean, artist_stddev, _ = get_cached_artist_stats(conn, artist)
                            artist_z = calculate_z_score_strict(popularity, artist_mean, artist_stddev)
                            result['artist_z_score'] = artist_z
                            return result
                    else:
                        log_debug(f"[DISCOGS_VIDEO] ✗ NOT confirmed - No official video found")
                        log_info(f"   ⓘ Discogs does not confirm music video: {title}")
                        log_debug(f"   Discogs result: No music video found for '{title}'")
                except Exception as e:
                    log_debug(f"[DISCOGS_VIDEO] ERROR during lookup: {type(e).__name__}: {str(e)}")
                    log_info(f"   ⚠ Discogs video check failed for {title}: {e}")
                    log_debug(f"   Discogs API error: {type(e).__name__}: {str(e)}")
                    discogs_video_confirmed = False
        elif verbose:
            log_debug(f"[DISCOGS_VIDEO] has_official_video method not available")
            log_info(f"   ⓘ Discogs video method not available")
            log_debug(f"   Discogs: has_official_video method not available")
    else:
        # Only log client availability messages in verbose mode to reduce log noise
        if verbose:
            if not discogs_client:
                log_debug(f"[DISCOGS_VIDEO] Client not available")
                log_info(f"   ⓘ Discogs video client not available")
                log_debug(f"   Discogs: Video client not available")
            elif not getattr(discogs_client, 'enabled', True):
                log_debug(f"[DISCOGS_VIDEO] Client disabled")
                log_info(f"   ⓘ Discogs client is disabled")
                log_debug(f"   Discogs: Client is disabled in configuration")
    
    # STAGE 4.6: Last.fm Single Check (MEDIUM CONFIDENCE)
    # Check if the track exists as a single/album on Last.fm (by track title)
    # and check album track count for traditional single detection.
    lastfm_single_confirmed = False
    
    # Only check Last.fm if we haven't reached HIGH confidence yet
    if not check_high_confidence_dynamic(
        discogs_confirmed, musicbrainz_confirmed, 
        discogs_video_confirmed, False, radio_edit_found,
        source_confidence_settings
    ):
        if lastfm_client:
            try:
                # First, check if track exists as a single on Last.fm (by track title)
                log_debug(f"[LASTFM] Checking if track exists as single: {title} by {artist}")
                track_is_single = lastfm_client.check_track_as_single(artist, lookup_title)
                
                if track_is_single:
                    result['single_sources'].append('lastfm')
                    result['single_sources_used'].append('lastfm')
                    lastfm_single_confirmed = True
                    log_debug(f"[LASTFM] ✓ CONFIRMED - Last.FM (Single) - Track '{title}' exists as single/album on Last.fm")
                    
                    # Check if HIGH confidence reached after this confirmation
                    if check_high_confidence_dynamic(
                        discogs_confirmed, musicbrainz_confirmed, 
                        discogs_video_confirmed, lastfm_single_confirmed, radio_edit_found,
                        source_confidence_settings
                    ):
                        # HIGH confidence achieved
                        log_debug(f"[DETECT] HIGH confidence achieved from Last.fm confirmation")
                        result['single_status'] = 'high'
                        result['single_confidence'] = 'high'
                        result['is_single'] = True
                        result['single_confidence_score'] = 1.0
                        album_z = calculate_z_score_strict(popularity, album_mean, album_stddev)
                        result['z_score'] = album_z
                        result['album_z_score'] = album_z
                        artist_mean, artist_stddev, _ = get_cached_artist_stats(conn, artist)
                        artist_z = calculate_z_score_strict(popularity, artist_mean, artist_stddev)
                        result['artist_z_score'] = artist_z
                        return result
                else:
                    # Fallback to album track count check
                    log_debug(f"[LASTFM] Track not found as single, checking album track count: {album} by {artist}")
                    album_track_count_lastfm = lastfm_client.get_album_track_count(artist, album)
                    
                    # Check if the album has a title track (song name matching release name)
                    has_title_track = False
                    if 4 <= album_track_count_lastfm <= 6:
                        # Only check for title track if we're in the 4-6 range
                        has_title_track = lastfm_client.has_title_track(artist, album)
                        if has_title_track:
                            log_debug(f"[LASTFM] Album has title track (song name matches release name)")
                    
                    # Singles on Last.fm typically have 1-3 tracks
                    # Or up to 6 tracks if the song name matches the release name (singles released as EPs)
                    if 1 <= album_track_count_lastfm <= 3:
                        result['single_sources'].append('lastfm')
                        result['single_sources_used'].append('lastfm')
                        lastfm_single_confirmed = True
                        log_debug(f"[LASTFM] ✓ CONFIRMED - Last.FM (Album Type) - Album has {album_track_count_lastfm} track(s) (single indicator)")
                    elif 4 <= album_track_count_lastfm <= 6 and has_title_track:
                        result['single_sources'].append('lastfm')
                        result['single_sources_used'].append('lastfm')
                        lastfm_single_confirmed = True
                        log_debug(f"[LASTFM] ✓ CONFIRMED - Last.FM (Album Type) - Album has {album_track_count_lastfm} track(s) with title track (single EP indicator)")
                    else:
                        log_debug(f"[LASTFM] Track count: {album_track_count_lastfm} (not in single range)")
                        lastfm_single_confirmed = False
                    
                    # Check if HIGH confidence reached after album count confirmation
                    if lastfm_single_confirmed and check_high_confidence_dynamic(
                        discogs_confirmed, musicbrainz_confirmed, 
                        discogs_video_confirmed, lastfm_single_confirmed, radio_edit_found,
                        source_confidence_settings
                    ):
                        # HIGH confidence achieved
                        log_debug(f"[DETECT] HIGH confidence achieved from Last.fm album track count confirmation")
                        result['single_status'] = 'high'
                        result['single_confidence'] = 'high'
                        result['is_single'] = True
                        result['single_confidence_score'] = 1.0
                        album_z = calculate_z_score_strict(popularity, album_mean, album_stddev)
                        result['z_score'] = album_z
                        result['album_z_score'] = album_z
                        artist_mean, artist_stddev, _ = get_cached_artist_stats(conn, artist)
                        artist_z = calculate_z_score_strict(popularity, artist_mean, artist_stddev)
                        result['artist_z_score'] = artist_z
                        return result
            except Exception as e:
                log_debug(f"[LASTFM] Error checking single: {e}")
                lastfm_single_confirmed = False
        else:
            log_debug(f"[LASTFM] Client not available")
    else:
        log_debug(f"[LASTFM] SKIPPED - HIGH confidence already achieved")
        lastfm_single_confirmed = False
    
    # Restore first-pass confirmations after the disabled duplicate pass.
    musicbrainz_confirmed = primary_musicbrainz_confirmed
    musicbrainz_compilation_confirmed = primary_musicbrainz_compilation_confirmed
    discogs_video_confirmed = primary_discogs_video_confirmed
    lastfm_single_confirmed = primary_lastfm_single_confirmed

    # Keep source lists stable even if any repeated append paths were hit.
    result['single_sources'] = list(dict.fromkeys(result['single_sources']))
    result['single_sources_used'] = list(dict.fromkeys(result['single_sources_used']))

    # STAGE 5: Popularity-Based Inference (including version count)
    # NOTE: Z-scores were already calculated in STAGE 2 using median+MAD
    # Reuse those values here for consistency
    # album_z and artist_z were already calculated in STAGE 2
    # Simply ensure they're stored in result for popularity inference
    
    # If we somehow bypassed STAGE 2 (edge case), recalculate using median+MAD
    if 'album_z_score' not in result or result['album_z_score'] is None:
        # Recalculate album z-score using median+MAD from STAGE 2 approach
        if album_median > 0 and album_popularities:
            from statistics import median as stat_median_stage5
            album_abs_devs = [abs(s - album_median) for s in album_popularities]
            album_mad = stat_median_stage5(album_abs_devs) if album_abs_devs else 0
            album_mad_scaled = album_mad * 1.4826
            album_spread = max(album_mad_scaled, 10.0)
            album_z = (popularity - album_median) / album_spread if album_spread > 0 else 0
        else:
            album_z = 0.0
        result['album_z_score'] = album_z
    else:
        album_z = result['album_z_score']
    
    result['z_score'] = album_z  # Store album z-score for backward compatibility
    
    # Similarly for artist z-score
    if 'artist_z_score' not in result or result['artist_z_score'] is None:
        try:
            if artist_popularities:
                from statistics import median as stat_median_artist_stage5
                artist_median_stage5 = stat_median_stage5(artist_popularities)
                artist_abs_devs = [abs(s - artist_median_stage5) for s in artist_popularities]
                artist_mad = stat_median_artist_stage5(artist_abs_devs) if artist_abs_devs else 0
                artist_mad_scaled = artist_mad * 1.4826
                artist_spread = max(artist_mad_scaled, 10.0)
                artist_z = (popularity - artist_median_stage5) / artist_spread if artist_spread > 0 else 0
            else:
                artist_z = 0.0
        except Exception as e:
            log_debug(f"[STAGE5] Could not recalculate artist z-score: {e}")
            artist_z = 0.0
        result['artist_z_score'] = artist_z
    else:
        artist_z = result['artist_z_score']
    
    if verbose:
        log_debug(f"Z-scores for '{title}': album_z={album_z:.2f}, artist_z={artist_z:.2f} (from STAGE 2, using median+MAD)")
        if 'artist_track_count' in locals():
            artist_track_count_val = locals().get('artist_track_count', 0)
            if artist_track_count_val > 0:
                log_debug(f"Artist stats: median-based z-score calculation (robust to outliers)")
    
    # ARTIST-LEVEL SANITY FILTER
    # If track.popularity < artist_mean_popularity:
    #     Reject single detection UNLESS the track has explicit metadata:
    #     - Discogs single
    #     - MusicBrainz single (strict)
    #     - Discogs music video
    #     - Last.fm single confirmation
    #
    # Do NOT allow z-score or popularity outlier to bypass this filter.
    has_explicit_metadata = (
        discogs_confirmed or musicbrainz_confirmed or discogs_video_confirmed
        or lastfm_single_confirmed
        or 'musicbrainz_video' in result['single_sources']
        or 'musicbrainz_compilation' in result['single_sources']
    )
    
    if artist_mean > 0 and popularity < artist_mean:
        if not has_explicit_metadata:
            log_debug(f"[ARTIST_FILTER] ✗ REJECTED - Artist-level sanity filter: pop={popularity:.1f} < artist_mean={artist_mean:.1f}, NO explicit metadata")
            if verbose:
                log_debug(f"Artist-level sanity filter: Rejecting {title} (pop={popularity:.1f} < artist_mean={artist_mean:.1f}, no explicit metadata)")
            # Return early - track doesn't qualify for single detection
            return result
        else:
            log_debug(f"[ARTIST_FILTER] ✓ PASSED - Despite pop={popularity:.1f} < artist_mean={artist_mean:.1f}, allowing due to explicit metadata")
            if verbose:
                log_debug(f"Artist-level sanity filter: Allowing {title} despite pop={popularity:.1f} < artist_mean={artist_mean:.1f} (has explicit metadata)")
    else:
        log_debug(f"[ARTIST_FILTER] ✓ PASSED - pop={popularity:.1f} >= artist_mean={artist_mean:.1f}")
    
    # Determine if this track is a standout across the entire artist catalogue
    # A track is considered an artist-level standout if it exceeds the artist median popularity
    is_artist_level_standout = artist_median_popularity > 0 and popularity >= artist_median_popularity
    
    log_debug(f"[ARTIST_STANDOUT] is_artist_level_standout={is_artist_level_standout} (pop={popularity:.1f}, median={artist_median_popularity:.1f})")
    
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
    # NOTE: Z-score inference is informational only - NOT added to sources
    # Medium/high confidence requires explicit metadata sources only
    # (Last.fm, Spotify, MusicBrainz, Discogs video, Radio Edit)
    if verbose and popularity_inferred:
        log_debug(f"Popularity: Z-score indicators present for {title} (album_z={album_z:.2f}, artist_z={artist_z:.2f}) - but not counted toward confidence without metadata sources")
    elif version_count_standout:
        # Version count standout is medium confidence but doesn't mark as single
        result['single_sources'].append('version_count')
        if verbose:
            log_debug(f"Version count: Medium confidence indicator for {title} (not marking as single)")
    
    # STAGE 7: Final Decision (using hybrid z-scores)
    # Check if we have any metadata sources for high-confidence determination
    # Metadata sources: discogs, musicbrainz, discogs_video
    # NOT z-score, popularity outlier, or version_count
    has_metadata = has_explicit_metadata
    
    log_debug(f"[FINAL_DECISION] Sources found: {result['single_sources']}")
    musicbrainz_video_confirmed = 'musicbrainz_video' in result['single_sources']
    musicbrainz_compilation_confirmed = 'musicbrainz_compilation' in result['single_sources']
    log_debug(f"[FINAL_DECISION] Has metadata: {has_metadata}, discogs: {discogs_confirmed}, mb: {musicbrainz_confirmed}, video: {discogs_video_confirmed}")
    log_debug(f"[FINAL_DECISION] Additional params: lastfm={lastfm_single_confirmed}, radio_edit={radio_edit_found}, album_z={album_z:.2f}, artist_z={artist_z:.2f}, version_count={version_count_value}")
    
    final_status = determine_final_status(
        discogs_confirmed,
        musicbrainz_confirmed,
        album_z,
        artist_z,
        version_count_value,
        album_is_underperforming,
        is_artist_level_standout,
        discogs_video_confirmed,
        lastfm_single_confirmed,
        musicbrainz_video_confirmed,
        musicbrainz_compilation_confirmed,
        popularity,
        album_mean,
        has_metadata,
        radio_edit_found,
        is_remastered_only
    )
    
    log_debug(f"[FINAL_DECISION] Final status determined: {final_status}")
    
    result['single_status'] = final_status
    result['single_confidence'] = final_status
    # Only high-confidence singles are marked as is_single=True.
    # Medium-confidence tracks retain their confidence level for star-rating logic
    # but do not get the is_single flag until they are promoted during star rating.
    result['is_single'] = final_status == 'high'
    
    # ===== SPECIAL CASE: High Z-Score without sources = "Popular" =====
    # If z-score > 2 but no confidence sources were found, mark as "Popular" (not as single)
    # These tracks get 5★ rating to indicate exceptional performance without single status
    max_z = max(album_z, artist_z)
    if max_z > 2.0 and final_status == 'none' and not result['is_single']:
        log_debug(f"[POPULAR] ✯ Track has exceptional z-score (max_z={max_z:.2f} > 2.0) without metadata sources - marking as 'Popular'")
        log_info(f"   ✯ {title} marked as 'Popular' (z-score {max_z:.2f}, no single sources)")
        result['single_status'] = 'popular'  # Special status for high z-score without sources
        result['single_confidence'] = 'popular'
        result['is_single'] = False  # NOT marked as single
        # Note: Star rating will be assigned separately in popularity.py based on this status
    
    # ===== EXCLUSION: Check for live/acoustic recordings =====
    # If the track has live/acoustic genre tags, exclude it from single detection
    # UNLESS the album itself is marked as a live release
    if result['is_single'] and not is_live_version_strict(title, album):
        current_genres = result.get('genres', '')
        if should_exclude_from_single_detection(current_genres, is_live_release=False):
            log_debug(f"[EXCLUDE] Track has live/acoustic tag(s) - excluding from single detection: {title}")
            result['is_single'] = False
            result['single_status'] = 'none'
            result['single_confidence'] = 'none'
            result['single_confidence_score'] = 0.0
    
    log_debug(f"[RESULT] is_single set to: {result['is_single']} (status: {final_status})")
    
    # Map confidence to numeric score (including 'popular' for display purposes)
    confidence_scores = {'high': 1.0, 'medium': 0.67, 'low': 0.33, 'popular': 0.5, 'none': 0.0}
    result['single_confidence_score'] = confidence_scores.get(final_status, 0.0)
    
    # Add final debug summary per track
    if verbose:
        log_debug(f"[DEBUG] Single detection sources for {title}: {result['single_sources']}")
        log_debug(f"[DEBUG] Final single status for {title}: {final_status}")
    
    log_debug(f"[DETECT] ✓ Detection complete for '{title}' - is_single={result['is_single']}, confidence={final_status}")
    
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
    import time
    import random
    
    max_retries = 5
    retry_count = 0
    base_delay = 0.5  # Start with 500ms
    
    while retry_count < max_retries:
        cursor = None
        try:
            placeholder = "%s"
            
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'tracks' AND column_name IN ('album_z_score', 'artist_z_score')
            """)
            existing_cols = {row['column_name'] for row in cursor.fetchall()}
            has_album_z = 'album_z_score' in existing_cols
            has_artist_z = 'artist_z_score' in existing_cols
            
            # Get z_score values with defaults
            z_score = result.get('z_score', 0.0)
            album_z_score = result.get('album_z_score', z_score)
            artist_z_score = result.get('artist_z_score', 0.0)
            
            # Update with new columns if they exist
            if has_album_z and has_artist_z:
                cursor.execute(f"""
                    UPDATE tracks
                    SET single_status = {placeholder},
                        single_confidence_score = {placeholder},
                        single_sources_used = {placeholder},
                        z_score = {placeholder},
                        album_z_score = {placeholder},
                        artist_z_score = {placeholder},
                        spotify_version_count = {placeholder},
                        discogs_release_ids = {placeholder},
                        musicbrainz_release_group_ids = {placeholder},
                        single_detection_last_updated = {placeholder},
                        is_single = {placeholder},
                        single_confidence = {placeholder},
                        single_sources = {placeholder}
                    WHERE id = {placeholder}
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
                    result['is_single'],
                    result['single_confidence'],
                    json.dumps(result['single_sources']),
                    track_id
                ))
            else:
                # Fallback to old schema without new z-score columns
                cursor.execute(f"""
                    UPDATE tracks
                    SET single_status = {placeholder},
                        single_confidence_score = {placeholder},
                        single_sources_used = {placeholder},
                        z_score = {placeholder},
                        spotify_version_count = {placeholder},
                        discogs_release_ids = {placeholder},
                        musicbrainz_release_group_ids = {placeholder},
                        single_detection_last_updated = {placeholder},
                        is_single = {placeholder},
                        single_confidence = {placeholder},
                        single_sources = {placeholder}
                    WHERE id = {placeholder}
                """, (
                    result['single_status'],
                    result['single_confidence_score'],
                    json.dumps(result['single_sources_used']),
                    z_score,
                    result['spotify_version_count'],
                    json.dumps(result.get('discogs_release_ids', [])),
                    json.dumps(result.get('musicbrainz_release_group_ids', [])),
                    result['single_detection_last_updated'],
                    result['is_single'],
                    result['single_confidence'],
                    json.dumps(result['single_sources']),
                    track_id
                ))
            
            conn.commit()
            return  # Success - exit retry loop
            
        except Exception as e:
            # Rollback on error to unlock the database
            try:
                conn.rollback()
            except:
                pass
            
            # Close cursor if it's open
            if cursor:
                try:
                    cursor.close()
                except:
                    pass
            
            error_msg = str(e).lower()
            
            # Check if it's a database lock error
            if 'database is locked' in error_msg or 'locked' in error_msg:
                retry_count += 1
                if retry_count >= max_retries:
                    # Final attempt failed - log and give up
                    log_debug(f"Database lock timeout after {max_retries} retries for track {track_id} (final error: {e})")
                    raise
                
                # Calculate exponential backoff with jitter
                delay = base_delay * (2 ** (retry_count - 1))
                # Add random jitter to avoid thundering herd
                delay += random.uniform(0, delay * 0.1)
                
                log_debug(f"Database locked writing single detection for track {track_id}, retrying in {delay:.3f}s (attempt {retry_count}/{max_retries})")
                time.sleep(delay)
            else:
                # Not a lock error - don't retry, just raise
                raise
