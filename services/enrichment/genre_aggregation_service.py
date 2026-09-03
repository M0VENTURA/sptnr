"""Genre Aggregation and Normalization Service

This module handles genre collection, normalization, and aggregation from multiple
music metadata sources (MusicBrainz, Discogs, AudioDB, Last.fm).

Key Responsibilities:
    - Genre name normalization and synonym resolution
    - Conflict detection and removal (e.g., "electronic" vs "punk")
    - Weighted aggregation from multiple sources
    - Top-N genre selection based on source authority
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
    import re as _re
    text = value
    text = _re.sub(r"\([^)]*\)", "", text)
    text = _re.sub(r"[-–—/\\]+\s*(remaster|remastered|bonus track|bonus|edit|radio edit|album version|single version|reissue|remastered version)\s*$", "", text, flags=_re.IGNORECASE)
    return text.strip()


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


def clean_conflicting_genres(genres: list[Any]) -> list[str]:
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


def aggregate_genres(
    source_map: dict[str, list[str]], 
    max_genres: int = 5, 
    context_title: str = "", 
    context_album: str = ""
) -> list[str]:
    votes: dict[str, float] = defaultdict(float)
    spellings: dict[str, list[tuple[float, str]]] = defaultdict(list)
    
    for source, genres in (source_map or {}).items():
        weight = _source_weight(source)
        for g in genres:
            if is_junk_genre(g) or is_admin_genre(g):
                continue
            key = normalize_genre_for_vote(g)
            if not key:
                continue
            votes[key] += weight
            spellings[key].append((weight, normalize_genre(g)))

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
    display_names = [_resolve_display_name(k, spellings) for k in qualified]
    cleaned = clean_conflicting_genres(display_names)
    return cleaned[:max_genres]


def get_top_genres_with_navidrome(sources: dict[str, list[str]], nav_genres: list[str], title: str = "", album: str = "") -> tuple[list[str], list[str]]:
    votes: dict[str, float] = defaultdict(float)
    spellings: dict[str, list[tuple[float, str]]] = defaultdict(list)
    
    for source, genres in (sources or {}).items():
        weight = _source_weight(source)
        for genre in genres:
            if is_junk_genre(genre) or is_admin_genre(genre):
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
    
    online_top = clean_conflicting_genres(
        [_resolve_display_name(k, spellings) for k in qualified]
    )[:3]
    nav_cleaned = sorted({
        normalize_genre(g).capitalize()
        for g in nav_genres
        if g and not is_junk_genre(g) and not is_admin_genre(g)
    })
    return online_top, nav_cleaned


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

    metal_subgenres = [x for x in adjusted if "metal" in x.lower() and x.lower() != "metal"]
    if metal_subgenres:
        adjusted = [x for x in adjusted if x.lower() not in ("metal", "heavy metal")]

    return list(dict.fromkeys(adjusted))


def enrich_genres_aggressively(artist_name: str, conn: Any = None, verbose: bool = False) -> set[str]:
    genres_collected: set[str] = set()

    # Collect from Discogs
    try:
        from services.enrichment.discogs_service import get_discogs_genres
        discogs_genres = get_discogs_genres(artist_name, "")
        if discogs_genres:
            genres_collected.update(g.lower() for g in discogs_genres)
            if verbose:
                logger.info("Discogs genres found", artist=artist_name, count=len(discogs_genres))
    except Exception as e:
        logger.debug("Discogs genre lookup failed", artist=artist_name, error=str(e))

    # Collect from AudioDB
    try:
        from api_clients.audiodb import get_audiodb_genres
        audiodb_genres = get_audiodb_genres(artist_name)
        if audiodb_genres:
            genres_collected.update(g.lower() for g in audiodb_genres)
            if verbose:
                logger.info("AudioDB genres found", artist=artist_name, count=len(audiodb_genres))
    except Exception as e:
        logger.debug("AudioDB genre lookup failed", artist=artist_name, error=str(e))

    # Collect from MusicBrainz (✅ Using shared service singleton + 2-step lookup)
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
                mb_genres = [g for g in mb_genres if g]
                
                if mb_genres:
                    genres_collected.update(g.lower() for g in mb_genres)
                    if verbose:
                        logger.info("MusicBrainz genres found", artist=artist_name, count=len(mb_genres))
    except Exception as e:
        logger.debug("MusicBrainz genre lookup failed", artist=artist_name, error=str(e))

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


def update_get_top_genres_with_navidrome(sources: dict[str, list[str]], nav_genres: list[str], title: str = "", album: str = "") -> list[str]:
    votes: dict[str, float] = defaultdict(float)
    spellings: dict[str, list[tuple[float, str]]] = defaultdict(list)

    for source, genres in (sources or {}).items():
        weight = _source_weight(source)
        for genre in genres:
            if is_junk_genre(genre) or is_admin_genre(genre):
                continue
            key = normalize_genre_for_vote(genre)
            if not key:
                continue
            votes[key] += weight
            spellings[key].append((weight, normalize_genre(genre)))

    for genre in nav_genres or []:
        if is_junk_genre(genre) or is_admin_genre(genre):
            continue
        key = normalize_genre_for_vote(genre)
        if key:
            votes[key] += 0.30
            spellings[key].append((0.30, normalize_genre(genre)))

    if re.search(r"\blive\b", (title or "").lower()) or re.search(r"\blive\b", (album or "").lower()):
        votes["live"] += 0.5
        spellings["live"].append((0.5, "live"))

    if any(word in (title or "").lower() or word in (album or "").lower()
           for word in ["christmas", "xmas", "hanukkah", "holiday"]):
        votes["christmas"] += 0.5
        spellings["christmas"].append((0.5, "christmas"))

    min_weight = _genre_min_weight()
    qualified = [k for k, v in votes.items() if min_weight <= 0 or v >= min_weight]
    qualified.sort(key=votes.get, reverse=True)
    
    cleaned = clean_conflicting_genres(
        [_resolve_display_name(k, spellings) for k in qualified]
    )[:5]

    metal_subgenres = [g for g in cleaned if "metal" in g.lower() and g.lower() != "heavy metal"]
    if metal_subgenres:
        cleaned = [g for g in cleaned if g.lower() not in ("metal", "heavy metal")]

    return cleaned[:5]
