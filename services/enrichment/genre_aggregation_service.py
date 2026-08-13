"""Genre Aggregation and Normalization Service

This module handles genre collection, normalization, and aggregation from multiple
music metadata sources (MusicBrainz, Discogs, AudioDB, Last.fm).

Key Responsibilities:
    - Genre name normalization and synonym resolution
    - Conflict detection and removal (e.g., "electronic" vs "punk")
    - Weighted aggregation from multiple sources
    - Top-N genre selection based on source authority
    
Genre Source Weights:
    Genres are weighted by source authority:
    
    - MusicBrainz: 0.40 (most authoritative, community-curated)
    - Discogs: 0.25 (comprehensive, user-submitted)
    - AudioDB: 0.20 (curated database)
    - Last.fm: 0.10 (user tags, can be noisy)
    
    Note: These weights are configurable via config.yaml using
          helpers.config_helpers.get_genre_weights()

Normalization Rules:
    - Case-insensitive comparison (all converted to lowercase)
    - Synonym mapping (e.g., "hip hop" → "hip-hop", "r&b" → "rnb")
    - Whitespace trimming
    
Conflict Resolution:
    The service detects and removes conflicting genre pairs:
    - "electronic" is removed if "punk" or "metal" are present
    - This prevents overly broad genres from diluting specific ones

Usage:
    >>> from services.enrichment.genre_aggregation_service import aggregate_genres
    >>> source_map = {
    ...     "musicbrainz": ["rock", "alternative"],
    ...     "discogs": ["alternative rock", "indie"],
    ...     "lastfm": ["rock", "post-punk"]
    ... }
    >>> top_genres = aggregate_genres(source_map)
    >>> print(top_genres)  # ['alternative', 'rock', 'indie', 'post-punk']

Architecture:
    Pure function-based design with no external dependencies.
    Called by: Album/artist enrichment services, genre display routes
    Should use: helpers.config_helpers.get_genre_weights(), get_genre_synonyms()
"""
from __future__ import annotations
import re
from db.engine import db_session
from collections import defaultdict
from sqlalchemy import text
import logging


logger = logging.getLogger(__name__)

# Import centralized configuration getters
from helpers.config_helpers import get_genre_weights, get_genre_synonyms

# Load configuration at module initialization
GENRE_WEIGHTS = get_genre_weights()
GENRE_SYNONYMS = get_genre_synonyms()

_CHRISTMAS_KEYWORDS = [
    "christmas", "xmas", "yuletide", "jingle bells", "silent night",
    "deck the halls", "winter wonderland", "feliz navidad",
    "rudolph", "santa claus", "sleigh bells", "noel", "hanukkah",
]
_LIVE_PATTERNS = [
    r"\blive\b", r"\bunplugged\b", r"\bconcert\b",
    r"\bat\s+\w+\s+(arena|stadium|hall|club|theatre|theater)",
]
_SPECIFIC_TO_GENERIC: dict[str, list[str]] = {
    "progressive metal": ["metal", "heavy metal"],
    "death metal": ["metal", "heavy metal"],
    "black metal": ["metal", "heavy metal"],
    "doom metal": ["metal", "heavy metal"],
    "power metal": ["metal", "heavy metal"],
    "symphonic metal": ["metal", "heavy metal"],
    "folk metal": ["metal", "heavy metal"],
    "nu metal": ["metal", "heavy metal"],
    "metalcore": ["metal", "heavy metal"],
    "post-punk": ["punk", "rock"],
    "hardcore punk": ["punk"],
    "electronic rock": ["electronic", "rock"],
    "indie rock": ["rock", "alternative"],
    "alternative rock": ["rock", "alternative"],
}


def normalize_genre(genre):
    value = str(genre or "").lower().strip()
    return GENRE_SYNONYMS.get(value, value)


def clean_conflicting_genres(genres):
    """Remove conflicting/broad genres when more specific ones exist."""
    cleaned = []
    lowered = {normalize_genre(g) for g in genres or []}

    for genre in lowered:
        if not genre:
            continue
        removed = False
        for specific, generics in _SPECIFIC_TO_GENERIC.items():
            if genre in generics and specific in lowered:
                removed = True
                break
        if genre == "electronic" and ("punk" in lowered or "metal" in lowered):
            continue
        if not removed:
            cleaned.append(genre)
    return sorted(cleaned)


