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


# Junk-genre blacklist.
#
# Last.fm top-tags are raw crowdsourced labels: users tag songs with release
# years ("2014", "2015"), moods ("beautiful", "romantic"), arbitrary words
# ("favourite", "love") and non-genre noise ("seen live").  With ``genres``
# feeding the ``{Genre} - Top Tracks`` playlist writer, a "2014" or
# "beautiful" tag that survives aggregation would generate literal
# ``2014 - Top Tracks.m3u`` / ``beautiful - Top Tracks.m3u`` files.
# These labels are blocked BEFORE voting so they can never reach a playlist.
_JUNK_GENRE_WORDS: frozenset[str] = frozenset({
    # years / numeric
    "2010", "2011", "2012", "2013", "2014", "2015", "2016", "2017",
    "2018", "2019", "2020", "2021", "2022", "2023", "2024", "2025",
    "2000", "2001", "2002", "2003", "2004", "2005", "2006", "2007",
    "2008", "2009", "1990", "1995", "1980", "1970", "1960", "1950",
    # non-genre moods / adjectives
    "beautiful", "romantic", "sad", "happy", "fun", "funny", "awesome",
    "amazing", "great", "best", "favourite", "favorite", "love", "loved",
    "loving", "beauty", "sexy", "cool", "epic", "brilliant", "good",
    "nice", "perfect", "wonderful", "powerful", "emotional", "feelings",
    "feeling", "guitar", "guitars", "singer", "voice", "vocals", "vocal",
    "drums", "bass", "songs", "song", "music", "album", "band", "artist",
    "seen live", "seen live in", "live seen", "classic", "oldies",
    "underrated", "overrated", "worship", "night", "summer", "winter",
    "party", "danceable", "melancholy", "mood", "moody", "relaxing",
    "chill", "chillout", "ambient chill", "study", "sleep", "workout",
    "work", "driving", "running", "gym", "morning", "evening", "nighttime",
})
_JUNK_GENRE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\d{3,4}$"),          # bare year / numeric
    re.compile(r"^\d+\s*'s$"),         # "80s", "90s"
    re.compile(r"^(19|20)\d{2}s?$"),   # "1980s", "2010s"
    re.compile(r"^[a-z]*\d+[a-z]*$"),  # "3d", "80s", "r2b2" — any embedded digit
    re.compile(r"^(my|the|a|an)\b", re.IGNORECASE),  # "my playlist", "the best"
)


def is_junk_genre(genre) -> bool:
    """True when a raw tag is not a real genre (year, mood, noise).

    Used to filter crowdsourced tags (Last.fm top-tags are raw user labels)
    BEFORE the aggregation vote so junk can never surface in the ``genres``
    column or a ``{Genre} - Top Tracks`` playlist name.  A value like
    ``"2014"`` or ``"beautiful"`` would otherwise become a literal playlist.
    Config ``genres.junk_filter`` (default True) can disable the filter.
    """
    if not genre:
        return True
    try:
        from helpers.config_helpers import get_config
        _cfg = get_config() or {}
        if not bool(_cfg.get("genres", {}).get("junk_filter", True)):
            return False
    except Exception:
        pass
    value = str(genre).strip().lower()
    if not value:
        return True
    if value in _JUNK_GENRE_WORDS:
        return True
    if any(p.search(value) for p in _JUNK_GENRE_PATTERNS):
        return True
    return False


def normalize_genre_for_vote(genre) -> str:
    """Canonicalise a genre label for SPLIT-VOTE stacking.

    MusicBrainz returns ``"nu metal"``, Last.fm ``"nu-metal"`` and Discogs
    ``"NuMetal"`` for the same genre — treated as three separate labels, none
    of them accumulates enough weight to pass the consensus threshold and a
    genuinely heavy track falls out of its genre playlist.  Before the vote,
    each label is lowercased, stripped of hyphens/punctuation and whitespace
    so all three forms stack onto one canonical key.  The DISPLAY name keeps
    the most authoritative (highest-weight source's) original spelling.
    """
    value = str(genre or "").lower().strip()
    # Keep the configured synonyms applied FIRST (canonical label), then
    # collapse separators for vote stacking.
    value = GENRE_SYNONYMS.get(value, value)
    return re.sub(r"[^a-z0-9]+", "", value)


