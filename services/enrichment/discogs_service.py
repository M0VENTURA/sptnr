"""Discogs enrichment service."""

from __future__ import annotations

import re
import threading
from difflib import SequenceMatcher
from typing import Any, TypedDict

import structlog

try:  # Optional C-speed fuzzy matching
    from rapidfuzz import fuzz as _rapidfuzz_fuzz
except ImportError:
    _rapidfuzz_fuzz = None

from api_clients.discogs_http import DiscogsHttpClient
from helpers.normalization_service import (
    normalize_title_for_lookup,
    strip_parentheses,
    strip_featured_artist,
    strip_featured_guest_suffix,
    clean_discogs_biography,
    edition_annotations_compatible,
)

logger = structlog.get_logger(__name__)

# --- CONSTANTS ---
MIN_DISCOGS_SIMILARITY = 0.75
DISCOGS_BASE_WEIGHT = 0.85
DISCOGS_FULL_CONFIDENCE = 0.85

ALBUM_FORMAT_TOKENS = frozenset({"album", "lp", "compilation", "mixtape"})
SINGLE_FORMAT_TOKENS = frozenset({"single", "ep", "maxi", "maxi-single"})
MAX_SINGLE_TRACKS = 6
# Cap master-format resolutions per artist fetch (see resolve_master_formats).
_MAX_MASTER_FORMAT_RESOLUTIONS = 15


def calculate_discogs_confidence(
    title: str, 
    similarity_ratio: float,
    artist_verified: bool
) -> dict[str, Any]:
    """Dynamic Discogs match confidence."""
    sim = float(similarity_ratio or 0.0)
    if sim < MIN_DISCOGS_SIMILARITY:
        return {"matched": False, "confidence": 0.0,
                "metadata": {"similarity_ratio": round(sim, 2)}}

    confidence = DISCOGS_BASE_WEIGHT * sim

    if not artist_verified:
        confidence *= 0.50

    if len(str(title or "").split()) <= 2 and sim < 0.95:
        confidence *= 0.60

    final = round(max(0.0, min(1.0, confidence)), 2)
    return {
        "matched": final >= DISCOGS_FULL_CONFIDENCE,
        "confidence": final,
        "metadata": {"similarity_ratio": round(sim, 2)},
    }


DISCOGS_NOISE_SUFFIX_RE = re.compile(
    r"\s*[-–—]\s*(?:the\s+)?"
    r"(?:single|ep|promo|radio\s+edit|edit|explicit|clean|remaster(?:ed)?|mono|stereo|album\s+version)"
    r"\s*$",
    re.IGNORECASE,
)


def _clean_title_for_comparison(title: str) -> str:
    if not title:
        return ""
    value = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", title)
    value = DISCOGS_NOISE_SUFFIX_RE.sub("", value)
    return normalize_title_for_lookup(value)


INVERTED_RETRY_MIN_SIMILARITY = 0.50


def release_format_key(formats: Any) -> str:
    """Normalize a Discogs ``format`` value (str or list) to a token string.

    Public alias of the former private ``_release_format_key`` so other
    modules (e.g. ``services.popularity.release_cache_service``) can reuse
    the classification tokens without tripping protected-member linters.
    """
    if not formats:
        return ""
    if isinstance(formats, str):
        parts = re.split(r"[,/]", formats)
    elif isinstance(formats, (list, tuple)):
        parts = [p for f in formats for p in re.split(r"[,/]", str(f))]
    else:
        parts = [str(formats)]
    return " ".join(p.strip().lower() for p in parts if p and p.strip())


# Backwards-compatible private alias (internal callers still reference it).
_release_format_key = release_format_key


