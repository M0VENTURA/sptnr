"""Low-level MusicBrainz HTTP client.

Strictly adheres to MusicBrainz API Rules:
1. Hard 1 request/sec global limit via thread-locked turnstile.
2. Compliant User-Agent with version and contact URL.
3. Exponential backoff on 429/503 responses.
4. Aggressive, thread-safe in-memory caching to minimize server load.
"""

from __future__ import annotations

import os
import re
import threading
import time
from typing import Any

import httpx
import structlog
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from api_clients import session
from services.infrastructure.api_rate_limiter import get_rate_limiter

logger = structlog.get_logger(__name__)


def get_version() -> str:
    """Retrieve application version for the User-Agent header."""
    try:
        version_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), "VERSION")
        with open(version_file, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except Exception:
        return "2.0.0-alpha"


# MusicBrainz requires an identifiable User-Agent with contact info.
USER_AGENT = f"Popularr/{get_version()} ( https://github.com/M0VENTURA/Popularr )"

MUSICBRAINZ_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

try:
    _rate_limiter = get_rate_limiter()
except Exception:
    _rate_limiter = None


def escape_lucene_special_chars(text: str) -> str:
    """Escape Lucene special chars for MusicBrainz search queries."""
    special_chars = ['+', '-', '!', '(', ')', '{', '}', '[', ']', '^', '"', '~', '*', '?', ':', '\\', '/']
    escaped = (text or "").replace('\\', '\\\\')
    for char in special_chars:
        if char != '\\':
            escaped = escaped.replace(char, '\\' + char)
    return escaped


# =============================================================================
# STRICT RATE LIMITING & THREAD-SAFE CACHES
# =============================================================================

_CACHE_LOCK = threading.Lock()
_THROTTLE_LOCK = threading.Lock()
_LAST_MB_REQUEST_TIME = 0.0

_RECORDING_INC_SUPERSET = "artist-credits+releases+work-rels+recording-rels+artist-rels+genres+tags"
_RECORDING_DETAIL_CACHE: dict[str, dict[str, Any]] = {}
_RECORDING_DETAIL_CACHE_MAX = 4000

_ISRC_INC_SUPERSET = "artist-credits+releases+work-rels"
_ISRC_LOOKUP_CACHE: dict[str, list[dict[str, Any]]] = {}
_ISRC_LOOKUP_CACHE_MAX = 2000

_RELEASE_INC_SUPERSET = "recordings+artist-credits+media+release-groups+labels"
_RELEASE_DETAIL_CACHE: dict[str, dict[str, Any]] = {}
_RELEASE_DETAIL_CACHE_MAX = 2000


def _strict_throttle() -> None:
    """A thread-locked turnstile guaranteeing <= 1 request per second globally.
    
    This acts as a failsafe even if the external _rate_limiter is bypassed,
    preventing 11 track workers from bursting MusicBrainz simultaneously.
    """
    global _LAST_MB_REQUEST_TIME
    
    if _rate_limiter:
        try:
            _rate_limiter.throttle_musicbrainz()
            return
        except Exception:
            pass

    # Fallback strict local throttle
    with _THROTTLE_LOCK:
        now = time.monotonic()
        elapsed = now - _LAST_MB_REQUEST_TIME
        if elapsed < 1.0:
            time.sleep(1.0 - elapsed)
        _LAST_MB_REQUEST_TIME = time.monotonic()


def _safe_cache_set(cache: dict[str, Any], key: str, value: Any, max_size: int) -> None:
    """Safely store items in a cache under a thread lock with FIFO eviction."""
    with _CACHE_LOCK:
        while len(cache) >= max_size:
            try:
                first_key = next(iter(cache))
                cache.pop(first_key, None)
            except (StopIteration, RuntimeError):
                break
        cache[key] = value


def _is_retryable_mb_error(exc: BaseException) -> bool:
    """Retry on transient network drops or temporary rate-limiting (429/503/504)."""
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {429, 502, 503, 504}
    return False


# =============================================================================
# HTTP CLIENT
# =============================================================================

class MusicBrainzHttpClient:
    """Strictly compliant MusicBrainz API wrapper."""

    def __init__(self, http_session: Any = None, enabled: bool = True):
        self.session = http_session or session
        self.enabled = enabled
        self.base_url = "https://musicbrainz.org/ws/2/"
        self.headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}

    def get(self, endpoint: str, *, params: dict[str, Any] | None = None, timeout: float = 10.0) -> dict[str, Any]:
        if not self.enabled:
            return {}

        url = f"{self.base_url}{endpoint.lstrip('/')}"
        query_params = params or {}

        # If MB throws a 503 Rate Limit, back off automatically and retry
        @retry(
            stop=stop_after_attempt(4),
            wait=wait_exponential(multiplier=1.5, min=1.5, max=10.0),
            retry=retry_if_exception(_is_retryable_mb_error),
            reraise=True,
        )
        def _execute_request() -> dict[str, Any]:
            _strict_throttle()
            response = self.session.get(
                url,
                params=query_params,
                headers=self.headers,
                timeout=timeout,
            )
            response.raise_for_status()
            payload = response.json()
            return payload if isinstance(payload, dict) else {}

        try:
            return _execute_request()
        except Exception as exc:
            logger.warning("MusicBrainz request failed permanently after retries", endpoint=endpoint, error=str(exc))
            return {}

    def search_release_groups(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        # MB Limit max is 100
        payload = self.get("release-group/", params={"query": query, "fmt": "json", "limit": max(1, min(limit, 100))})
        return payload.get("release-groups", []) if isinstance(payload.get("release-groups"), list) else []

    def search_releases(self, query: str, limit: int = 10, inc: str = "") -> list[dict[str, Any]]:
        params = {"query": query, "fmt": "json", "limit": max(1, min(limit, 100))}
        if inc:
            params["inc"] = inc
        payload = self.get("release/", params=params)
        return payload.get("releases", []) if isinstance(payload.get("releases"), list) else []

    def search_recordings(self, query: str, limit: int = 10, inc: str = "") -> list[dict[str, Any]]:
        params = {"query": query, "fmt": "json", "limit": max(1, min(limit, 100))}
        if inc:
            params["inc"] = inc
        payload = self.get("recording/", params=params)
        return payload.get("recordings", []) if isinstance(payload.get("recordings"), list) else []

    def search_artists(self, query: str, limit: int = 10, inc: str = "") -> list[dict[str, Any]]:
        params = {"query": query, "fmt": "json", "limit": max(1, min(limit, 100))}
        if inc:
            params["inc"] = inc
        payload = self.get("artist/", params=params)
        return payload.get("artists", []) if isinstance(payload.get("artists"), list) else []

    def get_artist_country(self, artist: str) -> str:
        if not self.enabled or not artist:
            return ""
        try:
            results = self.search_artists(
                f'artist:"{escape_lucene_special_chars(artist)}"',
                limit=1,
                inc="area",
            )
            if not results:
                return ""
            data = results[0]
            return (
                (data.get("area") or {}).get("name")
                or (data.get("begin-area") or {}).get("name")
                or ""
            )
        except Exception:
            return ""

    def get_release(self, release_mbid: str, inc: str = "", timeout: float = 10.0) -> dict[str, Any]:
        if not release_mbid:
            return {}
        with _CACHE_LOCK:
            cached = _RELEASE_DETAIL_CACHE.get(release_mbid)
        if cached is not None:
            return cached

        params = {"fmt": "json", "inc": _RELEASE_INC_SUPERSET}
        data = self.get(f"release/{release_mbid}", params=params, timeout=timeout)
        if data:
            _safe_cache_set(_RELEASE_DETAIL_CACHE, release_mbid, data, _RELEASE_DETAIL_CACHE_MAX)
        return data

    def get_release_group(self, release_group_mbid: str, inc: str = "", timeout: float = 10.0) -> dict[str, Any]:
        if not release_group_mbid:
            return {}
        params: dict[str, Any] = {"fmt": "json"}
        if inc:
            params["inc"] = inc
        return self.get(f"release-group/{release_group_mbid}", params=params, timeout=timeout)

    def get_recording(self, recording_mbid: str, inc: str = "", timeout: float = 10.0) -> dict[str, Any]:
        if not recording_mbid:
            return {}
        with _CACHE_LOCK:
            cached = _RECORDING_DETAIL_CACHE.get(recording_mbid)
        if cached is not None:
            return cached

        params = {"fmt": "json", "inc": _RECORDING_INC_SUPERSET}
        data = self.get(f"recording/{recording_mbid}", params=params, timeout=timeout)
        if data:
            _safe_cache_set(_RECORDING_DETAIL_CACHE, recording_mbid, data, _RECORDING_DETAIL_CACHE_MAX)
        return data

    def get_artist(self, artist_mbid: str, inc: str = "", timeout: float = 10.0) -> dict[str, Any]:
        if not artist_mbid:
            return {}
        params = {"fmt": "json"}
        if inc:
            params["inc"] = inc
        return self.get(f"artist/{artist_mbid}", params=params, timeout=timeout)

    def get_artist_members(self, artist_mbid: str, timeout: float = 10.0) -> list[dict[str, Any]]:
        if not self.enabled or not artist_mbid:
            return []
        try:
            data = self.get_artist(artist_mbid, inc="artist-rels", timeout=timeout)
            relations = data.get("relations", [])
            members = []
            allowed_types = {"member of band", "member", "has member", "founder", "co-founder"}
            for rel in relations:
                rtype = rel.get("type", "").lower()
                if rtype not in allowed_types:
                    continue
                target = rel.get("artist", {})
                if not target or not target.get("name"):
                    continue
                members.append({
                    "name": target.get("name"),
                    "relation_type": rtype,
                    "begin": rel.get("begin", ""),
                    "end": rel.get("end", ""),
                    "ended": rel.get("ended", False),
                    "attributes": rel.get("attributes", []),
                })
            return members
        except Exception:
            return []

    def browse_releases_for_group(self, release_group_mbid: str, inc: str = "media", limit: int = 50) -> list[dict[str, Any]]:
        payload = self.get("release", params={"fmt": "json", "release-group": release_group_mbid, "inc": inc, "limit": limit})
        return payload.get("releases", []) if isinstance(payload.get("releases"), list) else []

    def browse_work_recordings(self, work_mbid: str, inc: str = "artist-credits", limit: int = 100) -> list[dict[str, Any]]:
        if not self.enabled or not work_mbid:
            return []
        params = {"fmt": "json", "work": work_mbid, "limit": min(limit, 100)}
        if inc:
            params["inc"] = inc
        payload = self.get("recording", params=params)
        return payload.get("recordings", []) if isinstance(payload.get("recordings"), list) else []

    def browse_artist_releases(self, artist_mbid: str, inc: str = "", limit: int = 25, offset: int = 0) -> dict[str, Any]:
        params = {"fmt": "json", "artist": artist_mbid, "limit": min(limit, 100), "offset": offset}
        if inc:
            params["inc"] = inc
        payload = self.get("release", params=params)
        return {
            "releases": payload.get("releases", []) or [],
            "release_count": payload.get("release-count", 0) or 0,
            "release_offset": payload.get("release-offset", offset) or offset,
        }

    def browse_artist_release_groups(self, artist_mbid: str, inc: str = "", limit: int = 25, offset: int = 0) -> dict[str, Any]:
        params = {"fmt": "json", "artist": artist_mbid, "limit": min(limit, 100), "offset": offset}
        if inc:
            params["inc"] = inc
        payload = self.get("release-group", params=params)
        return {
            "release_groups": payload.get("release-groups", []) or [],
            "release_group_count": payload.get("release-group-count", 0) or 0,
            "release_group_offset": payload.get("release-group-offset", offset) or offset,
        }

    def lookup_by_isrc(self, isrc: str, inc: str = "") -> list[dict[str, Any]]:
        if not isrc:
            return []
        cache_key = isrc.strip().upper()
        with _CACHE_LOCK:
            cached = _ISRC_LOOKUP_CACHE.get(cache_key)
        if cached is not None:
            return cached

        params = {"fmt": "json", "inc": _ISRC_INC_SUPERSET}
        payload = self.get(f"isrc/{isrc}", params=params)
        recordings = payload.get("recordings", []) if isinstance(payload.get("recordings"), list) else []
        
        _safe_cache_set(_ISRC_LOOKUP_CACHE, cache_key, recordings, _ISRC_LOOKUP_CACHE_MAX)
        return recordings

    def search_recordings_with_genres(self, query: str, limit: int = 25) -> list[dict[str, Any]]:
        return self.search_recordings(query, limit=limit, inc="genres")