def _source_weight(source: str) -> float:
    """Return the live configured weight for a genre source.

    Reads ``genres.weights`` from config at call time so UI weight edits
    apply immediately (the module-level ``GENRE_WEIGHTS`` constant is a
    frozen import-time snapshot used only as fallback).
    """
    try:
        from helpers.config_helpers import get_genre_weights
        return float(get_genre_weights().get(source, 0.05) or 0)
    except Exception:
        return float(GENRE_WEIGHTS.get(source, 0.05) or 0)


def _genre_min_weight() -> float:
    """Consensus threshold below which a genre is discarded.

    Config ``genres.min_weight`` (default 0.25).  A single Last.fm tag earns
    only 0.10 — without a second source backing it, it can never clear 0.25,
    so one-off crowdsourced labels ("K-pop" tagged by a single user) are
    filtered out while a label confirmed by Last.fm (0.10) + Essentia (0.20)
    passes, and a lone Discogs genre (0.25) still passes on its own.
    Returns 0.0 when the config disables the gate.
    """
    try:
        from helpers.config_helpers import get_config
        cfg = get_config() or {}
        return max(0.0, float(cfg.get("genres", {}).get("min_weight", 0.25) or 0.25))
    except Exception:
        return 0.25


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


def _resolve_display_name(key: str, spellings: dict[str, list[tuple[float, str]]]) -> str:
    """Pick the readable display name for a merged vote key.

    ``key`` is the hyphen/space-stripped vote key (``"numetal"``); each
    spelling is ``(source_weight, normalized_spelling)``.  Display-name
    preference: (1) a space-separated spelling ("nu metal"), (2) a
    hyphenated spelling ("nu-metal"), (3) CamelCase-split of the key
    ("NuMetal" → "nu metal").  Ties break toward the higher-weight source,
    then the longest spelling.
    """
    if key not in spellings:
        return key
    candidates = spellings[key]
    best = max(candidates, key=lambda s: s[0])

    def _top_weight(forms: list[str]) -> str:
        # Among the given readable forms, prefer the highest-weight source's
        # spelling; ties break toward the longest (most descriptive).
        form_weight = {
            s[1]: s[0] for s in candidates if s[1] in forms
        }
        return sorted(forms, key=lambda f: (form_weight.get(f, 0.0), len(f)))[-1]

    spaced = [s[1] for s in candidates if " " in s[1]]
    if spaced:
        return _top_weight(spaced)
    hyphenated = [s[1] for s in candidates if "-" in s[1]]
    if hyphenated:
        return _top_weight(hyphenated)
    if " " in best[1]:
        return best[1]
    # Every spelling was stripped ("NuMetal") — split CamelCase back.
    split = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", best[1]).lower()
    return split.strip() or best[1]


def aggregate_genres(source_map, max_genres: int = 5, context_title: str = "", context_album: str = ""):
    """Aggregate genres from multiple sources with weighted scoring and context boosts.

    Consensus model: every raw tag is (1) junk-filtered, (2) split-vote
    normalised (``nu metal``/``nu-metal``/``NuMetal`` stack onto one key),
    (3) weighted by source authority, and (4) gated by
    ``genres.min_weight`` — a genre needs a SECOND source to back a lone
    low-weight tag, otherwise it is discarded.  The most authoritative
    readable spelling wins for display.
    """
    votes: dict[str, float] = defaultdict(float)
    spellings: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for source, genres in (source_map or {}).items():
        weight = _source_weight(source)
        for g in genres:
            if is_junk_genre(g):
                continue
            key = normalize_genre_for_vote(g)
            if not key:
                continue
            votes[key] += weight
            spellings[key].append((weight, normalize_genre(g)))

    # Single-source context boosts (Christmas / Live) bypass the threshold —
    # they are deterministic title/album detections, not crowdsourced votes.
    context_lower = f"{context_title} {context_album}".lower()
    if any(kw in context_lower for kw in _CHRISTMAS_KEYWORDS):
        votes["christmas"] += 2.0
        spellings["christmas"].append((1.0, "christmas"))
    if any(re.search(p, context_lower) for p in _LIVE_PATTERNS):
        votes["live"] += 0.5
        spellings["live"].append((0.5, "live"))

    min_weight = _genre_min_weight()
    qualified = [k for k, v in votes.items() if min_weight <= 0 or v >= min_weight]
    qualified.sort(key=votes.get, reverse=True)
    # Map back to DISPLAY names before conflict-cleaning — the
    # ``_SPECIFIC_TO_GENERIC`` rules match space-separated names, not the
    # hyphen-stripped vote keys.
    display_names = [_resolve_display_name(k, spellings) for k in qualified]
    cleaned = clean_conflicting_genres(display_names)
    return cleaned[:max_genres]


