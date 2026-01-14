# Explicitly export the main API for importers
__all__ = ["rate_track_single_detection", "WEIGHTS"]

# --- DB Helper for single detection state ---
import sqlite3
import json
import logging
import re
import difflib

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
    import os
    
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
                tags = [t.get("name", "").lower() for t in track_info.get("toptags", {}).get("tag", [])]
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
#!/usr/bin/env python3
"""
Advanced Single Detection with Configurable Weights and Explainable Decisions

Uses multiple sources (Discogs, Spotify, MusicBrainz, Last.fm) with weighted scoring
to determine if a track is a single vs album track.
"""

from dataclasses import dataclass, field
from typing import Dict, Any, List, Tuple, Optional
import logging

logger = logging.getLogger(__name__)

# ----------------------------
# Configuration (tune these)
# ----------------------------
WEIGHTS = {
    "discogs_single": 100,            # hard guarantee
    "spotify_single": 50,
    "musicbrainz_single": 50,
    "lastfm_single_tag": 40,
    "discogs_video": 20               # intentionally low due to false positives
}

THRESHOLD_SCORE = 100                # total weighted score needed (when NOT Discogs-confirmed)
REQUIRED_MATCH_COUNT = 2             # minimum count of confirming sources (excluding Discogs single hard match)

# Artists known to publish official videos for non-singles; add more as you encounter them
VIDEO_EXCEPTION_ARTISTS = {
    "Weird Al Yankovic": {"video_weight": 5}  # reduce video weight drastically
}

# Terms in Discogs/MB that indicate non-single promotional/album videos
VIDEO_EXCLUDE_TERMS = {"promo", "album version", "official video (album track)"}

# Use these to treat formats as single-like even if the word "Single" isn't present
SINGLE_FORMAT_HINTS = {"single", "maxi-single", "7\"", "12\""}


@dataclass
class TrackMeta:
    """Metadata container for single detection."""
    artist: str
    title: str
    album: Optional[str] = None
    isrc: Optional[str] = None
    release_date: Optional[str] = None
    # Source-specific identifiers if you have them
    spotify_id: Optional[str] = None
    discogs_release_id: Optional[str] = None
    musicbrainz_recording_id: Optional[str] = None
    musicbrainz_release_group_id: Optional[str] = None
    lastfm_track_mbid: Optional[str] = None
    # Raw source payloads (optional, if you already have the JSON blobs)
    raw_sources: Dict[str, Any] = field(default_factory=dict)


# ------------------------------------
# Source check helpers (plug yours in)
# ------------------------------------
def check_discogs_single(meta: TrackMeta, discogs_data: Optional[Dict[str, Any]] = None) -> Tuple[bool, Dict]:
    """
    Return (is_single, details). Treat as guaranteed if Discogs release 'format' or 'type' clearly indicates Single.
    """
    data = discogs_data or meta.raw_sources.get("discogs")
    details = {"source": "discogs", "reason": [], "release_id": meta.discogs_release_id}
    if not data:
        details["reason"].append("No Discogs data")
        return False, details

    # Discogs structure varies; check common fields
    formats = set()
    release_types = set()
    track_count = None

    # Example field extraction; adjust to your Discogs data shape
    if "formats" in data:
        for f in data["formats"]:
            name = (f.get("name") or "").lower()
            descriptions = [d.lower() for d in f.get("descriptions", [])]
            formats.add(name)
            release_types.update(descriptions)

    if "tracklist" in data:
        track_count = len(data["tracklist"])

    release_title = (data.get("title") or "").lower()

    # Guarantee rules
    is_single = (
        ("single" in release_types or "single" in formats) or
        any(h in formats for h in SINGLE_FORMAT_HINTS)
    )
    if is_single:
        details["reason"].append(f"Discogs formats/types indicate Single ({formats or release_types})")
        return True, details

    # Heuristic: very short releases (1–3 tracks) with single-like formats
    if track_count and track_count <= 3 and any(h in formats for h in SINGLE_FORMAT_HINTS):
        details["reason"].append(f"Discogs short release ({track_count} tracks) with single-like format")
        return True, details

    details["reason"].append("Discogs did not indicate Single")
    return False, details


