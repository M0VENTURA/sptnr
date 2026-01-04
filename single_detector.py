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