def _discogs_title_similarity(local_title: str, candidate_title: str) -> float:
    local_key = _clean_title_for_comparison(local_title)
    candidate_key = _clean_title_for_comparison(candidate_title)
    if not local_key or not candidate_key:
        return 0.0
    if local_key == candidate_key:
        return 1.0

    shorter, longer = (
        (local_key, candidate_key)
        if len(local_key) <= len(candidate_key)
        else (candidate_key, local_key)
    )
    if shorter in longer and len(shorter) / len(longer) >= 0.70:
        return 0.95

    if "/" in (local_title or "") or "/" in (candidate_title or ""):
        for raw in (local_title, candidate_title):
            if "/" not in (raw or ""):
                continue
            primary = re.split(r"\s*/\s*", raw.strip(), maxsplit=1)[0] if raw else ""
            primary_key = _clean_title_for_comparison(primary)
            if not primary_key:
                continue
            if primary_key == local_key or primary_key == candidate_key:
                return 0.95
            for other_key in (local_key, candidate_key):
                if primary_key in other_key and len(primary_key) / len(other_key) >= 0.70:
                    return 0.95

    def _sorted(value: str) -> str:
        return " ".join(sorted(value.split()))

    sim = max(
        _rapidfuzz_fuzz.token_set_ratio(local_key, candidate_key),
        _rapidfuzz_fuzz.partial_ratio(local_key, candidate_key),
    ) / 100.0 if _rapidfuzz_fuzz is not None else max(
        SequenceMatcher(None, local_key, candidate_key).ratio(),
        SequenceMatcher(None, _sorted(local_key), _sorted(candidate_key)).ratio(),
    )

    local_words = re.findall(r"[a-z0-9]+", local_key)
    cand_words = re.findall(r"[a-z0-9]+", candidate_key)
    if cand_words and len(local_words) > 2 * len(cand_words):
        local_set = set(local_words)
        if all(w in local_set for w in cand_words):
            return 0.60

    return sim


def _release_artist_matches(result_artist: str, query_artist: str) -> bool:
    def _norm(value: str) -> str:
        value = strip_featured_artist(value or "")
        value = re.sub(r"[\(\[][^\)\]]*[\)\]]", " ", value)
        return re.sub(r"\s+", " ", value).strip().lower()

    q, r = _norm(query_artist), _norm(result_artist)
    return bool(q and r and (q == r or q in r or r in q))


class DiscogsTrack(TypedDict):
    number: str
    title: str
    artist: str
    duration: int | None
    isrc: str


class DiscogsArtistProfile(TypedDict):
    profile: str
    real_name: str | None
    urls: list[str]
    images: list[dict[str, Any]]


def _parse_discogs_duration(duration_str: str) -> int | None:
    if not duration_str: 
        return None
    try:
        parts = duration_str.split(":")
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
    except Exception: 
        pass
    return None


def resolve_master_formats(releases: list[dict[str, Any]], http: DiscogsHttpClient) -> None:
    # Cap how many master-format resolutions run per artist fetch.  Each
    # master requires its own ``get_release`` call at Discogs' 1 req/s
    # throttle — a catalogue-heavy artist with 50+ masters would otherwise
    # add 50+ serialised seconds to EVERY cold artist-releases fetch (which
    # used to run once per track worker, blowing the per-track budget).
    # Formats resolved here are only a classification aid; an unresolved
    # master falls back to its existing ``format`` token.
    resolved = 0
    for rel in releases:
        if not isinstance(rel, dict):
            continue
        if (
            rel.get("type") == "master"
            and not rel.get("format")
            and str(rel.get("role") or "Main").lower() == "main"
            and rel.get("main_release")
        ):
            if resolved >= _MAX_MASTER_FORMAT_RESOLUTIONS:
                continue
            resolved += 1
            try:
                main = http.get_release(rel["main_release"], timeout=8.0)
                rel["format"] = [
                    " ".join(
                        part for part in (
                            str(f.get("name") or ""),
                            " ".join(str(d) for d in (f.get("descriptions") or [])),
                        ) if part
                    )
                    for f in (main.get("formats") or [])
                ]
                rel["track_count"] = len(main.get("tracklist") or []) or None
            except Exception as exc:
                logger.debug("Master format lookup failed", title=rel.get("title"), error=str(exc))