def aggregate_genres(source_map, max_genres: int = 5, context_title: str = "", context_album: str = ""):
    """Aggregate genres from multiple sources with weighted scoring and context boosts."""
    scores = defaultdict(float)
    for source, genres in source_map.items():
        weight = GENRE_WEIGHTS.get(source, 0.05)
        for g in genres:
            scores[normalize_genre(g)] += weight

    context_lower = f"{context_title} {context_album}".lower()
    if any(kw in context_lower for kw in _CHRISTMAS_KEYWORDS):
        scores["christmas"] += 2.0
    if any(re.search(p, context_lower) for p in _LIVE_PATTERNS):
        scores["live"] += 0.5

    ordered = sorted(scores, key=scores.get, reverse=True)
    return clean_conflicting_genres(ordered)[:max_genres]


def get_top_genres_with_navidrome(sources, nav_genres, title="", album=""):
    """Combine online-sourced genres with Navidrome genres."""
    scores = defaultdict(float)
    for source, genres in (sources or {}).items():
        weight = GENRE_WEIGHTS.get(source, 0.05)
        for genre in genres:
            scores[normalize_genre(genre)] += weight

    context_lower = f"{title} {album}".lower()
    if any(kw in context_lower for kw in _CHRISTMAS_KEYWORDS):
        scores["christmas"] += 2.0
    if any(re.search(p, context_lower) for p in _LIVE_PATTERNS):
        scores["live"] += 0.5

    ordered = sorted(scores, key=scores.get, reverse=True)
    online_top = clean_conflicting_genres(ordered)[:3]
    nav_cleaned = sorted({normalize_genre(g).capitalize() for g in nav_genres if g})
    return online_top, nav_cleaned

    for genre in nav_genres or []:
        scores[normalize_genre(genre)] += 0.30

    ordered = sorted(scores, key=scores.get, reverse=True)
    return clean_conflicting_genres(ordered)[:5]


def get_track_recommendations(artist: str, album: str) -> dict:
    """Get genre recommendations for all tracks in an album by aggregating DB sources."""
    with db_session() as session:
        result = session.execute(
            text("""SELECT lastfm_tags, musicbrainz_genres, discogs_genres
               FROM tracks WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist AND album = :album"""),
            {"artist": artist, "album": album},
        )
        rows = result.fetchall()

    source_map: dict[str, list[str]] = {}
    for row in rows:
        for idx, (src_key, col) in enumerate([
            ("lastfm", "lastfm_tags"),
            ("musicbrainz", "musicbrainz_genres"), ("discogs", "discogs_genres"),
        ]):
            val = row[idx]
            if val:
                source_map.setdefault(src_key, []).extend(val if isinstance(val, list) else [val])

    recommended = aggregate_genres(source_map)
    return {"success": True, "artist": artist, "album": album, "genres": recommended}


def adjust_genres(genres: list[str], artist_is_metal: bool = False) -> list[str]:
    """Adjust genres based on artist context (metal vs non-metal).

    When an artist is metal-dominant, rock sub-genres are converted to
    their metal equivalents. Generic 'Metal' / 'Heavy Metal' are removed
    when more specific sub-genres exist.

    Args:
        genres: List of genre names.
        artist_is_metal: Whether the artist is classified as metal.

    Returns:
        Adjusted, deduplicated genre list.
    """
    adjusted = []
    for g in genres:
        g_lower = g.lower()
        if artist_is_metal:
            if g_lower in ("prog rock", "progressive rock"):
                adjusted.append("Progressive metal")
            elif g_lower == "folk rock":
                adjusted.append("Folk metal")
            elif g_lower == "goth rock":
                adjusted.append("Gothic metal")
            else:
                adjusted.append(g)
        else:
            adjusted.append(g)

    # Remove generic 'metal' if specific sub-genres exist
    metal_subgenres = [x for x in adjusted if "metal" in x.lower() and x.lower() != "metal"]
    if metal_subgenres:
        adjusted = [x for x in adjusted if x.lower() not in ("metal", "heavy metal")]

    return list(dict.fromkeys(adjusted))  # Deduplicate preserving order


