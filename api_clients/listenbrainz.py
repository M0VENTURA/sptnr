"""ListenBrainz API client.

Handles ListenBrainz HTTP operations including popularity, metadata, and user interactions.
"""

from __future__ import annotations

import logging
import math
import os
import time
from datetime import datetime
from typing import Any

from api_clients import session

logger = logging.getLogger(__name__)

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


class ListenBrainzClient:
    """ListenBrainz public API wrapper."""

    DEFAULT_BASE_URL = "https://api.listenbrainz.org/1"

    def __init__(self, http_session=None, enabled: bool = True, user_token: str = "", base_url: str = DEFAULT_BASE_URL, user_agent: str = ""):
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

    def _throttle(self) -> None:
        """Best-effort pacing on ListenBrainz's OWN rate budget.

        ListenBrainz and MusicBrainz are separate services with independent
        rate buckets, so LB requests must NOT consume the MusicBrainz 1 req/s
        budget — this lets concurrent scans push MB metadata lookups and LB
        popularity lookups at the same time.
        """
        if _rate_limiter:
            try:
                _rate_limiter.throttle_listenbrainz()
                return
            except Exception:
                pass
        time.sleep(1.0)

    def _get(self, path: str, *, params: dict[str, Any] | None = None, authenticated: bool = False, timeout: float = 15.0) -> Any:
        if not self.enabled:
            raise ListenBrainzError("ListenBrainz client is disabled")
        self._throttle()
        response = self.session.get(f"{self.base_url}{path}", params=params, headers=self._headers(authenticated), timeout=timeout)
        response.raise_for_status()
        return response.json()

    def _post(self, path: str, *, payload: dict[str, Any], authenticated: bool = False, timeout: float = 15.0) -> Any:
        if not self.enabled:
            raise ListenBrainzError("ListenBrainz client is disabled")
        self._throttle()
        response = self.session.post(f"{self.base_url}{path}", json=payload, headers=self._headers(authenticated), timeout=timeout)
        response.raise_for_status()
        return response.json()

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
            logger.debug("Failed to fetch recording popularity batch: %s", exc)
        return result

    def get_recording_popularity(self, recording_mbid: str) -> dict[str, int | None]:
        """Fetch global popularity for a single recording MBID."""
        if not recording_mbid:
            return {"total_listen_count": None, "total_user_count": None}
        return self.get_recording_popularity_batch([recording_mbid]).get(recording_mbid, {"total_listen_count": None, "total_user_count": None})

    def get_listen_count(self, mbid: str = "", artist: str = "", title: str = "") -> int:
        """Backward-compatible global listen count helper."""
        if not self.enabled or not mbid:
            return 0
        try:
            count = self.get_recording_popularity(mbid).get("total_listen_count")
            return int(count) if count is not None else 0
        except Exception:
            return 0

    def get_recording_metadata_batch(self, recording_mbids: list[str], inc: str = "artist release tag") -> dict[str, dict[str, Any]]:
        """Fetch recording metadata keyed by recording MBID."""
        mbids = [m for m in recording_mbids if m]
        if not self.enabled or not mbids:
            return {}
        try:
            data = self._get("/metadata/recording/", params={"recording_mbids": ",".join(mbids), "inc": inc}, timeout=20.0)
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            logger.debug("Failed to fetch recording metadata: %s", exc)
            return {}

    def get_release_metadata_batch(self, release_mbids: list[str], inc: str = "artist release") -> dict[str, dict[str, Any]]:
        """Fetch release metadata (incl. tracklists) keyed by release MBID.

        The ``inc`` fields include the tracklist (``media`` → ``tracks`` with
        ``title`` + ``recording_mbid``), used to match local tracks to
        ListenBrainz recordings by title when no recording MBID is known.
        """
        mbids = [m for m in release_mbids if m]
        if not self.enabled or not mbids:
            return {}
        try:
            data = self._get("/metadata/release/", params={"release_mbids": ",".join(mbids), "inc": inc}, timeout=20.0)
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            logger.debug("Failed to fetch release metadata: %s", exc)
            return {}

    def get_recording_tags(self, mbid: str) -> list[dict[str, Any]]:
        """Get recording tags."""
        if not mbid:
            return []
        try:
            entry = self.get_recording_metadata_batch([mbid], inc="tag").get(mbid, {})
            tags = entry.get("tag", {}).get("recording", []) if isinstance(entry, dict) else []
            return sorted(tags, key=lambda item: item.get("count", 0), reverse=True) if isinstance(tags, list) else []
        except Exception as exc:
            logger.debug("Failed to fetch recording tags for %s: %s", mbid, exc)
            return []

    def get_top_recordings_for_artist(self, artist_mbid: str) -> list[dict[str, Any]]:
        """Fetch top recordings for an artist."""
        if not self.enabled or not artist_mbid:
            return []
        try:
            data = self._get(f"/popularity/top-recordings-for-artist/{artist_mbid}", timeout=10.0)
            return data if isinstance(data, list) else []
        except Exception as exc:
            logger.debug("Failed to fetch top recordings for artist %s: %s", artist_mbid, exc)
            return []

    def get_user_listen_count(self, username: str) -> int:
        """Get total listen count for a user."""
        if not self.enabled or not username:
            return 0
        try:
            data = self._get(f"/user/{username}/listen-count", timeout=10.0)
            count = data.get("payload", {}).get("count", 0) if isinstance(data, dict) else 0
            return int(count or 0)
        except Exception as exc:
            logger.debug("Failed to fetch user listen count for %s: %s", username, exc)
            return 0

    # ------------------------------------------------------------------
    # Artist / Release / Release-Group popularity batch lookups
    # ------------------------------------------------------------------

    def get_artist_popularity_batch(self, artist_mbids: list[str]) -> dict[str, dict[str, int | None]]:
        """Fetch listen counts for up to 100 artist MBIDs.

        POST /1/popularity/artist
        """
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
            logger.debug("Failed to fetch artist popularity batch: %s", exc)
        return result

    def get_release_popularity_batch(self, release_mbids: list[str]) -> dict[str, dict[str, int | None]]:
        """Fetch listen counts for up to 100 release MBIDs.

        POST /1/popularity/release
        """
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
            logger.debug("Failed to fetch release popularity batch: %s", exc)
        return result

    def get_release_group_popularity_batch(self, release_group_mbids: list[str]) -> dict[str, dict[str, int | None]]:
        """Fetch listen counts for up to 100 release-group MBIDs.

        POST /1/popularity/release-group
        """
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
            logger.debug("Failed to fetch release-group popularity batch: %s", exc)
        return result

    def get_top_release_groups_for_artist(self, artist_mbid: str) -> list[dict[str, Any]]:
        """Fetch top release groups by listen count for an artist.

        GET /1/popularity/top-release-groups-for-artist/<mbid>
        """
        if not self.enabled or not artist_mbid:
            return []
        try:
            data = self._get(f"/popularity/top-release-groups-for-artist/{artist_mbid}", timeout=10.0)
            return data if isinstance(data, list) else []
        except Exception as exc:
            logger.debug("Failed to fetch top release groups for artist %s: %s", artist_mbid, exc)
            return []

    def get_similar_artists(self, artist_mbid: str, limit: int = 10) -> list[dict[str, str]]:
        """Fetch similar artists from the ListenBrainz labs API.

        GET https://labs.api.listenbrainz.org/similar-artists/json?artist_mbids=<mbid>

        Returns up to ``limit`` dicts with ``name`` and ``mbid`` keys.
        """
        if not self.enabled or not artist_mbid:
            return []
        try:
            self._throttle()
            url = "https://labs.api.listenbrainz.org/similar-artists/json"
            response = self.session.get(
                url,
                params={"artist_mbids": artist_mbid},
                headers=self._headers(authenticated=False),
                timeout=(5, 10),
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
            logger.debug("Failed to fetch similar artists for %s: %s", artist_mbid, exc)
            return []

    # ------------------------------------------------------------------
    # User listening history
    # ------------------------------------------------------------------

    def get_user_listens(self, username: str, min_ts: int | None = None, max_ts: int | None = None, count: int = 25) -> dict[str, Any]:
        """Fetch listening history for a user.

        GET /1/user/<username>/listens
        """
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
            logger.debug("Failed to fetch listens for %s: %s", username, exc)
            return {"payload": {"listens": []}}


class ListenBrainzUserClient(ListenBrainzClient):
    """Authenticated ListenBrainz operations."""

    def __init__(self, user_token: str, http_session=None, enabled: bool = True, user_agent: str = ""):
        super().__init__(http_session=http_session, enabled=enabled, user_token=user_token, user_agent=user_agent)

    def love_track(self, mbid: str) -> bool:
        """Mark a recording as loved."""
        if not mbid or not self.user_token:
            return False
        try:
            self._post("/feedback/recording-feedback", payload={"recording_mbid": mbid, "score": 1}, authenticated=True)
            return True
        except Exception as exc:
            logger.error("Failed to love track %s: %s", mbid, exc)
            return False

    def unlove_track(self, mbid: str) -> bool:
        """Remove feedback from a recording."""
        if not mbid or not self.user_token:
            return False
        try:
            self._post("/feedback/recording-feedback", payload={"recording_mbid": mbid, "score": 0}, authenticated=True)
            return True
        except Exception as exc:
            logger.error("Failed to remove feedback for %s: %s", mbid, exc)
            return False

    # ------------------------------------------------------------------
    # Recording recommendations (collaborative filtering)
    # ------------------------------------------------------------------

    def get_recommendations(self, count: int = 25, offset: int = 0) -> dict[str, Any]:
        """Fetch recommended recordings for the authenticated user.

        GET /1/cf/recommendation/user/<username>/recording

        Args:
            count: Number of recommendations to return.
            offset: Pagination offset.

        Returns:
            Dict with ``payload`` containing recommended MBIDs and scores.
        """
        if not self.user_token:
            return {"payload": {"mbids": []}}
        try:
            # Username not needed in path when token identifies the user
            data = self._get(f"/cf/recommendation/user/{self.user_token}/recording",
                             params={"count": max(1, min(count, 100)), "offset": max(0, offset)},
                             authenticated=True,
                             timeout=30.0)
            return data if isinstance(data, dict) else {"payload": {"mbids": []}}
        except Exception as exc:
            logger.debug("Failed to fetch recommendations: %s", exc)
            return {"payload": {"mbids": []}}

    def get_user_feedback(self, username: str, score: int | None = None, count: int = 25, offset: int = 0) -> dict[str, Any]:
        """Get feedback (loves/hates) given by a user.

        GET /1/feedback/user/<username>/get-feedback

        Args:
            username: ListenBrainz username.
            score: Optional filter — 1 for loved, -1 for hated.
            count: Number of items to return.
            offset: Pagination offset.

        Returns:
            Dict with feedback items array.
        """
        if not self.enabled or not username:
            return {"feedback": []}
        params: dict[str, Any] = {"count": max(1, min(count, 100)), "offset": max(0, offset)}
        if score is not None:
            params["score"] = score
        try:
            data = self._get(f"/feedback/user/{username}/get-feedback", params=params, authenticated=True, timeout=15.0)
            return data if isinstance(data, dict) else {"feedback": []}
        except Exception as exc:
            logger.debug("Failed to fetch user feedback for %s: %s", username, exc)
            return {"feedback": []}


def score_by_age(playcount: int | float, release_str: str) -> tuple[float, int]:
    """Apply age decay to a playcount-like metric.
    
    Delegates to the canonical implementation in services.popularity.popularity_math."""
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
    """Fetch release metadata (incl. tracklists) keyed by release MBID.

    Module-level wrapper for ``ListenBrainzClient.get_release_metadata_batch``
    — the release-first path of the popularity scan (album-tracklist lookup).
    """
    return ListenBrainzClient(enabled=True, user_agent=DEFAULT_USER_AGENT).get_release_metadata_batch(release_mbids, inc=inc)


def get_listenbrainz_score(mbid: str, artist: str = "", title: str = "", enabled: bool = True) -> int:
    return _get_listenbrainz_client(enabled=enabled).get_listen_count(mbid=mbid, artist=artist, title=title)


def get_listenbrainz_popularity(mbid: str, enabled: bool = True) -> dict[str, int | None]:
    return _get_listenbrainz_client(enabled=enabled).get_recording_popularity(mbid)


def get_recording_tags(mbid: str, enabled: bool = True) -> list[dict[str, Any]]:
    return _get_listenbrainz_client(enabled=enabled).get_recording_tags(mbid)


def get_recording_tags_batch(recording_mbids: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Fetch tags for many recordings in ONE metadata call.

    Returns ``{recording_mbid: [{"tag", "count"}, ...]}`` sorted by count —
    the same per-recording shape ``get_recording_tags`` returns.  The scan
    runner uses this to serve an album's genre collection from a single
    request instead of one throttled call per track.
    """
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
        logger.debug("Failed to fetch recording tags batch: %s", exc)
        return {}