"""ListenBrainz API client.

Handles ListenBrainz HTTP operations including popularity, metadata, and user interactions.
Modernized with strict thread-safe throttling and structured logging.

Retry policy: the shared ``api_clients.session`` (``_RetryTransport``) is the
SINGLE retry authority — it already retries 3× with a cumulative 40s wait
budget for 429/502/503/504 and network errors.  Do NOT add a second tenacity
``@retry`` layer here; stacking two retry loops (outer app-level + inner
transport) multiplied worst-case latency per call (~350s vs ~80s) and was a
major contributor to scan-stage "budget exceeded" stalls.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

import httpx
import structlog

from api_clients import session

logger = structlog.get_logger(__name__)

try:
    from services.infrastructure.api_rate_limiter import get_rate_limiter
    _rate_limiter = get_rate_limiter()
except Exception:
    _rate_limiter = None


def _get_version() -> str:
    """Read app version for User-Agent."""
    try:
        version_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), "VERSION")
        with open(version_file, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except Exception:
        return "2.0.0-alpha"


DEFAULT_USER_AGENT = f"Popularr/{_get_version()} ( https://github.com/M0VENTURA/Popularr )"


class ListenBrainzError(Exception):
    """Raised for ListenBrainz client errors."""


# =============================================================================
# STRICT RATE LIMITING
# =============================================================================

_THROTTLE_LOCK = threading.Lock()
_LAST_LB_REQUEST_TIME = 0.0


def _strict_throttle() -> None:
    """Best-effort pacing on ListenBrainz's OWN rate budget.

    ListenBrainz and MusicBrainz are separate services with independent
    rate buckets, so LB requests must NOT consume the MusicBrainz 1 req/s
    budget. A thread-locked turnstile prevents concurrent worker bursts.
    """
    global _LAST_LB_REQUEST_TIME
    
    if _rate_limiter:
        try:
            _rate_limiter.throttle_listenbrainz()
            return
        except Exception:
            pass
            
    # Fallback thread-safe throttle (approx 1 req/sec)
    with _THROTTLE_LOCK:
        now = time.monotonic()
        elapsed = now - _LAST_LB_REQUEST_TIME
        if elapsed < 1.0:
            time.sleep(1.0 - elapsed)
        _LAST_LB_REQUEST_TIME = time.monotonic()


# =============================================================================
# HTTP CLIENT
# =============================================================================

class ListenBrainzClient:
    """ListenBrainz public API wrapper."""

    DEFAULT_BASE_URL = "https://api.listenbrainz.org/1"

    def __init__(
        self, 
        http_session: Any = None, 
        enabled: bool = True, 
        user_token: str = "", 
        base_url: str = DEFAULT_BASE_URL, 
        user_agent: str = ""
    ):
        self.session = http_session or session
        self.enabled = enabled
        self.user_token = user_token or ""
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent or DEFAULT_USER_AGENT

    def _headers(self, authenticated: bool = False) -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": self.user_agent}
        if authenticated and self.user_token:
            headers["Authorization"] = f"Token {self.user_token}"
        return headers

    def _get(self, path: str, *, params: dict[str, Any] | None = None, authenticated: bool = False, timeout: float = 15.0) -> Any:
        if not self.enabled:
            raise ListenBrainzError("ListenBrainz client is disabled")
        _strict_throttle()
        response = self.session.get(
            f"{self.base_url}{path}",
            params=params,
            headers=self._headers(authenticated),
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()

    def _post(self, path: str, *, payload: dict[str, Any], authenticated: bool = False, timeout: float = 15.0) -> Any:
        if not self.enabled:
            raise ListenBrainzError("ListenBrainz client is disabled")
        _strict_throttle()
        response = self.session.post(
            f"{self.base_url}{path}",
            json=payload,
            headers=self._headers(authenticated),
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()

    # ------------------------------------------------------------------
    # Batch & Popularity Lookups
    # ------------------------------------------------------------------

    def get_recording_popularity_batch(self, recording_mbids: list[str]) -> dict[str, dict[str, int | None]]:
        """Fetch global ListenBrainz popularity for up to 100 recording MBIDs."""
        mbids = [m for m in recording_mbids if m]
        result: dict[str, dict[str, int | None]] = {}

        for mbid in mbids:
            result[mbid] = {
                "total_listen_count": None,
                "total_user_count": None,
            }
            
        if not self.enabled or not mbids:
            return result
            
        if len(mbids) > 100:
            logger.warning("ListenBrainz popularity batch > 100 items; truncating to 100")
            mbids = mbids[:100]
            
        try:
            data = self._post("/popularity/recording", payload={"recording_mbids": mbids}, timeout=20.0)
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get("recording_mbid") in result:
                        result[item["recording_mbid"]] = {
                            "total_listen_count": item.get("total_listen_count"),
                            "total_user_count": item.get("total_user_count"),
                        }
            elif isinstance(data, dict):
                for mbid in mbids:
                    if isinstance(data.get(mbid), dict):
                        result[mbid] = {
                            "total_listen_count": data[mbid].get("total_listen_count"),
                            "total_user_count": data[mbid].get("total_user_count"),
                        }
        except Exception as exc:
            logger.debug("Failed to fetch recording popularity batch", error=str(exc))
        return result

    def get_recording_popularity(self, recording_mbid: str) -> dict[str, int | None]:
        if not recording_mbid:
            return {"total_listen_count": None, "total_user_count": None}
        return self.get_recording_popularity_batch([recording_mbid]).get(
            recording_mbid, {"total_listen_count": None, "total_user_count": None}
        )

    def get_listen_count(self, mbid: str = "", artist: str = "", title: str = "") -> int:
        if not self.enabled or not mbid:
            return 0
        try:
            count = self.get_recording_popularity(mbid).get("total_listen_count")
            return int(count) if count is not None else 0
        except Exception:
            return 0

    def get_recording_metadata_batch(self, recording_mbids: list[str], inc: str = "artist release tag") -> dict[str, dict[str, Any]]:
        mbids = [m for m in recording_mbids if m]
        if not self.enabled or not mbids:
            return {}
        try:
            data = self._get("/metadata/recording/", params={"recording_mbids": ",".join(mbids), "inc": inc}, timeout=20.0)
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            logger.debug("Failed to fetch recording metadata", error=str(exc))
            return {}

    def get_release_metadata_batch(self, release_mbids: list[str], inc: str = "artist release") -> dict[str, dict[str, Any]]:
        mbids = [m for m in release_mbids if m]
        if not self.enabled or not mbids:
            return {}
        try:
            data = self._get("/metadata/release/", params={"release_mbids": ",".join(mbids), "inc": inc}, timeout=20.0)
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            logger.debug("Failed to fetch release metadata", error=str(exc))
            return {}

    def get_recording_tags(self, mbid: str) -> list[dict[str, Any]]:
        if not mbid:
            return []
        try:
            entry = self.get_recording_metadata_batch([mbid], inc="tag").get(mbid, {})
            tags = entry.get("tag", {}).get("recording", []) if isinstance(entry, dict) else []
            return sorted(tags, key=lambda item: item.get("count", 0), reverse=True) if isinstance(tags, list) else []
        except Exception as exc:
            logger.debug("Failed to fetch recording tags", mbid=mbid, error=str(exc))
            return []

    def get_top_recordings_for_artist(self, artist_mbid: str) -> list[dict[str, Any]]:
        if not self.enabled or not artist_mbid:
            return []
        try:
            data = self._get(f"/popularity/top-recordings-for-artist/{artist_mbid}", timeout=10.0)
            return data if isinstance(data, list) else []
        except Exception as exc:
            logger.debug("Failed to fetch top recordings for artist", artist_mbid=artist_mbid, error=str(exc))
            return []

    def get_user_listen_count(self, username: str) -> int:
        if not self.enabled or not username:
            return 0
        try:
            data = self._get(f"/user/{username}/listen-count", timeout=10.0)
            count = data.get("payload", {}).get("count", 0) if isinstance(data, dict) else 0
            return int(count or 0)
        except Exception as exc:
            logger.debug("Failed to fetch user listen count", username=username, error=str(exc))
            return 0

    # ------------------------------------------------------------------
    # Artist / Release / Release-Group popularity batch lookups
    # ------------------------------------------------------------------

    def get_artist_popularity_batch(self, artist_mbids: list[str]) -> dict[str, dict[str, int | None]]:
        mbids = [m for m in artist_mbids if m]
        result: dict[str, dict[str, int | None]] = {m: {"total_listen_count": None, "total_user_count": None} for m in mbids}
        if not self.enabled or not mbids:
            return result
        if len(mbids) > 100:
            mbids = mbids[:100]
        try:
            data = self._post("/popularity/artist", payload={"artist_mbids": mbids}, timeout=20.0)
            if isinstance(data, list):
                for item in data:
                    mbid = item.get("artist_mbid") if isinstance(item, dict) else None
                    if mbid and mbid in result:
                        result[mbid] = {"total_listen_count": item.get("total_listen_count"), "total_user_count": item.get("total_user_count")}
        except Exception as exc:
            logger.debug("Failed to fetch artist popularity batch", error=str(exc))
        return result

    def get_release_popularity_batch(self, release_mbids: list[str]) -> dict[str, dict[str, int | None]]:
        mbids = [m for m in release_mbids if m]
        result: dict[str, dict[str, int | None]] = {m: {"total_listen_count": None, "total_user_count": None} for m in mbids}
        if not self.enabled or not mbids:
            return result
        if len(mbids) > 100:
            mbids = mbids[:100]
        try:
            data = self._post("/popularity/release", payload={"release_mbids": mbids}, timeout=20.0)
            if isinstance(data, list):
                for item in data:
                    mbid = item.get("release_mbid") if isinstance(item, dict) else None
                    if mbid and mbid in result:
                        result[mbid] = {"total_listen_count": item.get("total_listen_count"), "total_user_count": item.get("total_user_count")}
        except Exception as exc:
            logger.debug("Failed to fetch release popularity batch", error=str(exc))
        return result

    def get_release_group_popularity_batch(self, release_group_mbids: list[str]) -> dict[str, dict[str, int | None]]:
        mbids = [m for m in release_group_mbids if m]
        result: dict[str, dict[str, int | None]] = {m: {"total_listen_count": None, "total_user_count": None} for m in mbids}
        if not self.enabled or not mbids:
            return result
        if len(mbids) > 100:
            mbids = mbids[:100]
        try:
            data = self._post("/popularity/release-group", payload={"release_group_mbids": mbids}, timeout=20.0)
            if isinstance(data, list):
                for item in data:
                    mbid = item.get("release_group_mbid") if isinstance(item, dict) else None
                    if mbid and mbid in result:
                        result[mbid] = {"total_listen_count": item.get("total_listen_count"), "total_user_count": item.get("total_user_count")}
        except Exception as exc:
            logger.debug("Failed to fetch release-group popularity batch", error=str(exc))
        return result

    def get_top_release_groups_for_artist(self, artist_mbid: str) -> list[dict[str, Any]]:
        if not self.enabled or not artist_mbid:
            return []
        try:
            data = self._get(f"/popularity/top-release-groups-for-artist/{artist_mbid}", timeout=10.0)
            return data if isinstance(data, list) else []
        except Exception as exc:
            logger.debug("Failed to fetch top release groups for artist", artist_mbid=artist_mbid, error=str(exc))
            return []

    def get_similar_artists(self, artist_mbid: str, limit: int = 10) -> list[dict[str, str]]:
        if not self.enabled or not artist_mbid:
            return []
        try:
            _strict_throttle()
            url = "https://labs.api.listenbrainz.org/similar-artists/json"
            response = self.session.get(
                url,
                params={"artist_mbids": artist_mbid},
                headers=self._headers(authenticated=False),
                timeout=10.0,
            )
            response.raise_for_status()
            data = response.json()
            if isinstance(data, dict) and "payload" in data:
                similar_records = data.get("payload", {}).get("artists", []) or []
                return [
                    {"name": str(record.get("artist_name", "")), "mbid": str(record.get("artist_mbid", ""))}
                    for record in similar_records[:limit]
                    if record.get("artist_name")
                ]
            return []
        except Exception as exc:
            logger.debug("Failed to fetch similar artists", artist_mbid=artist_mbid, error=str(exc))
            return []

    def get_user_listens(self, username: str, min_ts: int | None = None, max_ts: int | None = None, count: int = 25) -> dict[str, Any]:
        if not self.enabled or not username:
            return {"payload": {"listens": []}}
        params: dict[str, Any] = {"count": max(1, min(count, 100))}
        if min_ts is not None:
            params["min_ts"] = min_ts
        if max_ts is not None:
            params["max_ts"] = max_ts
        try:
            return self._get(f"/user/{username}/listens", params=params, timeout=15.0)
        except Exception as exc:
            logger.debug("Failed to fetch listens", username=username, error=str(exc))
            return {"payload": {"listens": []}}


class ListenBrainzUserClient(ListenBrainzClient):
    """Authenticated ListenBrainz operations."""

    def __init__(self, user_token: str, http_session: Any = None, enabled: bool = True, user_agent: str = ""):
        super().__init__(http_session=http_session, enabled=enabled, user_token=user_token, user_agent=user_agent)

    def love_track(self, mbid: str) -> bool:
        if not mbid or not self.user_token:
            return False
        try:
            self._post("/feedback/recording-feedback", payload={"recording_mbid": mbid, "score": 1}, authenticated=True)
            return True
        except Exception as exc:
            logger.error("Failed to love track", mbid=mbid, error=str(exc))
            return False

    def unlove_track(self, mbid: str) -> bool:
        if not mbid or not self.user_token:
            return False
        try:
            self._post("/feedback/recording-feedback", payload={"recording_mbid": mbid, "score": 0}, authenticated=True)
            return True
        except Exception as exc:
            logger.error("Failed to remove feedback", mbid=mbid, error=str(exc))
            return False

    def get_recommendations(self, count: int = 25, offset: int = 0) -> dict[str, Any]:
        if not self.user_token:
            return {"payload": {"mbids": []}}
        try:
            data = self._get(
                f"/cf/recommendation/user/{self.user_token}/recording",
                params={"count": max(1, min(count, 100)), "offset": max(0, offset)},
                authenticated=True,
                timeout=30.0
            )
            return data if isinstance(data, dict) else {"payload": {"mbids": []}}
        except Exception as exc:
            logger.debug("Failed to fetch recommendations", error=str(exc))
            return {"payload": {"mbids": []}}

    def get_user_feedback(self, username: str, score: int | None = None, count: int = 25, offset: int = 0) -> dict[str, Any]:
        if not self.enabled or not username:
            return {"feedback": []}
        params: dict[str, Any] = {"count": max(1, min(count, 100)), "offset": max(0, offset)}
        if score is not None:
            params["score"] = score
        try:
            data = self._get(f"/feedback/user/{username}/get-feedback", params=params, authenticated=True, timeout=15.0)
            return data if isinstance(data, dict) else {"feedback": []}
        except Exception as exc:
            logger.debug("Failed to fetch user feedback", username=username, error=str(exc))
            return {"feedback": []}


# =============================================================================
# MODULE-LEVEL WRAPPERS
# =============================================================================

def score_by_age(playcount: int | float, release_str: str) -> tuple[float, int]:
    from services.popularity.popularity_math import score_by_age as _score_by_age
    return _score_by_age(playcount, release_str)


_listenbrainz_client: ListenBrainzClient | None = None

def _get_listenbrainz_client(enabled: bool = True, user_token: str = "") -> ListenBrainzClient:
    global _listenbrainz_client
    if _listenbrainz_client is None:
        _listenbrainz_client = ListenBrainzClient(enabled=enabled, user_token=user_token)
    return _listenbrainz_client


def get_recording_popularity_batch(recording_mbids: list[str], user_agent: str = "") -> dict[str, dict[str, int | None]]:
    return ListenBrainzClient(enabled=True, user_agent=user_agent or DEFAULT_USER_AGENT).get_recording_popularity_batch(recording_mbids)


def get_release_metadata_batch(release_mbids: list[str], inc: str = "artist release") -> dict[str, dict[str, Any]]:
    return ListenBrainzClient(enabled=True, user_agent=DEFAULT_USER_AGENT).get_release_metadata_batch(release_mbids, inc=inc)


def get_listenbrainz_score(mbid: str, artist: str = "", title: str = "", enabled: bool = True) -> int:
    return _get_listenbrainz_client(enabled=enabled).get_listen_count(mbid=mbid, artist=artist, title=title)


def get_listenbrainz_popularity(mbid: str, enabled: bool = True) -> dict[str, int | None]:
    return _get_listenbrainz_client(enabled=enabled).get_recording_popularity(mbid)


def get_recording_tags(mbid: str, enabled: bool = True) -> list[dict[str, Any]]:
    return _get_listenbrainz_client(enabled=enabled).get_recording_tags(mbid)


def get_recording_tags_batch(recording_mbids: list[str]) -> dict[str, list[dict[str, Any]]]:
    mbids = [m for m in recording_mbids if m]
    if not mbids:
        return {}
    try:
        data = _get_listenbrainz_client().get_recording_metadata_batch(mbids, inc="tag") or {}
        out: dict[str, list[dict[str, Any]]] = {}
        for mbid, entry in data.items():
            if not isinstance(entry, dict):
                continue
            tags = entry.get("tag", {}).get("recording", []) if isinstance(entry.get("tag"), dict) else []
            if isinstance(tags, list):
                out[mbid] = sorted(
                    tags,
                    key=lambda item: item.get("count", 0) if isinstance(item, dict) else 0,
                    reverse=True,
                )
        return out
    except Exception as exc:
        logger.debug("Failed to fetch recording tags batch", error=str(exc))
        return {}