def enrich_genres_aggressively(artist_name: str, conn=None, verbose: bool = False) -> set[str]:
    """Collect genres from all available external sources for an artist.

    Queries Discogs, AudioDB, and MusicBrainz for genre information and
    stores the results in the database for later use.

    Args:
        artist_name: Name of the artist to enrich.
        conn: Optional database connection for persistence.
        verbose: Enable verbose logging.

    Returns:
        Set of collected genre names (lowercased).
    """
    from sqlalchemy import text as _text
    from db.engine import db_session as _db_session

    genres_collected: set[str] = set()

    # Collect from Discogs
    try:
        from services.enrichment.discogs_service import get_discogs_genres
        discogs_genres = get_discogs_genres(artist_name, "")
        if discogs_genres:
            genres_collected.update(g.lower() for g in discogs_genres)
            if verbose:
                logger.info("Discogs genres for %s: %s", artist_name, discogs_genres)
    except Exception as e:
        logger.debug("Discogs genre lookup failed for %s: %s", artist_name, e)

    # Collect from AudioDB
    try:
        from api_clients.audiodb import get_audiodb_genres
        audiodb_genres = get_audiodb_genres(artist_name)
        if audiodb_genres:
            genres_collected.update(g.lower() for g in audiodb_genres)
            if verbose:
                logger.info("AudioDB genres for %s: %s", artist_name, audiodb_genres)
    except Exception as e:
        logger.debug("AudioDB genre lookup failed for %s: %s", artist_name, e)

    # Collect from MusicBrainz
    try:
        from services.enrichment.musicbrainz_service import MusicBrainzService
        mb = MusicBrainzService(enabled=True)
        mb_genres = mb.get_genres(artist_name, "")
        if mb_genres:
            genres_collected.update(g.lower() for g in mb_genres)
            if verbose:
                logger.info("MusicBrainz genres for %s: %s", artist_name, mb_genres)
    except Exception as e:
        logger.debug("MusicBrainz genre lookup failed for %s: %s", artist_name, e)

    # Persist to database
    if genres_collected:
        from sqlalchemy import text as _text
        from db.engine import db_session as _db_session
        try:
            with _db_session() as session:
                result = session.execute(
                    _text(
                        "UPDATE tracks SET genres = :genres_str "
                        "WHERE artist = :artist_name AND (genres IS NULL OR genres = '')"
                    ),
                    {"genres_str": ", ".join(sorted(genres_collected)), "artist_name": artist_name},
                )
            if verbose:
                logger.info("Updated %s tracks for %s with %s genres", result.rowcount, artist_name, len(genres_collected))
        except Exception as e:
            logger.debug("Failed to update genres for %s: %s", artist_name, e)

    return genres_collected


def update_get_top_genres_with_navidrome(sources: dict, nav_genres: list, title: str = "", album: str = "") -> list[str]:
    """Enhanced version of get_top_genres_with_navidrome with live/Christmas detection.

    Extends the base implementation with contextual genre detection based on
    track/album titles (live recordings, Christmas music).

    Args:
        sources: Dict mapping source names to genre lists.
        nav_genres: List of Navidrome-provided genres.
        title: Track title for contextual detection.
        album: Album title for contextual detection.

    Returns:
        Top 5 ranked genre names.
    """
    scores: dict[str, float] = defaultdict(float)

    for source, genres in (sources or {}).items():
        weight = GENRE_WEIGHTS.get(source, 0.05)
        for genre in genres:
            scores[normalize_genre(genre)] += weight

    for genre in nav_genres or []:
        scores[normalize_genre(genre)] += 0.30

    # Contextual live detection
    if re.search(r"\blive\b", (title or "").lower()) or re.search(r"\blive\b", (album or "").lower()):
        scores["live"] += 0.5

    # Contextual Christmas detection
    if any(word in (title or "").lower() or word in (album or "").lower()
           for word in ["christmas", "xmas", "hanukkah", "holiday"]):
        scores["christmas"] += 0.5

    ordered = sorted(scores, key=scores.get, reverse=True)
    cleaned = clean_conflicting_genres(ordered)[:5]

    # Remove generic 'metal'/'heavy metal' if specific sub-genres exist
    metal_subgenres = [g for g in cleaned if "metal" in g.lower() and g.lower() != "heavy metal"]
    if metal_subgenres:
        cleaned = [g for g in cleaned if g.lower() not in ("metal", "heavy metal")]

    return cleaned[:5]
