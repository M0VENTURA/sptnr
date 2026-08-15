"""Low-level MusicBrainz HTTP client.

Owns only MusicBrainz request mechanics:
- User-Agent
- throttling (1 request/sec per MusicBrainz policy)
- Lucene escaping
- raw endpoint wrappers
- lookup, search, browse, and non-MBID lookups

Business rules and DB writes belong in services/repositories.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

from api_clients import session
from services.infrastructure.api_rate_limiter import get_rate_limiter
logger = logging.getLogger(__name__)


def get_version() -> str:
    try:
        version_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), "VERSION")
        with open(version_file, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except Exception:
        return "2.0.0-alpha"


USER_AGENT = f"Popularr/{get_version()} ( https://github.com/M0VENTURA/Popularr )"
MUSICBRAINZ_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

try:

    _rate_limiter = get_rate_limiter()
except Exception:
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


# Process-wide lookup caches.  The same recording / ISRC is resolved by
# several independent code paths during one scan (popularity fallbacks,
# single detection, cover detection, genres, work-level LB aggregation) —
# each used to pay a full throttled MusicBrainz request.  These dicts are
# GIL-safe for concurrent reads/writes and bounded: payloads are small, and
# the cache is cleared when it grows past its cap so a long scan cannot
# balloon memory.
#
# Keyed by MBID / ISRC alone (NOT by (id, inc)): callers ask for different
# ``inc`` subsets (artist-credits+releases / genres+tags / work-rels / ...),
# and a (id, inc) key meant the SAME recording was fetched up to 5× per scan
# at the 1 req/s throttle.  A canonical superset is always requested, so a
# single call satisfies every caller's subset.
_RECORDING_INC_SUPERSET = "artist-credits+releases+work-rels+recording-rels+artist-rels+genres+tags"
_ISRC_INC_SUPERSET = "artist-credits+releases+work-rels"

_RECORDING_DETAIL_CACHE: dict[str, dict[str, Any]] = {}
_RECORDING_DETAIL_CACHE_MAX = 4000
_ISRC_LOOKUP_CACHE: dict[str, list[dict[str, Any]]] = {}
_ISRC_LOOKUP_CACHE_MAX = 2000

# Release lookups are fetched independently by the LB album-tracklist path,
# end-of-album tag-sync, cover detection and the compare / release-resolution
# flows — keyed by release MBID alone with a canonical inc superset so a
# single throttled call satisfies every caller (recordings for tracklists,
# artist-credits, media, release-groups).
_RELEASE_INC_SUPERSET = "recordings+artist-credits+media+release-groups+labels"
_RELEASE_DETAIL_CACHE: dict[str, dict[str, Any]] = {}
_RELEASE_DETAIL_CACHE_MAX = 2000


class MusicBrainzHttpClient:
    """Raw MusicBrainz API wrapper."""

    def __init__(self, http_session=None, enabled: bool = True):
        self.session = http_session or session
        self.enabled = enabled
        self.base_url = "https://musicbrainz.org/ws/2/"
        self.headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}

    def throttle(self) -> None:
        if _rate_limiter:
            try:
                _rate_limiter.throttle_musicbrainz()
                return
            except Exception:
                pass
        time.sleep(1.0)

    def get(self, endpoint: str, *, params: dict[str, Any] | None = None, timeout: float = 10.0) -> dict[str, Any]:
        if not self.enabled:
            return {}
        self.throttle()
        response = self.session.get(
            f"{self.base_url}{endpoint.lstrip('/')}",
            params=params or {},
            headers=self.headers,
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    def search_release_groups(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        payload = self.get("release-group/", params={"query": query, "fmt": "json", "limit": max(1, min(limit, 100))})
        return payload.get("release-groups", []) if isinstance(payload.get("release-groups"), list) else []

    def search_releases(self, query: str, limit: int = 10, inc: str = "") -> list[dict[str, Any]]:
        params = {"query": query, "fmt": "json", "limit": max(1, min(limit, 25))}
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
        params = {"query": query, "fmt": "json", "limit": max(1, min(limit, 25))}
        if inc:
            params["inc"] = inc
        payload = self.get("artist/", params=params)
        return payload.get("artists", []) if isinstance(payload.get("artists"), list) else []

    def get_artist_country(self, artist: str) -> str:
        """Return the readable country/area name for an artist (e.g. "United States").

        Uses the MusicBrainz ``area`` field — ``area.name`` is the display
        name, whereas the plain ``country`` field is only an ISO code and is
        often missing entirely.
        """
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
        # Cached by release MBID alone with a canonical inc superset — the
        # same release is fetched by the LB album-tracklist path, tag-sync,
        # cover detection and the compare / release-resolution flows with
        # overlapping inc sets; keying by (mbid, inc) paid a throttled call
        # per inc set.
        cached = _RELEASE_DETAIL_CACHE.get(release_mbid)
        if cached is not None:
            return cached
        params = {"fmt": "json", "inc": _RELEASE_INC_SUPERSET}
        data = self.get(f"release/{release_mbid}", params=params, timeout=timeout)
        if data:
            while len(_RELEASE_DETAIL_CACHE) >= _RELEASE_DETAIL_CACHE_MAX:
                try:
                    _RELEASE_DETAIL_CACHE.pop(next(iter(_RELEASE_DETAIL_CACHE)))
                except (StopIteration, KeyError):
                    break
            _RELEASE_DETAIL_CACHE[release_mbid] = data
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
        # Cached process-wide by MBID alone with a canonical inc superset —
        # the same recording is fetched by metadata, genres, single detection,
        # cover detection and work-level LB aggregation with overlapping inc
        # sets; keying by (mbid, inc) fragmented the cache and paid a
        # throttled request per inc set.
        cached = _RECORDING_DETAIL_CACHE.get(recording_mbid)
        if cached is not None:
            return cached
        params = {"fmt": "json", "inc": _RECORDING_INC_SUPERSET}
        data = self.get(f"recording/{recording_mbid}", params=params, timeout=timeout)
        if data:
            while len(_RECORDING_DETAIL_CACHE) >= _RECORDING_DETAIL_CACHE_MAX:
                try:
                    _RECORDING_DETAIL_CACHE.pop(next(iter(_RECORDING_DETAIL_CACHE)))
                except (StopIteration, KeyError):
                    break
            _RECORDING_DETAIL_CACHE[recording_mbid] = data
        return data

    def get_artist(self, artist_mbid: str, inc: str = "", timeout: float = 10.0) -> dict[str, Any]:
        if not artist_mbid:
            return {}
        params = {"fmt": "json"}
        if inc:
            params["inc"] = inc
        return self.get(f"artist/{artist_mbid}", params=params, timeout=timeout)

    def get_artist_members(self, artist_mbid: str, timeout: float = 10.0) -> list[dict[str, Any]]:
        """Fetch band members/relations for a MusicBrainz artist.

        Returns a list of dicts with keys: name, relation_type, begin, end, attributes, ended.
        """
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
                # Direction "backward" → this artist is the object (band), rel.artist is the subject (member)
                # Direction "forward" → this artist is the subject, rel.artist is the object
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
        """Browse every recording linked to a MusicBrainz Work.

        Singles splinter their ListenBrainz scrobbles across the album cut,
        the 7" single edit, Greatest Hits masters and radio promos — each is
        a separate recording that links to the same Work.  Browsing
        ``recording?work=<mbid>`` returns them all in one throttled call
        (the Work-level ListenBrainz aggregation relies on this).
        """
        if not self.enabled or not work_mbid:
            return []
        params = {"fmt": "json", "work": work_mbid, "limit": min(limit, 100)}
        if inc:
            params["inc"] = inc
        payload = self.get("recording", params=params)
        return payload.get("recordings", []) if isinstance(payload.get("recordings"), list) else []

    # ------------------------------------------------------------------
    # Browse endpoints (efficient lookups by linked entity)
    # ------------------------------------------------------------------

    def browse_artist_releases(self, artist_mbid: str, inc: str = "", limit: int = 25, offset: int = 0) -> dict[str, Any]:
        """Browse releases for an artist MBID. Supports paging via offset."""
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
        """Browse release-groups for an artist MBID."""
        params = {"fmt": "json", "artist": artist_mbid, "limit": min(limit, 100), "offset": offset}
        if inc:
            params["inc"] = inc
        payload = self.get("release-group", params=params)
        return {
            "release_groups": payload.get("release-groups", []) or [],
            "release_group_count": payload.get("release-group-count", 0) or 0,
            "release_group_offset": payload.get("release-group-offset", offset) or offset,
        }

    # ------------------------------------------------------------------
    # Non-MBID lookups (ISRC, discid, ISWC)
    # ------------------------------------------------------------------

    def lookup_by_isrc(self, isrc: str, inc: str = "") -> list[dict[str, Any]]:
        """Lookup recordings by ISRC code.

        Cached process-wide by ISRC alone (canonical inc superset) — the same
        code is resolved up to 5× per track per scan (Last.fm fallback, LB
        MBID resolution, LB aggregation, ISRC single check, cover detection),
        each previously a full throttled request.
        """
        if not isrc:
            return []
        cache_key = isrc.strip().upper()
        cached = _ISRC_LOOKUP_CACHE.get(cache_key)
        if cached is not None:
            return cached
        params = {"fmt": "json", "inc": _ISRC_INC_SUPERSET}
        payload = self.get(f"isrc/{isrc}", params=params)
        recordings = payload.get("recordings", []) if isinstance(payload.get("recordings"), list) else []
        while len(_ISRC_LOOKUP_CACHE) >= _ISRC_LOOKUP_CACHE_MAX:
            try:
                _ISRC_LOOKUP_CACHE.pop(next(iter(_ISRC_LOOKUP_CACHE)))
            except (StopIteration, KeyError):
                break
        _ISRC_LOOKUP_CACHE[cache_key] = recordings
        return recordings

    # ------------------------------------------------------------------
    # Genre-aware lookups (inc=genres)
    # ------------------------------------------------------------------

    def search_recordings_with_genres(self, query: str, limit: int = 25) -> list[dict[str, Any]]:
        """Search recordings including MusicBrainz genre data."""
        return self.search_recordings(query, limit=limit, inc="genres")
