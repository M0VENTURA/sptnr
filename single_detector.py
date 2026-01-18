# Explicitly export the main API for importers
__all__ = ["rate_track_single_detection", "WEIGHTS"]

# --- DB Helper for single detection state ---
import sqlite3
import json
import logging
import re
import difflib
import os

# Import centralized logging
from logging_config import setup_logging, log_unified, log_info, log_debug

# Set up logging for single detector service
setup_logging("single_detector")

def get_db_connection():
    from start import DB_PATH
    conn = sqlite3.connect(DB_PATH, timeout=120.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn

def get_current_single_detection(track_id: str) -> dict:
    """Query the current single detection values from the database.
    Returns dict with is_single, single_confidence, single_sources, and stars.
    This is used to preserve user-edited single detection and star ratings across rescans.
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT is_single, single_confidence, single_sources, stars FROM tracks WHERE id = ?",
            (track_id,)
        )
        row = cursor.fetchone()
        conn.close()
        if row:
            is_single, confidence, sources_json, stars = row
            sources = json.loads(sources_json) if sources_json else []
            return {
                "is_single": bool(is_single),
                "single_confidence": confidence or "low",
                "single_sources": sources,
                "stars": stars or 0
            }
        return {"is_single": False, "single_confidence": "low", "single_sources": [], "stars": 0}
    except Exception as e:
        log_debug(f"Failed to get current single detection for track {track_id}: {e}")
        return {"is_single": False, "single_confidence": "low", "single_sources": [], "stars": 0}

# --- Helper functions for title analysis ---
def _base_title(title: str) -> str:
    """Remove common subtitle patterns to get base title."""
    # Remove anything in parentheses or brackets
    cleaned = re.sub(r'\s*[\(\[].*?[\)\]]', '', title)
    # Remove common suffixes
    cleaned = re.sub(r'\s*-\s*(Live|Remix|Remaster|Edit|Mix|Version).*$', '', cleaned, flags=re.IGNORECASE)
    return cleaned.strip()

def _has_subtitle_variant(title: str) -> bool:
    """Check if title has subtitle indicators (parentheses, brackets, dashes)."""
    return bool(re.search(r'[\(\[\-]', title))

def _similar(a: str, b: str) -> float:
    """Calculate similarity ratio between two strings."""
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()

def is_valid_version(title: str, allow_live_remix: bool = False) -> bool:
    """
    Check if track title is a valid canonical version.
    Returns True if it's not a live/remix/etc variant (unless allow_live_remix=True).
    """
    lower_title = title.lower()
    
    # Patterns that indicate non-canonical versions
    non_canonical_patterns = [
        r'\bremix\b',
        r'\bedit\b',
        r'\bmix\b',
        r'\bremaster\b',
        r'\bacoustic\b',
        r'\bdemo\b',
        r'\bkaraoke\b',
        r'\binstrumental\b',
    ]
    
    # Live/unplugged patterns (allowed if allow_live_remix=True)
    live_patterns = [
        r'\blive\b',
        r'\bunplugged\b',
    ]
    
    # Check non-canonical patterns
    for pattern in non_canonical_patterns:
        if re.search(pattern, lower_title):
            return False
    
    # Check live patterns
    if not allow_live_remix:
        for pattern in live_patterns:
            if re.search(pattern, lower_title):
                return False
    
    return True

# --- Import the advanced single detection logic from singledetection.py if needed ---
def rate_track_single_detection(
    track: dict,
    artist_name: str,
    album_ctx: dict,
    config: dict,
    title_sim_threshold: float = 0.92,
    count_short_release_as_match: bool = False,
    use_lastfm_single: bool = True,
    verbose: bool = False
) -> dict:
    """
    Perform single detection on a track and update its fields with single status, sources, and confidence.
    
    This function now delegates to the canonical single detection logic in popularity.py
    to ensure consistency across the codebase.
    
    Returns the updated track dict with:
    - is_single (bool)
    - single_sources (str): JSON-encoded list of sources
    - single_confidence (str): 'high', 'medium', 'low'
    - Audit fields: is_canonical_title, title_similarity_to_base, discogs_single_confirmed, discogs_video_found
    
    Note: Parameters config, title_sim_threshold, count_short_release_as_match, and use_lastfm_single
    are kept for backward compatibility but are not used in the delegated implementation.
    The canonical logic in popularity.py handles these concerns internally.
    """
    # Import the canonical single detection function from popularity.py
    from popularity import detect_single_for_track
    
    title = track.get("title", "")
    canonical_base = _base_title(title)
    sim_to_base = _similar(title, canonical_base)
    has_subtitle = _has_subtitle_variant(title)
    
    if verbose:
        logging.info(f"🎵 Checking: {title}")
    
    allow_live_remix = bool(album_ctx.get("is_live") or album_ctx.get("is_unplugged"))
    canonical = is_valid_version(title, allow_live_remix=allow_live_remix)
    
    # ✅ Store canonical title audit fields
    track['is_canonical_title'] = 1 if canonical else 0
    track['title_similarity_to_base'] = sim_to_base
    
    # Initialize audit fields to 0
    track['discogs_single_confirmed'] = 0
    track['discogs_video_found'] = 0
    
    # Get album track count from context if available
    album_track_count = album_ctx.get("total_tracks", 1)
    
    # Call the canonical single detection function from popularity.py
    detection_result = detect_single_for_track(
        title=title,
        artist=artist_name,
        album_track_count=album_track_count,
        spotify_results_cache=None,  # Not using cache in single_detector context
        verbose=verbose
    )
    
    # Extract results
    sources = detection_result["sources"]
    single_confidence = detection_result["confidence"]
    is_single = detection_result["is_single"]
    
    # Update track with results - ensure sources is a list before converting to JSON
    track["is_single"] = is_single
    track["single_confidence"] = single_confidence
    track["single_sources"] = json.dumps(list(sources) if not isinstance(sources, list) else sources)
    
    # Set audit fields for backward compatibility with tests
    if "discogs" in sources:
        track['discogs_single_confirmed'] = 1
    if "discogs_video" in sources:
        track['discogs_video_found'] = 1
    
    if verbose:
        if is_single:
            source_str = ", ".join(sources)
            logging.info(f"✅ SINGLE ({single_confidence} confidence, sources: {source_str}): {title}")
        else:
            logging.info(f"❌ NOT SINGLE ({single_confidence} confidence): {title}")
    
    return track