class DiscogsService:
    def __init__(self, token: str, http_client: DiscogsHttpClient | None = None, enabled: bool = True):
        self.token = token or ""
        self.enabled = enabled
        self.http = http_client or DiscogsHttpClient(token=token)
        self._single_cache: dict[tuple[str, str], dict[str, Any]] = {}
        self._video_cache: dict[tuple[str, str], bool] = {}
        self._artist_releases_cache: dict[str, list[dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def _normalize_title(self, title: str) -> str:
        base = strip_parentheses(strip_featured_guest_suffix(title) or title)
        return normalize_title_for_lookup(base or title)

    def _get_artist_releases(self, artist: str) -> list[dict[str, Any]]:
        key = artist.lower()

        # Double-checked locking: hold the lock across the ENTIRE fetch so
        # concurrent track workers for the same artist don't each re-run the
        # full Discogs catalogue fetch (get_artist_id + 10 pages of releases
        # + resolve_master_formats) simultaneously.  Previously the fetch ran
        # OUTSIDE the lock, so with a cold cache every worker serialised its
        # own copy of 50-100+ Discogs calls on the 1 req/s throttle — each
        # track then took 300-600s+ and every album's workers timed out.
        with self._lock:
            if key in self._artist_releases_cache:
                return self._artist_releases_cache[key]

            releases: list[dict[str, Any]] = []
            rows = None
            try:
                from services.popularity.release_cache_service import get_cached_artist_release_rows
                rows = get_cached_artist_release_rows(artist, source="discogs")
            except Exception as exc:
                logger.debug("Release-cache read failed", artist=artist, error=str(exc))

            if rows is not None:
                releases = [
                    {
                        "title": str(r.get("title") or ""),
                        "role": "Main",
                        "id": str(r.get("release_id") or ""),
                        "year": r.get("year"),
                        "format": [str(r.get("release_type") or "album").lower()]
                        + (["promo"] if r.get("is_promo") else []),
                        "track_count": None,
                    }
                    for r in rows
                ]
            else:
                artist_id = self.get_artist_id(artist)
                if artist_id:
                    releases = self.http.get_artist_releases_all(artist_id, max_pages=10) or []
                    resolve_master_formats(releases, self.http)
                    try:
                        from services.popularity.release_cache_service import upsert_artist_release_rows
                        upsert_artist_release_rows(artist, releases)
                    except Exception as exc:
                        logger.debug("Release-cache write-back failed", artist=artist, error=str(exc))

            self._artist_releases_cache[key] = releases
            return releases

    @staticmethod
    def _release_is_promo(rel: dict[str, Any]) -> bool:
        return "promo" in _release_format_key(rel.get("format")).split()

    def _scan_releases(self, title: str, title_key: str, releases: list[dict[str, Any]],
                       artist_verified: bool = True) -> dict[str, Any] | None:
        best_commercial: dict[str, Any] | None = None
        best_promo: dict[str, Any] | None = None
        best_commercial_score = 0.0
        best_promo_score = 0.0

        title = strip_featured_guest_suffix(title) or title

        def _status(rel: dict[str, Any], formats: str, is_promo: bool, sim: float) -> dict[str, Any]:
            return {
                "is_single": True,
                "is_promo": is_promo,
                "release_year": rel.get("year") if isinstance(rel.get("year"), int) else None,
                "release_id": str(rel.get("id") or "") or None,
                "format": formats,
                "similarity": round(sim, 2),
                "artist_verified": artist_verified,
            }

        for rel in releases:
            if str(rel.get("role") or "Main").strip().lower() != "main":
                continue
            formats = _release_format_key(rel.get("format"))
            if not formats:
                continue
            if ALBUM_FORMAT_TOKENS.intersection(formats.split()):
                continue
            if not SINGLE_FORMAT_TOKENS.intersection(formats.split()):
                continue
            track_count = rel.get("track_count")
            if track_count and int(track_count) > MAX_SINGLE_TRACKS:
                continue
            if not edition_annotations_compatible(title, str(rel.get("title") or "")):
                continue
            rel_title = self._normalize_title(str(rel.get("title") or ""))
            if not rel_title:
                continue
            sim = _discogs_title_similarity(title, str(rel.get("title") or ""))
            if sim < MIN_DISCOGS_SIMILARITY:
                continue
            is_promo = "promo" in formats
            status = _status(rel, formats, is_promo, sim)
            if not is_promo:
                if sim > best_commercial_score:
                    best_commercial_score = sim
                    best_commercial = status
            elif sim > best_promo_score:
                best_promo_score = sim
                best_promo = status
        return best_commercial or best_promo

    def get_single_status(self, title: str, artist: str,
                          album_context: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.enabled or not self.token or not title or not artist:
            return {"is_single": False, "is_promo": False, "release_year": None, "release_id": None, "format": ""}
        if album_context and album_context.get("is_special_edition"):
            return {"is_single": False, "is_promo": False, "release_year": None, "release_id": None, "format": ""}

        title_key = self._normalize_title(title)
        cache_key = (artist.lower(), title_key)
        
        with self._lock:
            if cache_key in self._single_cache:
                return self._single_cache[cache_key]

        artist_releases = self._get_artist_releases(artist) or []
        status = self._scan_releases(title, title_key, artist_releases, artist_verified=True)

        if status is None:
            logger.debug("No single/EP match on artist releases", artist=artist, track=title, release_count=len(artist_releases))

        if status is None:
            results = self.http.search_database({"q": f"{strip_featured_artist(artist)} {title_key}", "type": "release", "per_page": 25})
            results = [
                r for r in (results or [])
                if _release_artist_matches(str(r.get("artist") or ""), artist)
            ]
            status = self._scan_releases(title, title_key, results, artist_verified=False)

        _inv_used = False
        if (status is None or float(status.get("similarity") or 0.0) < INVERTED_RETRY_MIN_SIMILARITY):
            from services.popularity.popularity_sources import invert_featured_artist
            inverted = invert_featured_artist(artist)
            if inverted != artist:
                _std_sim = float(status.get("similarity") or 0.0) if status else 0.0
                inv_status = self._scan_releases(
                    title, title_key, self._get_artist_releases(inverted) or [],
                    artist_verified=True,
                )
                if inv_status is None:
                    inv_results = self.http.search_database(
                        {"q": f"{inverted} {title_key}", "type": "release", "per_page": 25}
                    )
                    inv_status = self._scan_releases(title, title_key, inv_results or [], artist_verified=False)
                if inv_status and float(inv_status.get("similarity") or 0.0) > _std_sim:
                    inv_status["inverted_match_used"] = True
                    status = inv_status
                    _inv_used = True
                    _sim = inv_status.get("similarity", 0.0)
                    logger.info(
                        "Inverted artist match retry succeeded",
                        standard_sim=_std_sim, inverted=inverted, sim=_sim,
                    )

        if status is None:
            status = {"is_single": False, "is_promo": False, "release_year": None,
                      "release_id": None, "format": "", "similarity": 0.0,
                      "artist_verified": False}
        if _inv_used:
            status["inverted_match_used"] = True

        with self._lock:
            self._single_cache[cache_key] = status
            
        return status

    @staticmethod
    def _is_official_video_for_track(video: dict[str, Any], track_title_lower: str) -> bool:
        video_title = (video.get("title") or "").lower()
        video_desc = (video.get("description") or "").lower()

        official_pattern = re.compile(r"\b(official|promo)\b")
        is_official_or_promo = bool(
            official_pattern.search(video_title) or official_pattern.search(video_desc)
        )

        def _canonical(value: str) -> str:
            return normalize_title_for_lookup(
                value.replace("'", "").replace("’", "")
            )

        video_title_cleaned = re.sub(
            r"\s*[\(\[]?(official|music|promo)?\s*(video|music video|mv|hd|4k|lyric video)[\)\]]?\s*$",
            "", video_title, flags=re.IGNORECASE,
        ).strip()
        if " - " in video_title_cleaned:
            parts = video_title_cleaned.split(" - ", 1)
            if len(parts) == 2:
                video_title_cleaned = parts[1].strip()

        matches_title = _canonical(track_title_lower) == _canonical(video_title_cleaned)

        if not matches_title and video_desc:
            desc_cleaned = re.sub(
                r"\s*[\(\[]?(official|music|promo)?\s*(video|music video|mv|hd|4k|lyric video)[\)\]]?\s*",
                "", video_desc, flags=re.IGNORECASE,
            ).strip()
            if " - " in desc_cleaned:
                parts = desc_cleaned.split(" - ", 1)
                if len(parts) == 2:
                    desc_cleaned = parts[1].strip()
            matches_title = _canonical(track_title_lower) == _canonical(desc_cleaned)

        return is_official_or_promo and matches_title

    def has_official_video(self, title: str, artist: str) -> bool:
        if not self.enabled or not self.token or not title or not artist:
            return False
            
        cache_key = (artist.lower(), self._normalize_title(title))
        
        with self._lock:
            if cache_key in self._video_cache:
                return self._video_cache[cache_key]
                
        matched = False
        try:
            results = self.http.search_database(
                {"q": f"{artist} {title}", "type": "master", "per_page": 10}
            ) or []
            for rel in results[:5]:
                master_id = rel.get("id")
                if not master_id:
                    continue
                master = self.http.get_master(master_id, timeout=8.0)
                if not master:
                    continue
                for video in (master.get("videos") or []):
                    if self._is_official_video_for_track(video, title.lower()):
                        matched = True
                        break
                if matched:
                    break
        except Exception as exc:
            logger.debug("Official video check failed", artist=artist, track=title, error=str(exc))
            
        with self._lock:
            self._video_cache[cache_key] = matched
            
        return matched

    def is_single(self, title: str, artist: str, album_context: dict[str, Any] | None = None) -> bool:
        return bool(self.get_single_status(title, artist, album_context=album_context).get("is_single"))

    def get_artist_id(self, artist: str, timeout: float = 10.0) -> str | None:
        if not self.enabled or not self.token or not artist:
            return None
        try:
            results = self.http.search_database(
                {"q": artist, "type": "artist", "per_page": 5},
                timeout=timeout,
            )
            if results and isinstance(results, list):
                first = results[0]
                if isinstance(first, dict) and first.get("id"):
                    return str(first["id"])
        except Exception as exc:
            logger.debug("Artist ID lookup failed", artist=artist, error=str(exc))
        return None

    def get_genres(self, title: str, artist: str) -> list[str]:
        if not self.enabled or not self.token: 
            return []
            
        results = self.http.search_database({"q": f"{artist} {title}", "type": "release", "per_page": 5})
        
        genres = []
        for r in results:
            genres.extend(r.get("genre", []))
            genres.extend(r.get("style", []))
        return genres

    def get_artist_biography(self, artist: str) -> DiscogsArtistProfile:
        results = self.http.search_database({"q": artist, "type": "artist", "per_page": 1})
        if not results:
            return {"profile": "", "real_name": None, "urls": [], "images": []}
        
        artist_id = results[0].get("id")
        data = self.http.get_artist(artist_id) if artist_id else {}
        return {
            "profile": clean_discogs_biography(data.get("profile", "")),
            "real_name": data.get("realname"),
            "urls": data.get("urls", []),
            "images": data.get("images", []),
        }

    def get_release_tracks(self, release_id: str) -> list[DiscogsTrack]:
        if not self.enabled or not self.token or not release_id: 
            return []
        release = self.http.get_release(release_id)
        if not isinstance(release, dict): 
            return []
        
        tracks = []
        for track in release.get("tracklist", []):
            tracks.append({
                "number": track.get("position", ""),
                "title": track.get("title", ""),
                "artist": track.get("artist", track.get("artists", [{}])[0].get("name", "")),
                "duration": _parse_discogs_duration(track.get("duration", "")),
                "isrc": ""
            })
        return tracks


# --- BRIDGE FUNCTIONS ---
_DEFAULT_SERVICE: DiscogsService | None = None
_INIT_LOCK = threading.Lock()

def _get_service(token: str) -> DiscogsService:
    global _DEFAULT_SERVICE
    if _DEFAULT_SERVICE is None or getattr(_DEFAULT_SERVICE, "token", None) != token:
        with _INIT_LOCK:
            if _DEFAULT_SERVICE is None or getattr(_DEFAULT_SERVICE, "token", None) != token:
                _DEFAULT_SERVICE = DiscogsService(token=token)
    return _DEFAULT_SERVICE

def is_discogs_single(title: str, artist: str, token: str = "", album_context: dict[str, Any] | None = None) -> bool:
    return _get_service(token).is_single(title, artist, album_context=album_context)

def get_discogs_genres(title: str, artist: str, token: str = "") -> list[str]:
    return _get_service(token).get_genres(title, artist)

def get_discogs_artist_biography(artist: str, token: str = "") -> DiscogsArtistProfile:
    return _get_service(token).get_artist_biography(artist)

def has_discogs_video(title: str, artist: str, token: str = "") -> bool:
    return _get_service(token).has_official_video(title, artist)


def lookup_discogs_album(artist: str, album: str) -> dict[str, Any]:
    from api_clients.discogs_http import DiscogsHttpClient
    from helpers.config_helpers import get_config
    cfg = get_config() or {}
    token = cfg.get("api_integrations", {}).get("discogs", {}).get("token", "") or ""
    if not token:
        return {"success": False, "error": "Discogs token not configured"}
    try:
        http = DiscogsHttpClient(token=token)
        results = http.search_database({"q": f"{artist} {album}", "type": "release", "per_page": 5})
        return {"success": True, "results": results}
    except Exception as exc:
        return {"success": False, "error": str(exc)}
