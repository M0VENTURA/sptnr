"""Genre Aggregation and Normalization Service

This module handles genre collection, normalization, and aggregation from multiple
music metadata sources (MusicBrainz, Discogs, AudioDB, Last.fm).

Key Responsibilities:
    - Genre name normalization and synonym resolution
    - Conflict detection and removal (e.g., "electronic" vs "punk")
    - Weighted aggregation from multiple sources
    - Top-N genre selection based on source authority

Ordering contract: every ranking function in this module returns genres in
descending order of vote weight (highest-confidence first). Callers that
slice the result to a Top-N (e.g. ``cleaned[:max_genres]``) depend on this —
re-sorting alphabetically anywhere in the pipeline would silently change
which genres survive the cut.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

import structlog
from sqlalchemy import text

from db.engine import db_session
from helpers.config_helpers import get_genre_weights, get_genre_synonyms

logger = structlog.get_logger(__name__)

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

# Curated "specific implies suppress these generics" relationships that
# are NOT simple substring/word-boundary matches of the generic label
# itself (e.g. "post-punk" doesn't literally contain the word "rock", and
# "metalcore" doesn't contain "metal" as a separate word, so neither can be
# caught by the generic ``_GENERIC_ROOTS`` word-boundary check below).
#
# Plain "<word> metal" subgenres (Thrash Metal, Death Metal, Viking Metal,
# Gothic Metal, ...) do NOT need an entry here — they're handled
# automatically by ``_suppress_generic_parents``/``_GENERIC_ROOTS``.
_SPECIFIC_TO_GENERIC: dict[str, list[str]] = {
    "metalcore": ["metal", "heavy metal"],
    "post-punk": ["punk", "rock"],
    "hardcore punk": ["punk"],
    "electronic rock": ["electronic", "rock"],
    "indie rock": ["rock", "alternative"],
    "alternative rock": ["rock", "alternative"],
}

# Generic "root" labels that should be dropped whenever a more specific
# subgenre of the same family is present (e.g. "Thrash Metal" makes the
# bare "Metal" / "Heavy Metal" labels redundant). Matching is done on a
# word-boundary basis (not a plain substring) so this doesn't misfire on
# unrelated words that merely contain the root as a substring (e.g.
# "metallic hardcore" does not contain the standalone word "metal").
_GENERIC_ROOTS: dict[str, frozenset[str]] = {
    "metal": frozenset({"metal", "heavy metal"}),
}
_GENERIC_ROOT_PATTERNS: dict[str, re.Pattern[str]] = {
    root: re.compile(rf"\b{re.escape(root)}\b") for root in _GENERIC_ROOTS
}


def normalize_genre(genre: Any) -> str:
    value = str(genre or "").lower().strip()
    return GENRE_SYNONYMS.get(value, value)


_ADMIN_GENRE_WORDS: frozenset[str] = frozenset({
    "cover", "covers", "tribute", "tributes", "tribute band",
    "live", "unplugged", "live album", "live recordings",
    "remix", "remixes", "remixed", "rework", "reworked", "mashup", "mashups",
    "demo", "demos", "mixtape", "mixtapes",
    "soundtrack", "soundtracks", "score", "scores", "original soundtrack",
    "karaoke", "instrumental", "instrumentals",
    "bootleg", "bootlegs", "unofficial", "promo", "promos", "sampler",
})


def _strip_admin_genre_markers(value: str) -> str:
    if not value:
        return ""
    text_value = value
    text_value = re.sub(r"\([^)]*\)", "", text_value)
    text_value = re.sub(
        r"[-–—/\\]+\s*(remaster|remastered|bonus track|bonus|edit|radio edit|album version|single version|reissue|remastered version)\s*$",
        "",
        text_value,
        flags=re.IGNORECASE,
    )
    return text_value.strip()


def is_admin_genre(genre: Any) -> bool:
    if not genre:
        return True
    value = str(genre).strip().lower()
    if not value:
        return True
    value = _strip_admin_genre_markers(value)
    if not value:
        return True
    if value in _ADMIN_GENRE_WORDS:
        return True
    return False


_JUNK_GENRE_WORDS: frozenset[str] = frozenset({
    "2010", "2011", "2012", "2013", "2014", "2015", "2016", "2017",
    "2018", "2019", "2020", "2021", "2022", "2023", "2024", "2025",
    "2000", "2001", "2002", "2003", "2004", "2005", "2006", "2007",
    "2008", "2009", "1990", "1995", "1980", "1970", "1960", "1950",
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
    re.compile(r"^\d{3,4}$"),
    re.compile(r"^\d+\s*'s$"),
    re.compile(r"^(19|20)\d{2}s?$"),
    re.compile(r"^[a-z]*\d+[a-z]*$"),
    re.compile(r"^(my|the|a|an)\b", re.IGNORECASE),
)


def is_junk_genre(genre: Any) -> bool:
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


def normalize_genre_for_vote(genre: Any) -> str:
    value = str(genre or "").lower().strip()
    value = GENRE_SYNONYMS.get(value, value)
    return re.sub(r"[^a-z0-9]+", "", value)


def _source_weight(source: str) -> float:
    try:
        from helpers.config_helpers import get_genre_weights
        return float(get_genre_weights().get(source, 0.05) or 0)
    except Exception:
        return float(GENRE_WEIGHTS.get(source, 0.05) or 0)


def _genre_min_weight() -> float:
    try:
        from helpers.config_helpers import get_config
        cfg = get_config() or {}
        return max(0.0, float(cfg.get("genres", {}).get("min_weight", 0.25) or 0.25))
    except Exception:
        return 0.25


def _suppress_generic_parents(genres: list[str]) -> list[str]:
    """Drop a bare generic label (e.g. "Metal", "Heavy Metal") when a more
    specific subgenre of the same family is present (e.g. "Thrash Metal").

    This covers any "<word> <root>" subgenre generically via a word-boundary
    match rather than a hardcoded list of specific subgenres, so newly
    encountered ones (Thrash Metal, Viking Metal, Sludge Metal, Groove
    Metal, ...) are handled without needing to be enumerated one by one.

    A genre only counts as a "specific" trigger if it is NOT itself one of
    the configured generic labels for that root. This is what prevents the
    previous bug where a track tagged only ["Metal", "Heavy Metal"] — with
    no real subgenre present — had BOTH labels wiped out, because one of
    the generic labels was mistakenly treated as a subgenre trigger for the
    other.
    """
    if not genres:
        return genres
    lowered = [g.lower() for g in genres]
    to_drop: set[str] = set()

    for root, generic_labels in _GENERIC_ROOTS.items():
        pattern = _GENERIC_ROOT_PATTERNS[root]
        has_specific_subgenre = any(
            pattern.search(g) and g not in generic_labels
            for g in lowered
        )
        if has_specific_subgenre:
            to_drop.update(generic_labels)

    if not to_drop:
        return genres
    return [g for g, low in zip(genres, lowered) if low not in to_drop]


def clean_conflicting_genres(genres: list[Any]) -> list[str]:
    """Remove genres that conflict with a more specific sibling already
    present, and fold bare generic labels into their more specific
    subgenres via ``_suppress_generic_parents``.

    Order is preserved (callers typically pass an already vote-ranked
    list) rather than being flattened into a set and re-sorted
    alphabetically — the previous alphabetical sort here silently
    discarded the ranking that ``aggregate_genres`` and friends rely on to
    pick the top N *highest-confidence* genres, not merely the first N
    alphabetically.
    """
    ordered_keys: list[str] = []
    seen: set[str] = set()
    for g in genres or []:
        norm = normalize_genre(g)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        ordered_keys.append(norm)

    lowered_set = set(ordered_keys)
    cleaned: list[str] = []
    for genre in ordered_keys:
        removed = False
        for specific, generics in _SPECIFIC_TO_GENERIC.items():
            if genre in generics and specific in lowered_set:
                removed = True
                break
        if genre == "electronic" and ("punk" in lowered_set or "metal" in lowered_set):
            continue
        if not removed:
            cleaned.append(genre)

    return _suppress_generic_parents(cleaned)


def _resolve_display_name(key: str, spellings: dict[str, list[tuple[float, str]]]) -> str:
    if key not in spellings:
        return key
    candidates = spellings[key]
    best = max(candidates, key=lambda s: s[0])

    def _top_weight(forms: list[str]) -> str:
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
    split = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", best[1]).lower()
    return split.strip() or best[1]


def _vote_genres(
    source_map: dict[str, list[str]] | None,
    *,
    extra_votes: dict[str, tuple[float, str]] | None = None,
) -> tuple[dict[str, float], dict[str, list[tuple[float, str]]]]:
    """Tally weighted genre votes and track original spellings per
    normalized vote-key. Shared core used by every public ranking function
    in this module so junk/admin filtering, weighting, and spelling
    resolution can't drift out of sync between them.

    ``extra_votes`` lets a caller add further (weight, display_spelling)
    contributions keyed by an already-normalized vote key — e.g. a
    title/album context boost ("live", "christmas"), or Navidrome's local
    genre tags folded in alongside the online sources at their own weight.
    """
    votes: dict[str, float] = defaultdict(float)
    spellings: dict[str, list[tuple[float, str]]] = defaultdict(list)

    for source, genres in (source_map or {}).items():
        weight = _source_weight(source)
        for genre in genres or []:
            if is_junk_genre(genre) or is_admin_genre(genre):
                continue
            key = normalize_genre_for_vote(genre)
            if not key:
                continue
            votes[key] += weight
            spellings[key].append((weight, normalize_genre(genre)))

    for key, (weight, spelling) in (extra_votes or {}).items():
        votes[key] += weight
        spellings[key].append((weight, spelling))

    return votes, spellings


def _context_boost_votes(context_title: str, context_album: str) -> dict[str, tuple[float, str]]:
    """Christmas/live keyword boosts derived from title+album text.

    Shared by every ranking entry point in this module so the keyword list
    and weighting can't quietly drift apart between call sites the way the
    old ``get_top_genres_with_navidrome`` / ``update_get_top_genres_with_navidrome``
    pair had (different Christmas keyword sets, different live-detection
    patterns, for what was meant to be the same signal).
    """
    context_lower = f"{context_title or ''} {context_album or ''}".lower()
    boosts: dict[str, tuple[float, str]] = {}
    if any(kw in context_lower for kw in _CHRISTMAS_KEYWORDS):
        boosts["christmas"] = (2.0, "christmas")
    if any(re.search(p, context_lower) for p in _LIVE_PATTERNS):
        boosts["live"] = (0.5, "live")
    return boosts


def _rank_genres(
    votes: dict[str, float],
    spellings: dict[str, list[tuple[float, str]]],
    *,
    max_genres: int,
) -> list[str]:
    min_weight = _genre_min_weight()
    qualified = [k for k, v in votes.items() if min_weight <= 0 or v >= min_weight]
    qualified.sort(key=votes.get, reverse=True)
    display_names = [_resolve_display_name(k, spellings) for k in qualified]
    cleaned = clean_conflicting_genres(display_names)
    return cleaned[:max_genres]


def aggregate_genres(
    source_map: dict[str, list[str]],
    max_genres: int = 5,
    context_title: str = "",
    context_album: str = "",
) -> list[str]:
    votes, spellings = _vote_genres(
        source_map,
        extra_votes=_context_boost_votes(context_title, context_album),
    )
    return _rank_genres(votes, spellings, max_genres=max_genres)


def get_top_genres_with_navidrome(
    sources: dict[str, list[str]],
    nav_genres: list[str],
    title: str = "",
    album: str = "",
) -> tuple[list[str], list[str]]:
    """Return (top online-source genres, cleaned Navidrome-local genres) as
    two separate lists — the online ranking here does NOT fold
    ``nav_genres`` into the same vote.

    For a single merged ranking that folds Navidrome's local tags into the
    same weighted vote as the online sources, use
    ``rank_genres_with_local_tags`` instead.
    """
    votes, spellings = _vote_genres(
        sources,
        extra_votes=_context_boost_votes(title, album),
    )
    online_top = _rank_genres(votes, spellings, max_genres=3)

    nav_cleaned = sorted({
        normalize_genre(g).capitalize()
        for g in (nav_genres or [])
        if g and not is_junk_genre(g) and not is_admin_genre(g)
    })
    return online_top, nav_cleaned


def rank_genres_with_local_tags(
    sources: dict[str, list[str]],
    nav_genres: list[str],
    title: str = "",
    album: str = "",
    *,
    nav_weight: float = 0.30,
    max_genres: int = 5,
) -> list[str]:
    """Weighted-vote genre ranking that folds Navidrome's local genre tags
    in alongside the online sources (Last.fm/MusicBrainz/Discogs) at
    ``nav_weight`` each, then applies the shared generic-parent suppression
    (e.g. drop bare "Metal" when a specific subgenre like "Thrash Metal" is
    present) before returning the top ``max_genres``.

    This is the canonical replacement for the old
    ``update_get_top_genres_with_navidrome``. That name is kept below as a
    thin backwards-compatible alias for any existing callers — it now
    simply delegates here, so it also picks up the ranking-order and
    generic-suppression fixes automatically instead of carrying its own
    separate (and previously buggy) copy of this logic.
    """
    extra_votes: dict[str, tuple[float, str]] = dict(_context_boost_votes(title, album))

    for genre in nav_genres or []:
        if is_junk_genre(genre) or is_admin_genre(genre):
            continue
        key = normalize_genre_for_vote(genre)
        if not key:
            continue
        existing_weight, _ = extra_votes.get(key, (0.0, normalize_genre(genre)))
        extra_votes[key] = (existing_weight + nav_weight, normalize_genre(genre))

    votes, spellings = _vote_genres(sources, extra_votes=extra_votes)
    return _rank_genres(votes, spellings, max_genres=max_genres)


def update_get_top_genres_with_navidrome(
    sources: dict[str, list[str]],
    nav_genres: list[str],
    title: str = "",
    album: str = "",
) -> list[str]:
    """Deprecated alias for :func:`rank_genres_with_local_tags`.

    Kept so any existing callers of this name keep working unchanged; new
    code should call ``rank_genres_with_local_tags`` directly.
    """
    return rank_genres_with_local_tags(sources, nav_genres, title=title, album=album)


def get_track_recommendations(artist: str, album: str) -> dict[str, Any]:
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
    """Remap certain genre labels to their metal-adjacent equivalents when
    the artist is known to be a metal act, then apply the shared
    generic-parent suppression so a bare "Metal"/"Heavy Metal" doesn't
    survive alongside a specific subgenre.

    Previously this suppression was a separate, ad hoc inline check
    (``"metal" in x.lower() and x.lower() != "metal"``) that had the same
    wipeout bug as the old ``update_get_top_genres_with_navidrome``: a
    track tagged only ["Metal", "Heavy Metal"] would have both labels
    dropped, because "Metal" itself satisfied "!= 'metal'"... er, the
    mirror-image check here (`!= "heavy metal"`) meant "Metal" alone was
    wrongly treated as a subgenre trigger. Delegating to
    ``_suppress_generic_parents`` fixes this the same way it was fixed
    everywhere else in this module.
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

    adjusted = _suppress_generic_parents(adjusted)
    return list(dict.fromkeys(adjusted))


