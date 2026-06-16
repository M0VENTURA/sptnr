"""AudioDB and ListenBrainz API client module.

This rewrite fixes the ListenBrainz popularity integration and endpoint consistency.

Key changes:
- Uses ListenBrainz API v1 consistently
- Supports global popularity via /1/popularity/recording
- Parses recommendations using payload.mbids
- Parses metadata/recording responses keyed by recording MBID
- Keeps backward-compatible wrappers
"""

from __future__ import annotations

import logging
import math
import os
import time
from datetime import datetime
from typing import Any, Optional

import requests

from . import session

logger = logging.getLogger(__name__)


# Optional shared rate limiter
try:
    from helpers.api_rate_limiter import get_rate_limiter
    _rate_limiter = get_rate_limiter()
except Exception:
    _rate_limiter = None


def _get_version() -> str:
    """Read version from VERSION file."""
    try:
        version_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), "VERSION")
        with open(version_file, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return "2.0.0-alpha"


# MusicBrainz-standard User-Agent (complies with https://musicbrainz.org/doc/MusicBrainz_API)
_DEFAULT_USER_AGENT = f"sptnr/{_get_version()} ( https://github.com/M0VENTURA/sptnr )"


class ListenBrainzError(Exception):
    """Raised for ListenBrainz-specific client errors."""


class ListenBrainzClient:
    """ListenBrainz API wrapper for public and optionally authenticated operations."""

    DEFAULT_BASE_URL = "https://api.listenbrainz.org/1"

    def __init__(
        self,
        http_session=None,
        enabled: bool = True,
        user_token: str = "",
        base_url: str = DEFAULT_BASE_URL,
        user_agent: str = "",
    ):
        self.session = http_session or session
        self.enabled = enabled
        self.user_token = user_token or ""
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent or _DEFAULT_USER_AGENT

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    def _headers(self, authenticated: bool = False) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        }
        if authenticated and self.user_token:
            headers["Authorization"] = f"Token {self.user_token}"
        return headers

    def _throttle(self) -> None:
        """Best-effort throttling. Falls back to a light sleep if no shared limiter exists."""
        if _rate_limiter:
            try:
                # Reuse the same limiter if your project already shares MB/LB traffic
                _rate_limiter.throttle_musicbrainz()
                return
            except Exception:
                pass
        time.sleep(1.0)

    def _get(
        self,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        authenticated: bool = False,
        timeout: tuple[int, int] = (5, 15),
    ) -> Any:
        if not self.enabled:
            raise ListenBrainzError("ListenBrainz client is disabled")

        self._throttle()
        url = f"{self.base_url}{path}"
        res = self.session.get(
            url,
            params=params,
            headers=self._headers(authenticated=authenticated),
            timeout=timeout,
        )
        res.raise_for_status()
        return res.json()

    def _post(
        self,
        path: str,
        *,
        payload: dict[str, Any],
        authenticated: bool = False,
        timeout: tuple[int, int] = (5, 15),
    ) -> Any:
        if not self.enabled:
            raise ListenBrainzError("ListenBrainz client is disabled")

        self._throttle()
        url = f"{self.base_url}{path}"
        res = self.session.post(
            url,
            json=payload,
            headers=self._headers(authenticated=authenticated),
            timeout=timeout,
        )
        res.raise_for_status()
        return res.json()

    # -------------------------------------------------------------------------
    # Popularity
    # -------------------------------------------------------------------------

    def get_recording_popularity_batch(
        self,
        recording_mbids: list[str],
    ) -> dict[str, dict[str, Optional[int]]]:
        """Fetch global ListenBrainz popularity for up to 100 recording MBIDs.

        Returns:
            {
                "<recording_mbid>": {
                    "total_listen_count": int | None,
                    "total_user_count": int | None
                },
                ...
            }
        """
        result = {
            mbid: {"total_listen_count": None, "total_user_count": None}
            for mbid in recording_mbids
            if mbid
        }

        if not self.enabled or not recording_mbids:
            return result

        mbids = [m for m in recording_mbids if m]
        if not mbids:
            return result

        if len(mbids) > 100:
            logger.warning("ListenBrainz popularity batch > 100 items; truncating to 100")
            mbids = mbids[:100]

        try:
            data = self._post(
                "/popularity/recording",
                payload={"recording_mbids": mbids},
                authenticated=False,
                timeout=(5, 20),
            )

            # The documented response is a list preserving request order.
            # We match by recording_mbid when available for robustness.
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        resp_mbid = item.get("recording_mbid")
                        if resp_mbid in result:
                            result[resp_mbid] = {
                                "total_listen_count": item.get("total_listen_count"),
                                "total_user_count": item.get("total_user_count"),
                            }
            elif isinstance(data, dict):
                for mbid in mbids:
                    if mbid in data and isinstance(data[mbid], dict):
                        result[mbid] = {
                            "total_listen_count": data[mbid].get("total_listen_count"),
                            "total_user_count": data[mbid].get("total_user_count"),
                        }
            else:
                logger.warning("Unexpected popularity response type: %s", type(data).__name__)

        except Exception as e:
            logger.debug("Failed to fetch recording popularity batch: %s", e)

        return result

    def get_recording_popularity(self, recording_mbid: str) -> dict[str, Optional[int]]:
        """Fetch global ListenBrainz popularity for a single recording MBID."""
        if not recording_mbid:
            return {"total_listen_count": None, "total_user_count": None}

        batch = self.get_recording_popularity_batch([recording_mbid])
        return batch.get(
            recording_mbid,
            {"total_listen_count": None, "total_user_count": None},
        )

    def get_listen_count(self, mbid: str = "", artist: str = "", title: str = "") -> int:
        """Backward-compatible helper.

        Returns the global ListenBrainz listen count for a recording MBID.
        If the MBID is missing or not found, returns 0.

        Notes:
            - This is NOT a normalized 'score'; it is the total global listen count.
            - 'artist' and 'title' are accepted only for backward-compatibility.
        """
        if not self.enabled or not mbid:
            return 0

        try:
            popularity = self.get_recording_popularity(mbid)
            count = popularity.get("total_listen_count")
            return int(count) if count is not None else 0
        except Exception:
            return 0

    def get_top_recordings_for_artist(self, artist_mbid: str) -> list[dict[str, Any]]:
        """Fetch top recordings for an artist via the ListenBrainz popularity API."""
        if not self.enabled or not artist_mbid:
            return []

        try:
            data = self._get(
                f"/popularity/top-recordings-for-artist/{artist_mbid}",
                authenticated=False,
                timeout=(5, 10),
            )
            return data if isinstance(data, list) else []
        except Exception as e:
            logger.debug("Failed to fetch top recordings for artist %s: %s", artist_mbid, e)
            return []

    def get_similar_artists(self, artist_mbid: str) -> list[dict[str, str]]:
        """Fetch similar artists from the ListenBrainz labs API."""
        if not self.enabled or not artist_mbid:
            return []

        try:
            self._throttle()
            url = "https://labs.api.listenbrainz.org/similar-artists/json"
            res = self.session.get(
                url,
                params={"artist_mbids": artist_mbid},
                headers=self._headers(authenticated=False),
                timeout=(5, 10),
            )
            res.raise_for_status()
            data = res.json()
            if data and "payload" in data:
                similar_records = data.get("payload", {}).get("artists", [])
                return [
                    {"name": record.get("artist_name", ""), "mbid": record.get("artist_mbid", "")}
                    for record in similar_records[:10]
                ]
            return []
        except Exception as e:
            logger.debug("Failed to fetch similar artists for %s: %s", artist_mbid, e)
            return []

    # -------------------------------------------------------------------------
    # Metadata
    # -------------------------------------------------------------------------

    def get_recording_metadata_batch(
        self,
        recording_mbids: list[str],
        inc: str = "artist release tag",
    ) -> dict[str, dict[str, Any]]:
        """Fetch recording metadata for one or more MBIDs.

        Returns the raw dict keyed by recording MBID.
        """
        if not self.enabled:
            return {}

        mbids = [m for m in recording_mbids if m]
        if not mbids:
            return {}

        try:
            data = self._get(
                "/metadata/recording/",
                params={
                    "recording_mbids": ",".join(mbids),
                    "inc": inc,
                },
                authenticated=False,
                timeout=(5, 20),
            )

            return data if isinstance(data, dict) else {}
        except Exception as e:
            logger.debug("Failed to fetch recording metadata: %s", e)
            return {}

    def get_recording_tags(self, mbid: str) -> list[dict[str, Any]]:
        """Get tags for a recording from metadata/recording."""
        if not mbid:
            return []

        try:
            metadata = self.get_recording_metadata_batch([mbid], inc="tag")
            entry = metadata.get(mbid, {})
            tags = entry.get("tag", {}).get("recording", [])
            if not isinstance(tags, list):
                return []
            return sorted(tags, key=lambda x: x.get("count", 0), reverse=True)
        except Exception as e:
            logger.debug("Failed to fetch recording tags for %s: %s", mbid, e)
            return []

    def get_artist_tags_from_recording(self, mbid: str) -> list[dict[str, Any]]:
        """Get artist tags associated with a recording from metadata/recording."""
        if not mbid:
            return []

        try:
            metadata = self.get_recording_metadata_batch([mbid], inc="tag")
            entry = metadata.get(mbid, {})
            tags = entry.get("tag", {}).get("artist", [])
            if not isinstance(tags, list):
                return []
            return sorted(tags, key=lambda x: x.get("count", 0), reverse=True)
        except Exception as e:
            logger.debug("Failed to fetch artist tags for %s: %s", mbid, e)
            return []

    # -------------------------------------------------------------------------
    # User data
    # -------------------------------------------------------------------------

    def get_user_listen_count(self, username: str) -> int:
        """Get total listen count for a user."""
        if not self.enabled or not username:
            return 0

        try:
            data = self._get(
                f"/user/{username}/listen-count",
                authenticated=False,
                timeout=(5, 10),
            )
            payload = data.get("payload", {}) if isinstance(data, dict) else {}
            count = payload.get("count", 0)
            return int(count) if count is not None else 0
        except Exception as e:
            logger.debug("Failed to fetch user listen count for %s: %s", username, e)
            return 0

    # -------------------------------------------------------------------------
    # Recommendations
    # -------------------------------------------------------------------------

    def get_recommendation_mbids(
        self,
        username: str,
        count: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Get raw collaborative-filtering recommendation MBIDs."""
        if not self.enabled or not username:
            return []

        count = max(1, min(100, int(count)))

        try:
            data = self._get(
                f"/cf/recommendation/user/{username}/recording",
                params={"count": count, "offset": offset},
                authenticated=False,
                timeout=(5, 20),
            )
            payload = data.get("payload", {}) if isinstance(data, dict) else {}
            mbids = payload.get("mbids", [])
            return mbids if isinstance(mbids, list) else []
        except Exception as e:
            logger.debug("Failed to fetch recommendation MBIDs for %s: %s", username, e)
            return []

    def get_created_for_playlists(self, username: str) -> dict[str, list[dict[str, Any]]]:
        """Fetch current CF recommendations and resolve them to track metadata.

        Notes:
            - Weekly exploration / last-week archive variants are not reconstructed here.
            - This method maps the current CF recommendation MBIDs to metadata and returns
              them under 'weekly_jams' for compatibility with your older code.
        """
        result = {
            "weekly_jams": [],
            "weekly_exploration": [],
            "last_week_jams": [],
            "last_week_exploration": [],
        }

        if not self.enabled or not username:
            return result

        try:
            rec_items = self.get_recommendation_mbids(username=username, count=200, offset=0)
            rec_mbids = [
                item.get("recording_mbid", "")
                for item in rec_items
                if isinstance(item, dict) and item.get("recording_mbid")
            ]
            if not rec_mbids:
                return result

            metadata = self.get_recording_metadata_batch(rec_mbids, inc="artist release")

            tracks = []
            for item in rec_items:
                if not isinstance(item, dict):
                    continue

                recording_mbid = item.get("recording_mbid", "")
                if not recording_mbid:
                    continue

                entry = metadata.get(recording_mbid, {})
                recording = entry.get("recording", {}) if isinstance(entry, dict) else {}
                artist = entry.get("artist", {}) if isinstance(entry, dict) else {}
                release = entry.get("release", {}) if isinstance(entry, dict) else {}

                track = {
                    "artist_name": artist.get("name", ""),
                    "track_name": recording.get("title", "") or recording.get("name", ""),
                    "release_name": release.get("name", ""),
                    "recording_mbid": recording_mbid,
                    "release_mbid": release.get("mbid", ""),
                    "score": item.get("score"),
                    "source": "listenbrainz-cf",
                }
                tracks.append(track)

            result["weekly_jams"] = tracks
            return result

        except Exception as e:
            logger.error("Failed to fetch created-for playlists for %s: %s", username, e)
            return result

    def get_weekly_jams(self, username: str) -> list[dict[str, Any]]:
        return self.get_created_for_playlists(username).get("weekly_jams", [])

    def get_weekly_exploration(self, username: str) -> list[dict[str, Any]]:
        # Kept for compatibility; not separately built here
        return self.get_created_for_playlists(username).get("weekly_exploration", [])

    def get_last_week_jams(self, username: str) -> list[dict[str, Any]]:
        return self.get_created_for_playlists(username).get("last_week_jams", [])

    def get_last_week_exploration(self, username: str) -> list[dict[str, Any]]:
        return self.get_created_for_playlists(username).get("last_week_exploration", [])


class ListenBrainzUserClient(ListenBrainzClient):
    """Authenticated ListenBrainz operations."""

    def __init__(self, user_token: str, http_session=None, enabled: bool = True, user_agent: str = ""):
        super().__init__(
            http_session=http_session,
            enabled=enabled,
            user_token=user_token,
            user_agent=user_agent,
        )

    def love_track(self, mbid: str) -> bool:
        """Mark a recording as loved."""
        if not mbid or not self.user_token:
            return False

        try:
            self._post(
                "/feedback/recording-feedback",
                payload={"recording_mbid": mbid, "score": 1},
                authenticated=True,
            )
            logger.info("Marked %s as loved on ListenBrainz", mbid)
            return True
        except Exception as e:
            logger.error("Failed to love track %s: %s", mbid, e)
            return False

    def unlove_track(self, mbid: str) -> bool:
        """Remove feedback from a recording."""
        if not mbid or not self.user_token:
            return False

        try:
            self._post(
                "/feedback/recording-feedback",
                payload={"recording_mbid": mbid, "score": 0},
                authenticated=True,
            )
            logger.info("Removed love/feedback for %s on ListenBrainz", mbid)
            return True
        except Exception as e:
            logger.error("Failed to remove feedback for %s: %s", mbid, e)
            return False

    def get_feedback(
        self,
        username: str,
        score: Optional[int] = None,
        count: int = 100,
        offset: int = 0,
        metadata: bool = False,
    ) -> list[dict[str, Any]]:
        """Get feedback for a user.

        score:
            1  -> loved recordings
           -1  -> hated recordings
          None -> all feedback
        """
        if not username:
            return []

        params: dict[str, Any] = {
            "count": max(1, min(100, int(count))),
            "offset": max(0, int(offset)),
            "metadata": "true" if metadata else "false",
        }
        if score is not None:
            params["score"] = int(score)

        try:
            data = self._get(
                f"/feedback/user/{username}/get-feedback",
                params=params,
                authenticated=False,
                timeout=(5, 20),
            )

            payload = data.get("payload", {}) if isinstance(data, dict) else {}
            feedback = payload.get("feedback", [])
            return feedback if isinstance(feedback, list) else []
        except Exception as e:
            logger.error("Failed to get feedback for %s: %s", username, e)
            return []

    def get_loved_tracks(
        self,
        username: str,
        limit: int = 100,
        offset: int = 0,
        metadata: bool = False,
    ) -> list[dict[str, Any]]:
        """Get tracks the user has loved."""
        return self.get_feedback(
            username=username,
            score=1,
            count=limit,
            offset=offset,
            metadata=metadata,
        )


class AudioDbClient:
    """TheAudioDB API wrapper for artist genres."""

    def __init__(self, api_key: str, http_session=None, enabled: bool = True):
        self.api_key = api_key
        self.session = http_session or session
        self.enabled = enabled
        self.base_url = "https://theaudiodb.com/api/v1/json"

    def get_artist_genres(self, artist: str) -> list[str]:
        if not self.enabled or not self.api_key or not artist:
            return []

        try:
            url = f"{self.base_url}/{self.api_key}/search.php"
            res = self.session.get(url, params={"s": artist}, timeout=(5, 10))
            res.raise_for_status()

            data = res.json().get("artists", [])
            if isinstance(data, list) and data and isinstance(data[0], dict):
                genre = data[0].get("strGenre")
                return [genre] if genre else []
            return []
        except Exception as e:
            logger.warning("AudioDB lookup failed for '%s': %s", artist, e)
            return []


def score_by_age(playcount: int | float, release_str: str) -> tuple[float, int]:
    """Apply age decay to a playcount-like metric."""
    try:
        # Handle year-only strings (e.g. "2004")
        if len(release_str) == 4 and release_str.isdigit():
            release_date = datetime.strptime(release_str, "%Y")
        else:
            release_date = datetime.strptime(release_str, "%Y-%m-%d")
        days_since = max((datetime.now() - release_date).days, 30)
        capped_days = min(days_since, 5 * 365)
        decay = 1 / math.log2(capped_days + 2)
        return playcount * decay, days_since
    except Exception:
        return 0.0, 9999


# -----------------------------------------------------------------------------
# Backward-compatible module functions
# -----------------------------------------------------------------------------

_listenbrainz_client: Optional[ListenBrainzClient] = None
_audiodb_client: Optional[AudioDbClient] = None


def _get_listenbrainz_client(
    enabled: bool = True,
    user_token: str = "",
) -> ListenBrainzClient:
    global _listenbrainz_client
    if _listenbrainz_client is None:
        _listenbrainz_client = ListenBrainzClient(enabled=enabled, user_token=user_token)
    return _listenbrainz_client


def _get_audiodb_client(api_key: str, enabled: bool = True) -> AudioDbClient:
    global _audiodb_client
    if _audiodb_client is None:
        _audiodb_client = AudioDbClient(api_key=api_key, enabled=enabled)
    return _audiodb_client


def get_recording_popularity_batch(
    recording_mbids: list[str],
    user_agent: str = "",
) -> dict[str, dict[str, Optional[int]]]:
    """Backward-compatible wrapper for batch recording popularity."""
    client = ListenBrainzClient(enabled=True, user_agent=user_agent or _DEFAULT_USER_AGENT)
    return client.get_recording_popularity_batch(recording_mbids)


def get_listenbrainz_score(
    mbid: str,
    artist: str = "",
    title: str = "",
    enabled: bool = True,
) -> int:
    """Backward-compatible wrapper.

    Returns the global total_listen_count for a recording MBID.
    """
    client = _get_listenbrainz_client(enabled=enabled)
    return client.get_listen_count(mbid=mbid, artist=artist, title=title)


def get_listenbrainz_popularity(
    mbid: str,
    enabled: bool = True,
) -> dict[str, Optional[int]]:
    """New convenience wrapper returning both popularity fields."""
    client = _get_listenbrainz_client(enabled=enabled)
    return client.get_recording_popularity(mbid)


def get_recording_tags(
    mbid: str,
    enabled: bool = True,
) -> list[dict[str, Any]]:
    """Backward-compatible convenience wrapper for recording tags."""
    client = _get_listenbrainz_client(enabled=enabled)
    return client.get_recording_tags(mbid)


def get_audiodb_genres(
    artist: str,
    api_key: str = "",
    enabled: bool = True,
) -> list[str]:
    client = _get_audiodb_client(api_key, enabled)
    return client.get_artist_genres(artist)