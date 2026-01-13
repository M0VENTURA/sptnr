
# --- DB Helper for single detection state ---
import sqlite3
import json
import logging

def get_db_connection():
    from start import DB_PATH
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
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
    - Audit fields: is_canonical_title, title_similarity_to_base, discogs_single_confirmed
    
    Note: Stars are assigned later in the calling code based on is_single status and popularity scores.
    """
    from start import (
        _base_title,
        _has_subtitle_variant,
        _similar,
        is_valid_version,
        DISCOGS_ENABLED,
        DISCOGS_TOKEN,
        MUSICBRAINZ_ENABLED,
        CONTEXT_FALLBACK_STUDIO,
        config as global_config,
    )
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
    # Get track ID
    track_id = track.get("id", "")
    # Check for existing Spotify single detection results
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
    
    # Determine if track is a single based on multiple criteria
    is_single = False
    confidence = "low"
    
    # High confidence: Discogs confirms AND track is canonical
    if discogs_single_hit and canonical and not has_subtitle and sim_to_base >= title_sim_threshold:
        is_single = True
        confidence = "high"
    # Medium confidence: Spotify single match
    elif spotify_matched and canonical:
        is_single = True
        confidence = "medium"
    # Low confidence: Short release (1-2 tracks) if configured to count them
    elif short_release and count_short_release_as_match and canonical:
        is_single = True
        confidence = "low"
    
    # Update track fields
    track["is_single"] = is_single
    track["single_sources"] = list(sources)
    track["single_confidence"] = confidence
    
    if verbose and is_single:
        logging.info(f"✅ Single detected: {title} (confidence={confidence}, sources={sources})")
    elif verbose:
        logging.info(f"❌ Not a single: {title}")
    
    return track
