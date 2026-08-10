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
    # ``album_type_from_field`` (e.g. the MusicBrainz release type) is the
    # AUTHORITATIVE signal: when the album is matched to a known album type,
    # the type field ALONE decides.  A studio album whose title merely
    # contains "live"/"concert" text ("(how to live) as ghosts") must not be
    # flagged live, and an explicit "live" type is trusted even when the
    # title looks like a regular album.  Title-based heuristics only run as
    # a FALLBACK when the album has NO matched type (no MusicBrainz/Spotify
    # album-type match).
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


# Version-marker positions a bonus live/acoustic track uses.  Deliberately
# format-tag based (the same discipline ``is_live_album_enhanced`` applies to
# album titles): a bare ``\blive\b`` would mis-flag songs literally named
# "Live Fast, Die Young" or "Live In Colour", capping them at 4★.
_LIVE_ALTERNATE_TRACK_MARKERS = [
    # Trailing parenthetical version tags: "Song (Live)", "Song (Acoustic)",
    # "Song (Live In Tokyo 1994)", "Song (Unplugged)", "Song (Demo)".
    r"\((?:live|unplugged|acoustic|orchestral|demo)[^)]*\)",
    # Trailing "- Live" / "- Acoustic" separators.
    r"[-–—]\s*(?:live|unplugged|acoustic|orchestral|demo)\s*$",
]


def is_live_or_alternate_track_title(title: str) -> bool:
    """Return True when a track TITLE flags a live/acoustic alternate version.

    Matches version-marker positions only (a trailing ``(Live ...)`` /
    ``(Acoustic ...)`` / ``(Demo ...)`` parenthetical or a ``- Live``-style
    separator) — the markers a bonus live cut on a studio album carries.
    Used to give such a track live status (the live weight penalty on its
    popularity score and the 4★ cap on its star rating) without treating the
    whole album as live.
    """
    return any(
        re.search(pattern, title or "", re.IGNORECASE)
        for pattern in _LIVE_ALTERNATE_TRACK_MARKERS
    )


def is_bonus_track_title(title: str) -> bool:
    """Return True when a track TITLE indicates a bonus / alternate version.

    Title-only check matching ``ALT_TRACK_PATTERNS`` (live, unplugged,
    acoustic, orchestral, remix, demo, instrumental, karaoke).  Used to filter
    bonus tracks out of an album's average popularity scoring from STORED DB
    rows, where the album-context flags (``album_context_live``) are not
    persisted.  A studio album padded with extra live cuts is exactly the
    bonus-track case this targets — the album's core tracks should be scored
    against the album's core distribution, not the padded one.
    """
    return any(re.search(pattern, title or "", re.IGNORECASE) for pattern in ALT_TRACK_PATTERNS)


def should_exclude_track_from_stats(
    title: str,
    album: str = "",
    is_live: int = 0,
    album_context_live: int = 0,
    album_type: str = "",
) -> bool:

    if is_live or album_context_live:
        return True

    # When the album is matched to an authoritative album type (MusicBrainz/
    # Spotify), the type alone decides the live verdict — title text like
    # "(Live)" in an otherwise-studio album must not exclude its tracks.
    # Unmatched albums fall back to the title heuristic.
    if not (album_type or "").strip():
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


# Album-artist values that identify a release as a TRUE Various-Artists
# compilation — the per-track artist context is required for scoring/marking.
# Matches the "generic artist" set used across the scan pipeline (runner /
# album stage / finalise).
_VA_ALBUM_ARTIST_NAMES = frozenset(
    {
        "various artists", "various artists -", "various",
        "va", "v/a", "compilation", "soundtrack",
    }
)


def classify_compilation_category(
    artist: str,
    album: str,
    tracks: list,
    album_artist: str | None = None,
    spotify_album_type: str | None = None,
    musicbrainz_album_type: str | None = None,
) -> str:
    """Classify a compilation candidate as ``"va"``, ``"single_artist"`` or ``""``.

    MusicBrainz and Spotify both tag single-artist retrospectives ("Queen -
    Greatest Hits") AND true Various-Artists albums ("Now That's What I Call
    Music", movie soundtracks) with the ``compilation`` release type, but the
    two need OPPOSITE handling:

    - ``"va"``            — TRUE Various-Artists compilation: the album artist
      is a VA alias ("Various Artists" / "Soundtrack" / ...) or the tracklist
      spans >= 4 distinct artists that the album artist is not one of.  Every
      track has a different artist, so scoring and marking need per-track
      artist context and the singles z-gates are meaningless.
    - ``"single_artist"`` — release tagged ``compilation``/``soundtrack`` but
      the album artist is a single real artist (Greatest Hits / anthology).
      Treated like a standard studio album: album-relative scoring, the
      artist's own ``artist_stats`` and the normal singles gates apply.
    - ``""``              — not a compilation.

    ``detect_compilation_album`` keeps the original boolean view of this
    classification.
    """
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
    tracks: list,
    album_artist: str | None = None,
    spotify_album_type: str | None = None,
) -> bool:
    """Return True when the album is ANY kind of compilation (VA or single-artist).

    See ``classify_compilation_category`` for the VA vs single-artist split;
    this is the original boolean view kept for callers that only need the
    yes/no verdict.
    """
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