"""Low-level MusicBrainz HTTP client.

Strictly adheres to MusicBrainz API Rules:
1. Hard 1 request/sec global limit via thread-locked turnstile.
2. Compliant User-Agent with version and contact URL.
3. Exponential backoff on 429/503 responses.
4. Aggressive, thread-safe in-memory caching to minimize server load.
"""

from __future__ import annotations

import copy
import os
import re
import threading
import time
from collections import OrderedDict
from typing import Any

import httpx
import structlog
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
    wait_random_exponential,
)

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
# STRICT RATE LIMITING & THREAD-SAFE LRU CACHES
# =============================================================================

_THROTTLE_LOCK = threading.Lock()
_LAST_MB_REQUEST_TIME = 0.0

_RECORDING_INC_SUPERSET = "artist-credits+releases+work-rels+recording-rels+artist-rels+genres+tags"
_ISRC_INC_SUPERSET = "artist-credits+releases+work-rels"
_RELEASE_INC_SUPERSET = "recordings+artist-credits+media+release-groups+labels+work-rels+recording-rels+genres"


class _LruCache:
    """Thread-safe, size-bounded LRU cache.

    Each cache instance owns its own lock so that lookups/inserts against
    one cache (e.g. recordings) never contend with another (e.g. releases).
    Values are deep-copied on both set and get so callers can freely mutate
    what they receive without corrupting the shared cache entry (and vice
    versa — mutating a value before caching won't affect other holders of it).
    """

    def __init__(self, max_size: int):
        self._data: OrderedDict[str, Any] = OrderedDict()
        self._lock = threading.Lock()
        self._max_size = max_size

    def get(self, key: str) -> Any:
        with self._lock:
            if key not in self._data:
                return None
            self._data.move_to_end(key)
            return copy.deepcopy(self._data[key])

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
            self._data[key] = copy.deepcopy(value)
            while len(self._data) > self._max_size:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


_RECORDING_DETAIL_CACHE = _LruCache(max_size=4000)
_ISRC_LOOKUP_CACHE = _LruCache(max_size=2000)
_RELEASE_DETAIL_CACHE = _LruCache(max_size=2000)


def _strict_throttle() -> None:
    """A thread-locked turnstile guaranteeing <= 1 request per second globally.

    This acts as a failsafe even if the external _rate_limiter is bypassed,
    preventing track workers from bursting MusicBrainz simultaneously.
    """
    global _LAST_MB_REQUEST_TIME

    if _rate_limiter:
        try:
            _rate_limiter.throttle_musicbrainz()
            return
        except Exception as exc:
            logger.debug("External MusicBrainz rate limiter failed, using local fallback", error=str(exc))

    # Fallback strict local throttle
    with _THROTTLE_LOCK:
        now = time.monotonic()
        elapsed = now - _LAST_MB_REQUEST_TIME
        if elapsed < 1.0:
            time.sleep(1.0 - elapsed)
        _LAST_MB_REQUEST_TIME = time.monotonic()


def _is_retryable_mb_error(exc: BaseException) -> bool:
    """Retry on transient network drops or temporary rate-limiting (429/503/504)."""
    from api_clients.http_utils import is_ssl_cert_error

    if is_ssl_cert_error(exc):
        # Certificate verification failure is a deterministic config error
        # (missing CA bundle / expired cert) — retrying just burns ~40s per
        # call with no chance of success, which makes scans look stalled.
        return False
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {429, 502, 503, 504}
    return False


def _is_valid_mbid(mbid: str) -> bool:
    """Return True when *mbid* is a well-formed MusicBrainz UUID."""
    return bool(mbid) and bool(MUSICBRAINZ_UUID_RE.match(str(mbid).strip()))