def check_discogs_video(meta: TrackMeta, discogs_video_data: Optional[List[Dict[str, Any]]] = None) -> Tuple[bool, Dict]:
    """
    Return (video_confirms_single, details). Conservative: video rarely proves single status.
    """
    videos = discogs_video_data or meta.raw_sources.get("discogs_videos")
    details = {"source": "discogs_video", "reason": [], "matches": 0}
    if not videos:
        details["reason"].append("No Discogs video entries")
        return False, details

    # Basic filters
    artist = meta.artist
    lowered_exclude = {t.lower() for t in VIDEO_EXCLUDE_TERMS}

    valid_count = 0
    for v in videos:
        title = (v.get("title") or "").lower()
        description = (v.get("description") or "").lower()
        is_official = "official" in title or "official" in description

        # Reject promos/album videos
        if any(term in title or term in description for term in lowered_exclude):
            continue

        # Require track title match (simple containment)
        if meta.title.lower() in title or meta.title.lower() in description:
            # Optional: check release linkage if available
            valid_count += 1 if is_official else 0.5

    if valid_count == 0:
        details["reason"].append("No usable official video match")
        return False, details

    details["matches"] = valid_count
    details["reason"].append(f"Found {valid_count} official-ish video matches")

    # Apply exception downweighting
    return True, details


def check_spotify_single(meta: TrackMeta, spotify_data: Optional[Dict[str, Any]] = None) -> Tuple[bool, Dict]:
    """
    Return (is_single, details) based on Spotify track/album metadata.
    """
    data = spotify_data or meta.raw_sources.get("spotify")
    details = {"source": "spotify", "reason": []}
    if not data:
        details["reason"].append("No Spotify data")
        return False, details

    # Spotify 'album' object often has 'album_type' that can be 'single'|'album'|'compilation'
    album_type = (data.get("album", {}).get("album_type") or "").lower()
    if album_type == "single":
        details["reason"].append("Spotify album_type=single")
        return True, details

    # Heuristics: fewer tracks in album + release date alignment
    total_tracks = data.get("album", {}).get("total_tracks")
    if total_tracks and total_tracks <= 3:
        details["reason"].append(f"Spotify album total_tracks={total_tracks} (single-like)")
        return True, details

    details["reason"].append("Spotify did not indicate Single")
    return False, details


def check_musicbrainz_single(meta: TrackMeta, mb_data: Optional[Dict[str, Any]] = None) -> Tuple[bool, Dict]:
    """
    Return (is_single, details) based on MusicBrainz 'release-group.primary-type' == 'Single'.
    """
    data = mb_data or meta.raw_sources.get("musicbrainz")
    details = {"source": "musicbrainz", "reason": []}
    if not data:
        details["reason"].append("No MusicBrainz data")
        return False, details

    # Typical MB structure: release-group.primary-type
    rg = data.get("release-group") or data.get("release_group") or {}
    primary_type = (rg.get("primary-type") or rg.get("primary_type") or "").lower()
    if primary_type == "single":
        details["reason"].append("MusicBrainz release-group.primary-type=Single")
        return True, details

    # Fallback: short release with single-like format
    formats = {f.lower() for f in rg.get("secondary-types", [])}
    track_count = data.get("track_count") or rg.get("track_count")
    if track_count and track_count <= 3 and ("single" in formats or any(h in formats for h in SINGLE_FORMAT_HINTS)):
        details["reason"].append("MB short release with single-like format")
        return True, details

    details["reason"].append("MusicBrainz did not indicate Single")
    return False, details


def check_lastfm_single(meta: TrackMeta, lastfm_data: Optional[Dict[str, Any]] = None) -> Tuple[bool, Dict]:
    """
    Return (is_single, details) using tags or top-tags. Last.fm is community-driven; treat cautiously.
    """
    data = lastfm_data or meta.raw_sources.get("lastfm")
    details = {"source": "lastfm", "reason": []}
    if not data:
        details["reason"].append("No Last.fm data")
        return False, details

    tags = [t.get("name", "").lower() for t in data.get("toptags", {}).get("tag", [])]
    if "single" in tags:
        details["reason"].append("Last.fm top tag includes 'single'")
        return True, details

    details["reason"].append("Last.fm tags did not indicate Single")
    return False, details


