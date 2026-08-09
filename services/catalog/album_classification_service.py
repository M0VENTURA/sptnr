"""Album and track classification helpers for scan/stat eligibility."""

from __future__ import annotations
import re
from collections import defaultdict

from helpers.normalization_service import normalize_title_for_lookup


# --------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------

COMPILATION_TYPES = {"compilation", "soundtrack", "various artists"}

LIVE_ALBUM_PATTERNS = [
    r"\(live\)\s*$",
    r"\[live\]\s*$",
    r"-\s*live\s*$",
    r",\s*live\s*$",
    r"\+\s*live\s*$",
    r"\s+live\s*$",
    r"\s+live\s*[\)\]]\s*$",
    r"live\s+at\b",
    r"live\s+in\b",
    r"live\s+from\b",
    r"live\s+session\b",
    r"live\s+recording\b",
    r"live\s+tour\b",
    r"\bconcert\b",
    r"\bin\s+concert\b",
    r"\bunplugged\b",
    r"\bacoustic\b",
    r"\borchestral\b",
]

ALT_TRACK_PATTERNS = [
    r"\blive\b",
    r"\bunplugged\b",
    r"\bacoustic\b",
    r"\borchestral\b",
    r"\bremix\b",
    r"\bdemo\b",
    r"\binstrumental\b",
    r"\bkaraoke\b",
]


# --------------------------------------------------------------------
# Album classification
# --------------------------------------------------------------------

def is_compilation_type(album_type: str) -> bool:
    return any(token in (album_type or "").lower() for token in COMPILATION_TYPES)


def normalize_primary_release_type(album_type: str) -> str:
    value = (album_type or "").lower()

    if "single" in value:
        return "single"
    if "ep" in value:
        return "ep"
    if "album" in value:
        return "album"
    if "compilation" in value:
        return "compilation"

    return value.strip()


def is_live_or_alternate_album(album: str) -> bool:
    return any(re.search(pattern, album or "", re.IGNORECASE) for pattern in LIVE_ALBUM_PATTERNS)


def detect_live_album_type(album: str, album_type_from_field: str = "") -> str:
    # ``album_type_from_field`` (e.g. the MusicBrainz release type) is
    # authoritative — if the provider says the release is acoustic/live, trust
    # it even when the title alone is ambiguous.
    field_text = (album_type_from_field or "").lower()
    if "acoustic" in field_text or "unplugged" in field_text:
        return "acoustic"
    if "live" in field_text:
        return "live"

    title = album or ""
    title_text = title.lower()
    if "acoustic" in title_text or "unplugged" in title_text:
        return "acoustic"
    # Title-based live detection uses the format-tag patterns only — a bare
    # "live" word inside a phrase ("(how to live) as ghosts") is NOT a live
    # album and must not cap the album's star ratings at 4★.
    if is_live_album_enhanced(title):
        return "live"

    return ""


# --------------------------------------------------------------------
# Track-level classification
# --------------------------------------------------------------------

def is_live_or_unplugged_track_title(title: str) -> bool:
    return any(re.search(p, title or "", re.IGNORECASE) for p in [r"\blive\b", r"\bunplugged\b"])


def should_exclude_track_from_stats(
    title: str,
    album: str = "",
    is_live: int = 0,
    album_context_live: int = 0,
) -> bool:

    if is_live or album_context_live:
        return True

    if is_live_or_alternate_album(album):
        return True

    return any(re.search(pattern, title or "", re.IGNORECASE) for pattern in ALT_TRACK_PATTERNS)


# --------------------------------------------------------------------
# Advanced helpers
# --------------------------------------------------------------------

def detect_alternate_takes(tracks: list) -> dict:
    groups = defaultdict(list)

    for track in tracks or []:
        title = track.get("title") if isinstance(track, dict) else str(track)
        groups[normalize_title_for_lookup(title)].append(track)

    return {k: v for k, v in groups.items() if len(v) > 1}


def detect_compilation_album(
    artist: str,
    album: str,
    tracks: list,
    album_artist: str | None = None,
    spotify_album_type: str | None = None,
) -> bool:

    if is_compilation_type(spotify_album_type or ""):
        return True

    if (album_artist or "").lower() in {"various artists", "various"}:
        return True

    artists = {
        str(t.get("artist", "")).strip().lower()
        for t in tracks or []
        if isinstance(t, dict) and t.get("artist")
    }

    return len(artists) >= 4 and (artist or "").lower() not in artists


def detect_greatest_hits_album(album: str, artist: str) -> bool:
    text = (album or "").lower()

    return any(
        token in text
        for token in ["greatest hits", "best of", "anthology", "collection", "singles"]
    )


# --------------------------------------------------------------------
# Christmas detection
# --------------------------------------------------------------------

_CHRISTMAS_PATTERNS = [
    r"\bchristmas\b", r"\bxmas\b", r"\bx-mas\b", r"\bholiday",
    r"\bnoel\b", r"\bsanta\b", r"\bsleigh\b", r"\bjingle\b",
    r"\bsilent night\b", r"\bholy night\b", r"\bwinter wonderland\b",
    r"\bwhite christmas\b", r"\bjingle bells\b", r"\blast christmas\b",
    r"\byule\b", r"\byuletide\b", r"\badvent\b",
]


def detect_christmas_song(track_title: str, album_title: str) -> bool:
    """Detect if a song is Christmas-related based on track or album title.

    Args:
        track_title: Track title.
        album_title: Album title.

    Returns:
        True if detected as a Christmas song.
    """
    combined = f"{track_title or ''} {album_title or ''}".lower()
    return any(re.search(pattern, combined) for pattern in _CHRISTMAS_PATTERNS)


# --------------------------------------------------------------------
# Enhanced live-album detection
# --------------------------------------------------------------------

# More specific than LIVE_ALBUM_PATTERNS above — requires "live" to be
# in a format-tag position rather than anywhere in the title.
_LIVE_ALBUM_ENHANCED_PATTERNS = [
    r"\(live\)\s*$",
    r"\[live\]\s*$",
    r"-\s*live\s*$",
    r",\s*live\s*$",
    r"\+\s*live\s*$",
    r"\s+live\s*$",
    r"\s+live\s*[\)\]]\s*$",
    r"live\s+at\b",
    r"live\s+in\b",
    r"live\s+from\b",
    r"live\s+session",
    r"live\s+recording",
    r"live\s+tour\b",
    r"\bin\s+concert\b",
]


def is_live_album_enhanced(album_title: str) -> bool:
    """Enhanced live-album detection — only matches unambiguous format tags.

    Unlike ``is_live_or_alternate_album`` which catches any occurrence of
    "live" (including false positives like ``"(how to live) AS GHOSTS"``),
    this only matches when "live" appears in a format-tag position.

    Args:
        album_title: Album title to analyse.

    Returns:
        True when the title unambiguously indicates a live album.
    """
    if not album_title:
        return False
    text = album_title.lower()
    return any(re.search(pattern, text) for pattern in _LIVE_ALBUM_ENHANCED_PATTERNS)