def get_top_genres_with_navidrome(sources, nav_genres, title="", album=""):
    """Combine online-sourced genres with Navidrome genres.

    Applies the same consensus model as ``aggregate_genres`` (junk filter +
    split-vote stacking + ``genres.min_weight`` gate) to the online sources,
    then appends the Navidrome-provided genres as-is.  Returns
    ``(online_top, nav_cleaned)``.
    """
    votes: dict[str, float] = defaultdict(float)
    spellings: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for source, genres in (sources or {}).items():
        weight = _source_weight(source)
        for genre in genres:
            if is_junk_genre(genre):
                continue
            key = normalize_genre_for_vote(genre)
            if not key:
                continue
            votes[key] += weight
            spellings[key].append((weight, normalize_genre(genre)))

    context_lower = f"{title} {album}".lower()
    if any(kw in context_lower for kw in _CHRISTMAS_KEYWORDS):
        votes["christmas"] += 2.0
        spellings["christmas"].append((1.0, "christmas"))
    if any(re.search(p, context_lower) for p in _LIVE_PATTERNS):
        votes["live"] += 0.5
        spellings["live"].append((0.5, "live"))

    min_weight = _genre_min_weight()
    qualified = [k for k, v in votes.items() if min_weight <= 0 or v >= min_weight]
    qualified.sort(key=votes.get, reverse=True)
    # Map back to DISPLAY names before conflict-cleaning (see
    # ``aggregate_genres`` — the rules match space-separated names).
    online_top = clean_conflicting_genres(
        [_resolve_display_name(k, spellings) for k in qualified]
    )[:3]
    nav_cleaned = sorted({normalize_genre(g).capitalize() for g in nav_genres if g and not is_junk_genre(g)})
    return online_top, nav_cleaned


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
    votes: dict[str, float] = defaultdict(float)
    spellings: dict[str, list[tuple[float, str]]] = defaultdict(list)

    for source, genres in (sources or {}).items():
        weight = _source_weight(source)
        for genre in genres:
            if is_junk_genre(genre):
                continue
            key = normalize_genre_for_vote(genre)
            if not key:
                continue
            votes[key] += weight
            spellings[key].append((weight, normalize_genre(genre)))

    # Navidrome genres are authoritative local tags — they always vote.
    for genre in nav_genres or []:
        if is_junk_genre(genre):
            continue
        key = normalize_genre_for_vote(genre)
        if key:
            votes[key] += 0.30
            spellings[key].append((0.30, normalize_genre(genre)))

    # Contextual live detection
    if re.search(r"\blive\b", (title or "").lower()) or re.search(r"\blive\b", (album or "").lower()):
        votes["live"] += 0.5
        spellings["live"].append((0.5, "live"))

    # Contextual Christmas detection
    if any(word in (title or "").lower() or word in (album or "").lower()
           for word in ["christmas", "xmas", "hanukkah", "holiday"]):
        votes["christmas"] += 0.5
        spellings["christmas"].append((0.5, "christmas"))

    min_weight = _genre_min_weight()
    qualified = [k for k, v in votes.items() if min_weight <= 0 or v >= min_weight]
    qualified.sort(key=votes.get, reverse=True)
    # Map back to DISPLAY names before conflict-cleaning (see
    # ``aggregate_genres`` — the rules match space-separated names).
    cleaned = clean_conflicting_genres(
        [_resolve_display_name(k, spellings) for k in qualified]
    )[:5]

    # Remove generic 'metal'/'heavy metal' if specific sub-genres exist
    metal_subgenres = [g for g in cleaned if "metal" in g.lower() and g.lower() != "heavy metal"]
    if metal_subgenres:
        cleaned = [g for g in cleaned if g.lower() not in ("metal", "heavy metal")]

    return cleaned[:5]