def _wait_for_mb_retry_after(retry_state: Any) -> float:
    """Honor a server-provided ``Retry-After`` header (429/503) when present.

    MusicBrainz's rate limiting is enforced with bare 503s and no documented
    guarantee of a Retry-After header, but respecting one when it *is* sent
    is cheap, forward-compatible, and more considerate than a fixed backoff
    curve.  Falls back to the jittered exponential backoff otherwise.
    """
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    if isinstance(exc, httpx.HTTPStatusError):
        retry_after = exc.response.headers.get("Retry-After")
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                pass
    return wait_random_exponential(multiplier=1.5, max=10.0)(retry_state)


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

    def clear_caches(self) -> None:
        """Drop all in-memory MusicBrainz response caches.

        Long-running batch scans across thousands of artists can grow the
        LRU caches (recording detail, ISRC lookup, release detail) up to
        their max sizes; call this between major multi-artist pipeline runs
        to reclaim memory.  Thread-safe — each cache clears under its own
        lock.
        """
        _RECORDING_DETAIL_CACHE.clear()
        _ISRC_LOOKUP_CACHE.clear()
        _RELEASE_DETAIL_CACHE.clear()

    def get(self, endpoint: str, *, params: dict[str, Any] | None = None, timeout: float = 10.0) -> dict[str, Any]:
        if not self.enabled:
            return {}

        url = f"{self.base_url}{endpoint.lstrip('/')}"
        query_params = params or {}

        # If MB throws a 503 Rate Limit, back off automatically and retry.
        # ``_wait_for_mb_retry_after`` honors a server ``Retry-After`` header
        # when present and otherwise uses ``wait_random_exponential`` (jitter
        # stops concurrent workers waking at the same instant and re-hitting a
        # busy server — self-DOS).  The window stays capped at 10s.
        @retry(
            stop=stop_after_attempt(4),
            wait=_wait_for_mb_retry_after,
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
        except httpx.HTTPStatusError as exc:
            # 400/404 are deterministic "bad request / not found" responses
            # (deleted or merged MBIDs, stale cached GUIDs) — they are NOT
            # transient and retrying never helps.  Log them at DEBUG so dead
            # entities don't clutter production logs with WARNING noise.
            if exc.response.status_code in (400, 404):
                logger.debug(
                    "MusicBrainz request not found (permanent)",
                    endpoint=endpoint,
                    status_code=exc.response.status_code,
                    error=str(exc),
                )
            else:
                logger.warning(
                    "MusicBrainz request failed permanently after retries",
                    endpoint=endpoint,
                    status_code=exc.response.status_code,
                    error=str(exc),
                )
            return {}
        except Exception as exc:
            logger.warning("MusicBrainz request failed permanently after retries", endpoint=endpoint, error=str(exc))
            return {}

    def search_release_groups(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
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
        if not _is_valid_mbid(release_mbid):
            if release_mbid:
                logger.debug("Rejected malformed release MBID", release_mbid=release_mbid)
            return {}

        cached = _RELEASE_DETAIL_CACHE.get(release_mbid)
        if cached is not None:
            return cached

        params = {"fmt": "json", "inc": _RELEASE_INC_SUPERSET}
        data = self.get(f"release/{release_mbid}", params=params, timeout=timeout)
        if data:
            _RELEASE_DETAIL_CACHE.set(release_mbid, data)
        return data

    def get_release_group(self, release_group_mbid: str, inc: str = "", timeout: float = 10.0) -> dict[str, Any]:
        if not _is_valid_mbid(release_group_mbid):
            if release_group_mbid:
                logger.debug("Rejected malformed release-group MBID", release_group_mbid=release_group_mbid)
            return {}
        params: dict[str, Any] = {"fmt": "json"}
        if inc:
            params["inc"] = inc
        return self.get(f"release-group/{release_group_mbid}", params=params, timeout=timeout)

    def get_recording(self, recording_mbid: str, inc: str = "", timeout: float = 10.0) -> dict[str, Any]:
        if not _is_valid_mbid(recording_mbid):
            if recording_mbid:
                logger.debug("Rejected malformed recording MBID", recording_mbid=recording_mbid)
            return {}

        cached = _RECORDING_DETAIL_CACHE.get(recording_mbid)
        if cached is not None:
            return cached

        params = {"fmt": "json", "inc": _RECORDING_INC_SUPERSET}
        data = self.get(f"recording/{recording_mbid}", params=params, timeout=timeout)
        if data:
            _RECORDING_DETAIL_CACHE.set(recording_mbid, data)
        return data

    def get_recordings_bulk(self, recording_mbids: list[str], inc: str = "", timeout: float = 10.0) -> dict[str, Any]:
        """Fetch multiple recordings in a single request."""
        valid_ids = [m for m in recording_mbids if _is_valid_mbid(m)]
        if not valid_ids:
            return {}

        # Check the cache first to avoid unnecessary requests
        results = {}
        missing_ids = []
        for mbid in valid_ids:
            cached = _RECORDING_DETAIL_CACHE.get(mbid)
            if cached is not None:
                results[mbid] = cached
            else:
                missing_ids.append(mbid)

        if not missing_ids:
            return {"recordings": list(results.values())}

        # MusicBrainz allows multiple IDs separated by semicolons
        mbid_string = ";".join(missing_ids)
        params = {"fmt": "json", "query": f"rid:({mbid_string})"}
        if inc:
            params["inc"] = inc

        # Make one request for all missing IDs
        data = self.get("recording/", params=params, timeout=timeout)
        
        # Cache the new results
        if data and isinstance(data.get("recordings"), list):
            for rec in data["recordings"]:
                mbid = rec.get("id")
                if mbid:
                    _RECORDING_DETAIL_CACHE.set(mbid, rec)
                    results[mbid] = rec

        return {"recordings": list(results.values())}

    def get_artist(self, artist_mbid: str, inc: str = "", timeout: float = 10.0) -> dict[str, Any]:
        if not _is_valid_mbid(artist_mbid):
            if artist_mbid:
                logger.debug("Rejected malformed artist MBID", artist_mbid=artist_mbid)
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
            
        # Clean up braces/brackets often found in raw audio file tags
        clean_isrc = str(isrc).strip().strip("{}[]").upper()
        if not clean_isrc:
            return []

        cache_key = clean_isrc
        cached = _ISRC_LOOKUP_CACHE.get(cache_key)
        if cached is not None:
            return cached

        params = {"fmt": "json", "inc": _ISRC_INC_SUPERSET}
        
        # Use clean_isrc so the URL path never contains encoded curly braces
        payload = self.get(f"isrc/{clean_isrc}", params=params)
        recordings = payload.get("recordings", []) if isinstance(payload.get("recordings"), list) else []
        
        _ISRC_LOOKUP_CACHE.set(cache_key, recordings)
        return recordings

    def search_recordings_with_genres(self, query: str, limit: int = 25) -> list[dict[str, Any]]:
        return self.search_recordings(query, limit=limit, inc="genres")
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
# STRICT RATE LIMITING & THREAD-SAFE LRU CACHES
# =============================================================================

_THROTTLE_LOCK = threading.Lock()
_LAST_MB_REQUEST_TIME = 0.0

_RECORDING_INC_SUPERSET = "artist-credits+releases+work-rels+recording-rels+artist-rels+genres+tags"
_ISRC_INC_SUPERSET = "artist-credits+releases+work-rels"
_RELEASE_INC_SUPERSET = "recordings+artist-credits+media+release-groups+labels+work-rels+recording-rels+genres"


class _LruCache:
    """Thread-safe, size-bounded LRU cache.

    Each cache instance owns its own lock so that lookups/inserts against
    one cache (e.g. recordings) never contend with another (e.g. releases).
    Values are deep-copied on both set and get so callers can freely mutate
    what they receive without corrupting the shared cache entry (and vice
    versa — mutating a value before caching won't affect other holders of it).
    """

    def __init__(self, max_size: int):
        self._data: OrderedDict[str, Any] = OrderedDict()
        self._lock = threading.Lock()
        self._max_size = max_size

    def get(self, key: str) -> Any:
        with self._lock:
            if key not in self._data:
                return None
            self._data.move_to_end(key)
            return copy.deepcopy(self._data[key])

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
            self._data[key] = copy.deepcopy(value)
            while len(self._data) > self._max_size:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


_RECORDING_DETAIL_CACHE = _LruCache(max_size=4000)
_ISRC_LOOKUP_CACHE = _LruCache(max_size=2000)
_RELEASE_DETAIL_CACHE = _LruCache(max_size=2000)


def _strict_throttle() -> None:
    """A thread-locked turnstile guaranteeing <= 1 request per second globally.

    This acts as a failsafe even if the external _rate_limiter is bypassed,
    preventing track workers from bursting MusicBrainz simultaneously.
    """
    global _LAST_MB_REQUEST_TIME

    if _rate_limiter:
        try:
            _rate_limiter.throttle_musicbrainz()
            return
        except Exception as exc:
            logger.debug("External MusicBrainz rate limiter failed, using local fallback", error=str(exc))

    # Fallback strict local throttle
    with _THROTTLE_LOCK:
        now = time.monotonic()
        elapsed = now - _LAST_MB_REQUEST_TIME
        if elapsed < 1.0:
            time.sleep(1.0 - elapsed)
        _LAST_MB_REQUEST_TIME = time.monotonic()


def _is_retryable_mb_error(exc: BaseException) -> bool:
    """Retry on transient network drops or temporary rate-limiting (429/503/504)."""
    from api_clients.http_utils import is_ssl_cert_error

    if is_ssl_cert_error(exc):
        # Certificate verification failure is a deterministic config error
        # (missing CA bundle / expired cert) — retrying just burns ~40s per
        # call with no chance of success, which makes scans look stalled.
        return False
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {429, 502, 503, 504}
    return False


def _is_valid_mbid(mbid: str) -> bool:
    """Return True when *mbid* is a well-formed MusicBrainz UUID."""
    return bool(mbid) and bool(MUSICBRAINZ_UUID_RE.match(str(mbid).strip()))


def _wait_for_mb_retry_after(retry_state: Any) -> float:
    """Honor a server-provided ``Retry-After`` header (429/503) when present.

    MusicBrainz's rate limiting is enforced with bare 503s and no documented
    guarantee of a Retry-After header, but respecting one when it *is* sent
    is cheap, forward-compatible, and more considerate than a fixed backoff
    curve.  Falls back to the jittered exponential backoff otherwise.
    """
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    if isinstance(exc, httpx.HTTPStatusError):
        retry_after = exc.response.headers.get("Retry-After")
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                pass
    return wait_random_exponential(multiplier=1.5, max=10.0)(retry_state)


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

    def clear_caches(self) -> None:
        """Drop all in-memory MusicBrainz response caches.

        Long-running batch scans across thousands of artists can grow the
        LRU caches (recording detail, ISRC lookup, release detail) up to
        their max sizes; call this between major multi-artist pipeline runs
        to reclaim memory.  Thread-safe — each cache clears under its own
        lock.
        """
        _RECORDING_DETAIL_CACHE.clear()
        _ISRC_LOOKUP_CACHE.clear()
        _RELEASE_DETAIL_CACHE.clear()

    def get(self, endpoint: str, *, params: dict[str, Any] | None = None, timeout: float = 10.0) -> dict[str, Any]:
        if not self.enabled:
            return {}

        url = f"{self.base_url}{endpoint.lstrip('/')}"
        query_params = params or {}

        # If MB throws a 503 Rate Limit, back off automatically and retry.
        # ``_wait_for_mb_retry_after`` honors a server ``Retry-After`` header
        # when present and otherwise uses ``wait_random_exponential`` (jitter
        # stops concurrent workers waking at the same instant and re-hitting a
        # busy server — self-DOS).  The window stays capped at 10s.
        @retry(
            stop=stop_after_attempt(4),
            wait=_wait_for_mb_retry_after,
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
        except httpx.HTTPStatusError as exc:
            # 400/404 are deterministic "bad request / not found" responses
            # (deleted or merged MBIDs, stale cached GUIDs) — they are NOT
            # transient and retrying never helps.  Log them at DEBUG so dead
            # entities don't clutter production logs with WARNING noise.
            if exc.response.status_code in (400, 404):
                logger.debug(
                    "MusicBrainz request not found (permanent)",
                    endpoint=endpoint,
                    status_code=exc.response.status_code,
                    error=str(exc),
                )
            else:
                logger.warning(
                    "MusicBrainz request failed permanently after retries",
                    endpoint=endpoint,
                    status_code=exc.response.status_code,
                    error=str(exc),
                )
            return {}
        except Exception as exc:
            logger.warning("MusicBrainz request failed permanently after retries", endpoint=endpoint, error=str(exc))
            return {}

    def search_release_groups(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
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
        if not _is_valid_mbid(release_mbid):
            if release_mbid:
                logger.debug("Rejected malformed release MBID", release_mbid=release_mbid)
            return {}

        cached = _RELEASE_DETAIL_CACHE.get(release_mbid)
        if cached is not None:
            return cached

        params = {"fmt": "json", "inc": _RELEASE_INC_SUPERSET}
        data = self.get(f"release/{release_mbid}", params=params, timeout=timeout)
        if data:
            _RELEASE_DETAIL_CACHE.set(release_mbid, data)
        return data

    def get_release_group(self, release_group_mbid: str, inc: str = "", timeout: float = 10.0) -> dict[str, Any]:
        if not _is_valid_mbid(release_group_mbid):
            if release_group_mbid:
                logger.debug("Rejected malformed release-group MBID", release_group_mbid=release_group_mbid)
            return {}
        params: dict[str, Any] = {"fmt": "json"}
        if inc:
            params["inc"] = inc
        return self.get(f"release-group/{release_group_mbid}", params=params, timeout=timeout)

    def get_recording(self, recording_mbid: str, inc: str = "", timeout: float = 10.0) -> dict[str, Any]:
        if not _is_valid_mbid(recording_mbid):
            if recording_mbid:
                logger.debug("Rejected malformed recording MBID", recording_mbid=recording_mbid)
            return {}

        cached = _RECORDING_DETAIL_CACHE.get(recording_mbid)
        if cached is not None:
            return cached

        params = {"fmt": "json", "inc": _RECORDING_INC_SUPERSET}
        data = self.get(f"recording/{recording_mbid}", params=params, timeout=timeout)
        if data:
            _RECORDING_DETAIL_CACHE.set(recording_mbid, data)
        return data

    def get_artist(self, artist_mbid: str, inc: str = "", timeout: float = 10.0) -> dict[str, Any]:
        if not _is_valid_mbid(artist_mbid):
            if artist_mbid:
                logger.debug("Rejected malformed artist MBID", artist_mbid=artist_mbid)
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
            
        # Clean up braces/brackets often found in raw audio file tags
        clean_isrc = str(isrc).strip().strip("{}[]").upper()
        if not clean_isrc:
            return []

        cache_key = clean_isrc
        cached = _ISRC_LOOKUP_CACHE.get(cache_key)
        if cached is not None:
            return cached

        params = {"fmt": "json", "inc": _ISRC_INC_SUPERSET}
        
        # Use clean_isrc so the URL path never contains encoded curly braces
        payload = self.get(f"isrc/{clean_isrc}", params=params)
        recordings = payload.get("recordings", []) if isinstance(payload.get("recordings"), list) else []
        
        _ISRC_LOOKUP_CACHE.set(cache_key, recordings)
        return recordings

    def search_recordings_with_genres(self, query: str, limit: int = 25) -> list[dict[str, Any]]:
        return self.search_recordings(query, limit=limit, inc="genres")
