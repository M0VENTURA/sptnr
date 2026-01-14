# Explicitly export the main API for importers
__all__ = ["rate_track_single_detection", "WEIGHTS"]

# --- DB Helper for single detection state ---
import sqlite3
import json
import logging
import re
import difflib
import os

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
        logging.debug(f"Failed to get current single detection for track {track_id}: {e}")
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
    Perform single detection on a track and update its fields with single status, sources, and star assignment.
    Returns the updated track dict with:
    - is_single (bool)
    - single_sources (list)
    - single_confidence (str): 'high', 'medium', 'low'
    - stars (int): 5 for confirmed singles, 2 for single hints, 1 default
    - Audit fields: is_canonical_title, title_similarity_to_base, discogs_single_confirmed, discogs_video_found, album_context_live
    """
    # Get API configuration from environment
    DISCOGS_ENABLED = bool(os.getenv("DISCOGS_TOKEN"))
    DISCOGS_TOKEN = os.getenv("DISCOGS_TOKEN", "")
    MUSICBRAINZ_ENABLED = True  # MusicBrainz doesn't require auth
    LASTFM_API_KEY = os.getenv("LASTFM_API_KEY", "")
    
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
    # ✅ Get track ID for caching detection results
    track_id = track.get("id", "")
    # ✅ Try to get cached source detection results
    # (Assume get_cached_source_detections is available in singledetection.py if needed)
    # For now, skip cache for minimal move
    spotify_matched = bool(track.get("is_spotify_single"))
    tot = track.get("spotify_total_tracks")
    short_release = (tot is not None and tot > 0 and tot <= 2)
    # Accumulate sources for visibility
    sources = set()
    if spotify_matched:
        sources.add("spotify")
    if short_release:
        sources.add("short_release")
    if verbose:
        hints = []
        if spotify_matched:
            hints.append("Spotify single")
        if short_release:
            hints.append(f"short release ({tot} tracks)")
        if hints:
            logging.info(f"💡 Initial hints: {', '.join(hints)}")
    
    # --- Discogs Single (hard stop) - Check online and cache the result ---
    discogs_single_hit = False
    try:
        if verbose:
            logging.info("🔍 Checking Discogs single (online)...")
        logging.debug(f"Checking Discogs single for '{title}' by '{artist_name}'")
        from api_clients.discogs import is_discogs_single
        if DISCOGS_ENABLED and DISCOGS_TOKEN:
            discogs_single_hit = is_discogs_single(title, artist_name, album_ctx, token=DISCOGS_TOKEN)
            if discogs_single_hit:
                sources.add("discogs")
                track['discogs_single_confirmed'] = 1
                logging.debug(f"Discogs single detected for '{title}' (sources={sources})")
                if verbose:
                    logging.info("✅ Discogs single FOUND")
            else:
                logging.debug(f"Discogs single not detected for '{title}'")
                if verbose:
                    logging.info("❌ Discogs single not found")
    except Exception as e:
        logging.exception(f"is_discogs_single failed for '{title}': {e}")
    
    # --- MusicBrainz Single - Check online ---
    musicbrainz_single_hit = False
    try:
        if verbose:
            logging.info("🔍 Checking MusicBrainz single (online)...")
        logging.debug(f"Checking MusicBrainz single for '{title}' by '{artist_name}'")
        from api_clients.musicbrainz import is_musicbrainz_single
        if MUSICBRAINZ_ENABLED:
            musicbrainz_single_hit = is_musicbrainz_single(title, artist_name, enabled=True)
            if musicbrainz_single_hit:
                sources.add("musicbrainz")
                logging.debug(f"MusicBrainz single detected for '{title}' (sources={sources})")
                if verbose:
                    logging.info("✅ MusicBrainz single FOUND")
            else:
                logging.debug(f"MusicBrainz single not detected for '{title}'")
                if verbose:
                    logging.info("❌ MusicBrainz single not found")
    except Exception as e:
        logging.exception(f"is_musicbrainz_single failed for '{title}': {e}")
    
    # --- Last.fm Single Tag - Check online ---
    lastfm_single_hit = False
    if use_lastfm_single and LASTFM_API_KEY:
        try:
            if verbose:
                logging.info("🔍 Checking Last.fm tags (online)...")
            logging.debug(f"Checking Last.fm tags for '{title}' by '{artist_name}'")
            from api_clients.lastfm import LastFmClient
            lastfm_client = LastFmClient(api_key=LASTFM_API_KEY)
            # Get track info with tags
            track_info = lastfm_client.get_track_info(artist_name, title)
            # Check if 'single' tag is present in toptags
            if track_info and 'toptags' in track_info:
                toptags = track_info.get("toptags", {})
                tag_list = toptags.get("tag", [])
                # Ensure tag_list is a list (API can return a single dict if there's only one tag)
                if isinstance(tag_list, dict):
                    tag_list = [tag_list]
                tags = [t.get("name", "").lower() for t in tag_list if isinstance(t, dict)]
                if "single" in tags:
                    lastfm_single_hit = True
                    sources.add("lastfm")
                    logging.debug(f"Last.fm single tag detected for '{title}' (sources={sources})")
                    if verbose:
                        logging.info("✅ Last.fm single tag FOUND")
                else:
                    logging.debug(f"Last.fm single tag not detected for '{title}'")
                    if verbose:
                        logging.info("❌ Last.fm single tag not found")
        except Exception as e:
            logging.exception(f"Last.fm tag check failed for '{title}': {e}")
    
    # --- Discogs Video - Check online ---
    discogs_video_hit = False
    try:
        if verbose:
            logging.info("🔍 Checking Discogs music video (online)...")
        logging.debug(f"Checking Discogs music video for '{title}' by '{artist_name}'")
        from api_clients.discogs import has_discogs_video
        if DISCOGS_ENABLED and DISCOGS_TOKEN:
            discogs_video_hit = has_discogs_video(title, artist_name, token=DISCOGS_TOKEN)
            if discogs_video_hit:
                sources.add("discogs_video")
                track['discogs_video_found'] = 1
                logging.debug(f"Discogs music video detected for '{title}' (sources={sources})")
                if verbose:
                    logging.info("✅ Discogs music video FOUND")
            else:
                logging.debug(f"Discogs music video not detected for '{title}'")
                if verbose:
                    logging.info("❌ Discogs music video not found")
    except Exception as e:
        logging.exception(f"Discogs video check failed for '{title}': {e}")
    
    # --- Calculate confidence and is_single ---
    # Discogs is a hard guarantee if canonical
    if discogs_single_hit and canonical and not has_subtitle and sim_to_base >= title_sim_threshold:
        track["is_single"] = True
        track["single_confidence"] = "high"
        track["single_sources"] = json.dumps(list(sources))
        if verbose:
            logging.info(f"✅ SINGLE (Discogs guarantee): {title}")
    # Multiple sources indicate high confidence
    elif len(sources) >= 2:
        track["is_single"] = True
        track["single_confidence"] = "high"
        track["single_sources"] = json.dumps(list(sources))
        if verbose:
            logging.info(f"✅ SINGLE (multiple sources): {title}")
    # Single source indicates medium confidence
    elif len(sources) == 1:
        # For medium confidence, only mark as single if canonical
        if canonical:
            track["is_single"] = True
            track["single_confidence"] = "medium"
            track["single_sources"] = json.dumps(list(sources))
            if verbose:
                logging.info(f"✅ SINGLE (single source, canonical): {title}")
        else:
            track["is_single"] = False
            track["single_confidence"] = "low"
            track["single_sources"] = json.dumps(list(sources))
            if verbose:
                logging.info(f"❌ NOT SINGLE (single source, non-canonical): {title}")
    else:
        track["is_single"] = False
        track["single_confidence"] = "low"
        track["single_sources"] = json.dumps(list(sources))
        if verbose:
            logging.info(f"❌ NOT SINGLE (no sources): {title}")
    
    return track