def enrich_genres_aggressively(artist_name: str, conn: Any = None, verbose: bool = False) -> set[str]:
    """Collect genre tags for ``artist_name`` from Discogs, AudioDB,
    MusicBrainz, and Last.fm, filter out junk/administrative tags the same
    way the rest of this module does, and backfill any tracks that don't
    already have a ``genres`` value.

    Previously this function wrote genres straight to the database without
    running them through ``is_junk_genre``/``is_admin_genre`` at all, so
    tags like "live", "soundtrack", release years, or mood words could end
    up as a track's genre unfiltered. It also never queried Last.fm despite
    being documented (in this module's docstring) as one of the four
    sources — that gap is closed below.
    """
    genres_collected: set[str] = set()

    def _add_clean(raw_genres: list[str] | None, source: str) -> None:
        if not raw_genres:
            return
        kept = [
            g.lower() for g in raw_genres
            if g and not is_junk_genre(g) and not is_admin_genre(g)
        ]
        if kept:
            genres_collected.update(kept)
            if verbose:
                logger.info(f"{source} genres found", artist=artist_name, count=len(kept))

    # Collect from Discogs
    try:
        from services.enrichment.discogs_service import get_discogs_genres
        _add_clean(get_discogs_genres(artist_name, ""), "Discogs")
    except Exception as e:
        logger.debug("Discogs genre lookup failed", artist=artist_name, error=str(e))

    # Collect from AudioDB
    try:
        from api_clients.audiodb import get_audiodb_genres
        _add_clean(get_audiodb_genres(artist_name), "AudioDB")
    except Exception as e:
        logger.debug("AudioDB genre lookup failed", artist=artist_name, error=str(e))

    # Collect from MusicBrainz (shared service singleton + 2-step lookup)
    try:
        from services.enrichment.musicbrainz_service import get_shared_mb_client
        from api_clients.musicbrainz_http import escape_lucene_special_chars

        client = get_shared_mb_client()
        # Step 1: Resolve the artist MBID (Search API drops inc requests)
        query = f'artist:"{escape_lucene_special_chars(artist_name)}"'
        search_results = client.search_artists(query, limit=1)

        if search_results and search_results[0].get("id"):
            artist_mbid = search_results[0]["id"]
            # Step 2: Do a direct Lookup to get the genres array
            artist_data = client.get_artist(artist_mbid, inc="genres")
            if artist_data and artist_data.get("genres"):
                mb_genres = [str(g.get("name") or "").strip() for g in artist_data["genres"]]
                _add_clean([g for g in mb_genres if g], "MusicBrainz")
    except Exception as e:
        logger.debug("MusicBrainz genre lookup failed", artist=artist_name, error=str(e))

    # Collect from Last.fm
    try:
        from helpers.config_helpers import get_config
        lastfm_config = (get_config().get("api_integrations", {}) or {}).get("lastfm", {}) or {}
        api_key = str(lastfm_config.get("api_key") or "")
        if lastfm_config.get("enabled") and api_key not in {
            "", "your_lastfm_api_key", "YOUR_API_KEY", "<your_api_key>"
        }:
            from api_clients.lastfm import LastFmClient
            tags = LastFmClient(api_key).get_artist_top_tags(artist_name, limit=15) or []
            lastfm_genres = [
                str(tag.get("name") or "").strip()
                for tag in tags
                if isinstance(tag, dict)
            ]
            _add_clean([g for g in lastfm_genres if g], "Last.fm")
    except Exception as e:
        logger.debug("Last.fm genre lookup failed", artist=artist_name, error=str(e))

    # Persist to database
    if genres_collected:
        try:
            with db_session() as session:
                result = session.execute(
                    text(
                        "UPDATE tracks SET genres = :genres_str "
                        "WHERE artist = :artist_name AND (genres IS NULL OR genres = '')"
                    ),
                    {"genres_str": ", ".join(sorted(genres_collected)), "artist_name": artist_name},
                )
            if verbose:
                logger.info("Updated tracks with enriched genres", updated_rows=result.rowcount, artist=artist_name, genres_count=len(genres_collected))
        except Exception as e:
            logger.debug("Failed to update genres in DB", artist=artist_name, error=str(e))

    return genres_collected