# --------------------------------------------------
# Aggregator & decision (explainable output)
# --------------------------------------------------
def decide_is_single(meta: TrackMeta) -> Dict[str, Any]:
    """
    Evaluate sources, apply weights and rules, and return a decision dict:
    {
      'is_single': bool,
      'score': int,
      'matches': int,
      'reasons': [str],
      'source_breakdown': {source: {'is_single': bool, 'weight': int, 'details': dict}}
    }
    """
    reasons: List[str] = []
    breakdown: Dict[str, Dict[str, Any]] = {}
    total_score = 0
    matches = 0

    # 1) Discogs single (guarantee)
    d_single, d_details = check_discogs_single(meta)
    breakdown["discogs_single"] = {"is_single": d_single, "weight": WEIGHTS["discogs_single"], "details": d_details}
    if d_single:
        reasons.extend(d_details["reason"])
        return {
            "is_single": True,
            "score": WEIGHTS["discogs_single"],
            "matches": 1,  # guaranteed by Discogs
            "reasons": reasons,
            "source_breakdown": breakdown
        }

    # 2) Secondary sources (need >=2 matches + score threshold)
    s_spotify, s_spotify_det = check_spotify_single(meta)
    if s_spotify:
        total_score += WEIGHTS["spotify_single"]
        matches += 1
    breakdown["spotify"] = {"is_single": s_spotify, "weight": WEIGHTS["spotify_single"], "details": s_spotify_det}
    reasons.extend(s_spotify_det["reason"])

    s_mb, s_mb_det = check_musicbrainz_single(meta)
    if s_mb:
        total_score += WEIGHTS["musicbrainz_single"]
        matches += 1
    breakdown["musicbrainz"] = {"is_single": s_mb, "weight": WEIGHTS["musicbrainz_single"], "details": s_mb_det}
    reasons.extend(s_mb_det["reason"])

    s_lfm, s_lfm_det = check_lastfm_single(meta)
    if s_lfm:
        total_score += WEIGHTS["lastfm_single_tag"]
        matches += 1
    breakdown["lastfm"] = {"is_single": s_lfm, "weight": WEIGHTS["lastfm_single_tag"], "details": s_lfm_det}
    reasons.extend(s_lfm_det["reason"])

    # 3) Discogs video (guarded + exceptions)
    s_video, s_video_det = check_discogs_video(meta)
    video_weight = WEIGHTS["discogs_video"]
    if meta.artist in VIDEO_EXCEPTION_ARTISTS:
        video_weight = VIDEO_EXCEPTION_ARTISTS[meta.artist]["video_weight"]

    if s_video:
        total_score += int(video_weight)
        # Count as 1 match only if valid_count >= 1; we already reduced the weight for exception artists
        matches += 1

    breakdown["discogs_video"] = {"is_single": s_video, "weight": int(video_weight), "details": s_video_det}
    reasons.extend(s_video_det["reason"])

    # 4) Final decision
    is_single = (matches >= REQUIRED_MATCH_COUNT) and (total_score >= THRESHOLD_SCORE)
    reasons.append(f"Final: matches={matches}, score={total_score}, threshold={THRESHOLD_SCORE}")

    return {
        "is_single": is_single,
        "score": total_score,
        "matches": matches,
        "reasons": reasons,
        "source_breakdown": breakdown
    }


# -------------------------
# Example usage (replace)
# -------------------------
if __name__ == "__main__":
    # Example: populate 'raw_sources' with your already-fetched metadata objects
    example_meta = TrackMeta(
        artist="Weird Al Yankovic",
        title="Hardware Store",
        album="Poodle Hat",
        isrc="USKO10300001",
        raw_sources={
            "discogs": {
                "formats": [{"name": "CD", "descriptions": ["Album"]}],
                "tracklist": [{"title": "Hardware Store"}]  # album track
            },
            "discogs_videos": [
                {"title": "Weird Al - Hardware Store (Official Video)", "description": "Official video from album"}
            ],
            "spotify": {
                "album": {"album_type": "album", "total_tracks": 12}
            },
            "musicbrainz": {
                "release-group": {"primary-type": "Album", "secondary-types": ["Album"], "track_count": 12}
            },
            "lastfm": {
                "toptags": {"tag": [{"name": "parody"}, {"name": "comedy"}, {"name": "single"}]}
            }
        }
    )

    decision = decide_is_single(example_meta)
    print(decision)
