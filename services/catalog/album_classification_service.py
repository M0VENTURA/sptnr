"""Album and track classification helpers for scan/stat eligibility."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

import structlog

from helpers.normalization_service import normalize_title_for_lookup

logger = structlog.get_logger(__name__)


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
    r"\bjam[- ]along\b",
    r"\balternate\b",
    r"\balt\.\b",
    r"\b(?:single|radio|album)\s+edit\b",
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


def classify_album_type(album_row: dict[str, Any]) -> str:
    """Classify an album into the artist-page discography buckets."""
    raw_type = str(
        album_row.get("musicbrainz_albumtype")
        or album_row.get("spotify_album_type")
        or album_row.get("album_type")
        or ""
    ).lower()

    album_name = str(album_row.get("album") or "").lower()

    if "compilation" in raw_type:
        return "compilation"
    if "soundtrack" in raw_type or "soundtrack" in album_name:
        return "compilation"

    if "live" in raw_type or re.search(r"\blive\b", album_name) or "unplugged" in album_name:
        return "live_album"

    if "remix" in raw_type or "remix" in album_name:
        return "remix_album"

    if "ep" in raw_type:
        return "ep"

    if "single" in raw_type:
        return "single"

    return "album"


def is_live_or_alternate_album(album: str) -> bool:
    return any(re.search(pattern, album or "", re.IGNORECASE) for pattern in LIVE_ALBUM_PATTERNS)


def detect_live_album_type(album: str, album_type_from_field: str = "") -> str:
    field_text = (album_type_from_field or "").lower()
    if field_text:
        if "acoustic" in field_text or "unplugged" in field_text:
            return "acoustic"
        if "live" in field_text:
            return "live"
        return ""

    title = album or ""
    title_text = title.lower()
    if "acoustic" in title_text or "unplugged" in title_text:
        return "acoustic"
        
    if is_live_album_enhanced(title):
        return "live"

    return ""


# --------------------------------------------------------------------
# Track-level classification
# --------------------------------------------------------------------

def is_live_or_unplugged_track_title(title: str) -> bool:
    return any(re.search(p, title or "", re.IGNORECASE) for p in [r"\blive\b", r"\bunplugged\b"])


_LIVE_ALTERNATE_TRACK_MARKERS = [
    r"\((?:live|unplugged|acoustic|orchestral|demo|jam[- ]along|alternate)[^)]*\)",
    r"[-–—]\s*(?:live|unplugged|acoustic|orchestral|demo|jam[- ]along|alternate)\s*$",
]


def is_live_or_alternate_track_title(title: str) -> bool:
    """Return True when a track TITLE flags a live/acoustic alternate version."""
    return any(
        re.search(pattern, title or "", re.IGNORECASE)
        for pattern in _LIVE_ALTERNATE_TRACK_MARKERS
    )


_INSTRUMENTAL_TITLE_RE = re.compile(r"\binstrumental\b", re.IGNORECASE)


def is_instrumental_track_title(title: str) -> bool:
    """Return True when a track TITLE flags an instrumental version."""
    return bool(_INSTRUMENTAL_TITLE_RE.search(title or ""))


def is_bonus_track_title(title: str) -> bool:
    """Return True when a track TITLE indicates a bonus / alternate version."""
    return any(re.search(pattern, title or "", re.IGNORECASE) for pattern in ALT_TRACK_PATTERNS)


def should_exclude_track_from_stats(
    title: str,
    album: str = "",
    is_live: int = 0,
    album_context_live: int = 0,
    album_type: str = "",
    duration: float | None = None,
    exclude_below_seconds: float | None = None,
    exclude_title_regex: str | None = None,
) -> bool:
    """True when a track must not anchor the album/artist stats baseline."""
    if is_live or album_context_live:
        return True

    if duration is not None:
        try:
            if exclude_below_seconds is None:
                from services.popularity.popularity_config import (
                    get_exclude_from_median_below_seconds,
                )
                exclude_below_seconds = get_exclude_from_median_below_seconds()
            if float(exclude_below_seconds) > 0 and float(duration) < float(exclude_below_seconds):
                return True
        except Exception:
            pass

    if title:
        try:
            if exclude_title_regex is None:
                from services.popularity.popularity_config import (
                    get_exclude_title_regex,
                )
                exclude_title_regex = get_exclude_title_regex()
            if exclude_title_regex:
                if re.search(exclude_title_regex, title, re.IGNORECASE):
                    return True
        except Exception:
            pass

    if not (album_type or "").strip():
        if is_live_or_alternate_album(album):
            return True

    return any(re.search(pattern, title or "", re.IGNORECASE) for pattern in ALT_TRACK_PATTERNS)


# --------------------------------------------------------------------
# Advanced helpers
# --------------------------------------------------------------------

def detect_alternate_takes(tracks: list[Any]) -> dict[str, list[Any]]:
    groups: dict[str, list[Any]] = defaultdict(list)

    for track in tracks or []:
        title = track.get("title") if isinstance(track, dict) else str(track)
        groups[normalize_title_for_lookup(title)].append(track)

    return {k: v for k, v in groups.items() if len(v) > 1}


_VA_ALBUM_ARTIST_NAMES = frozenset(
    {
        "various artists", "various artists -", "various",
        "va", "v/a", "compilation", "soundtrack",
    }
)


def classify_compilation_category(
    artist: str,
    album: str,
    tracks: list[Any],
    album_artist: str | None = None,
    spotify_album_type: str | None = None,
    musicbrainz_album_type: str | None = None,
) -> str:
    """Classify a compilation candidate as ``"va"``, ``"single_artist"`` or ``""``."""
    type_text = " ".join(
        filter(None, (spotify_album_type or "", musicbrainz_album_type or ""))
    ).lower()
    is_compilation_type_tag = any(token in type_text for token in COMPILATION_TYPES)

    if (album_artist or "").strip().lower() in _VA_ALBUM_ARTIST_NAMES:
        return "va"

    artists = {
        str(t.get("artist", "")).strip().lower()
        for t in tracks or []
        if isinstance(t, dict) and t.get("artist")
    }
    if len(artists) >= 4 and (artist or "").lower() not in artists:
        return "va"

    if is_compilation_type_tag:
        return "single_artist"

    return ""


def detect_compilation_album(
    artist: str,
    album: str,
    tracks: list[Any],
    album_artist: str | None = None,
    spotify_album_type: str | None = None,
) -> bool:
    """Return True when the album is ANY kind of compilation."""
    return (
        classify_compilation_category(
            artist=artist,
            album=album,
            tracks=tracks,
            album_artist=album_artist,
            spotify_album_type=spotify_album_type,
        )
        != ""
    )


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
    """Detect if a song is Christmas-related based on track or album title."""
    combined = f"{track_title or ''} {album_title or ''}".lower()
    return any(re.search(pattern, combined) for pattern in _CHRISTMAS_PATTERNS)


# --------------------------------------------------------------------
# Enhanced live-album detection
# --------------------------------------------------------------------

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
    """Enhanced live-album detection — only matches unambiguous format tags."""
    if not album_title:
        return False
    text = album_title.lower()
    return any(re.search(pattern, text) for pattern in _LIVE_ALBUM_ENHANCED_PATTERNS)
