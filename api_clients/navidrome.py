"""Navidrome API client.

This module is intentionally HTTP-focused.

Responsibilities kept here:
- Build Subsonic/Navidrome request parameters.
- Call Navidrome HTTP endpoints.
- Return raw/near-raw API data.
- Playlist/star/scan endpoint wrappers.

Responsibilities moved out:
- Artist-index orchestration -> services.scanning.navidrome_service
- Concurrent multi-page fetching -> services.scanning.navidrome_service
- Track metadata normalization -> services.scanning.metadata_extractor
- DB writes -> db.repositories.tracks / db.repositories.scan_repository
"""

from __future__ import annotations

import hashlib
import os
import random
import re
import time
from typing import Any

import structlog

from api_clients import session
from api_clients.http_utils import create_retry_client

logger = structlog.get_logger(__name__)

# Dedicated session for Navidrome with its OWN connection pool.
navidrome_session = create_retry_client(
    retries=1,
    backoff=0.5,
    status_forcelist=(429, 502, 503, 504),
    timeout=15.0,
)

_DEFAULT_RETRIES = 0
_RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})
_NON_RETRYABLE_STATUSES = frozenset({400, 401, 403, 404, 405, 409, 410})

_nav_error_log_ts: dict[str, float] = {}
_NAV_ERROR_LOG_COOLDOWN_SECONDS = 60.0


def _md5_hex(value: str) -> str:
    """Return the hex MD5 digest of a string."""
    return hashlib.md5(value.encode("utf-8")).hexdigest()


def _coerce_modified_ts(value: Any) -> int | None:
    """Normalise a timestamp into epoch seconds for modified/ifModifiedSince."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return int(float(s))
        except (TypeError, ValueError):
            pass
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return int(dt.timestamp())
        except (TypeError, ValueError):
            return None
    try:
        return int(value.timestamp())
    except (AttributeError, TypeError):
        return None


def _log_throttled_error(endpoint: str, exc: Exception, prefix: str = "Navidrome request failed") -> None:
    """Throttle repeated failures per endpoint to avoid log spam when offline."""
    _now = time.time()
    _last = _nav_error_log_ts.get(endpoint, 0.0)
    
    if _now - _last >= _NAV_ERROR_LOG_COOLDOWN_SECONDS:
        _nav_error_log_ts[endpoint] = _now
        logger.error(
            prefix,
            endpoint=endpoint,
            error=str(exc),
            error_type=type(exc).__name__,
        )
    else:
        logger.debug(
            f"{prefix} (suppressed)",
            endpoint=endpoint,
            error=str(exc),
            error_type=type(exc).__name__,
        )


class NavidromeClient:
    """HTTP client for Navidrome's Subsonic/OpenSubsonic API."""

    def __init__(self, base_url: str, username: str, password: str, http_session: Any = None, use_token_auth: bool = True):
        self.base_url = str(base_url or "").rstrip("/")
        if self.base_url and not re.match(r"^https?://", self.base_url, re.IGNORECASE):
            self.base_url = "http://" + self.base_url
            
        self.username = username or ""
        self.password = password or ""
        self.session = http_session or navidrome_session
        self.use_token_auth = use_token_auth
        self._stats_cache: dict[str, Any] | None = None
        self._last_stats_time = 0.0

    # ------------------------------------------------------------------
    # Core request helpers
    # ------------------------------------------------------------------

    def _build_params(self, **kwargs: Any) -> dict[str, Any]:
        params: dict[str, Any] = {
            "u": self.username,
            "v": "1.16.1",
            "c": "popularr",
            "f": "json",
        }

        if self.use_token_auth and self.password:
            salt = f"{random.getrandbits(64):016x}"
            params["t"] = _md5_hex(self.password + salt)
            params["s"] = salt
        else:
            params["p"] = self.password

        params.update(kwargs)
        return params

    def _get_subsonic_response(self, endpoint: str, *, timeout: int = 30, retries: int = _DEFAULT_RETRIES, **params: Any) -> dict[str, Any]:
        url = f"{self.base_url}/rest/{endpoint}"
        last_error: Exception | None = None
        retry_flag = True

        for attempt in range(1 + max(0, retries)):
            try:
                response = self.session.get(url, params=self._build_params(**params), timeout=timeout)
                
                if response.status_code in _RETRYABLE_STATUSES and attempt < retries:
                    wait = 0.5 * (attempt + 1)
                    logger.debug("Navidrome returned retryable HTTP status", endpoint=endpoint, status=response.status_code, retry_in=wait)
                    time.sleep(wait)
                    continue
                    
                if response.status_code in _NON_RETRYABLE_STATUSES:
                    retry_flag = False
                    response.raise_for_status()
                    
                response.raise_for_status()
                # Some Subsonic servers (Navidrome included) return HTTP 200 with
                # an EMPTY body for some endpoints (e.g. updatePlaylist).  Treat
                # an empty body as a successful empty response, not an error.
                if not response.content or not response.text.strip():
                    return {}
                result = response.json().get("subsonic-response", {}) or {}
                
                if not result:
                    logger.warning("Navidrome returned empty subsonic-response", endpoint=endpoint)
                return result
                
            except Exception as exc:
                last_error = exc
                if retry_flag and attempt < retries:
                    wait = 0.5 * (attempt + 1)
                    logger.debug("Navidrome request failed, retrying", endpoint=endpoint, retry_in=wait, error=str(exc))
                    time.sleep(wait)
                    continue

        if last_error:
            _log_throttled_error(endpoint, last_error, prefix=f"Navidrome {endpoint} failed after {retries + 1} attempts")
        return {}

    def _post_subsonic_response(self, endpoint: str, *, timeout: int = 60, **params: Any) -> dict[str, Any]:
        _body = self._build_params(**params) or {}
        url = f"{self.base_url}/rest/{endpoint}"
        try:
            response = self.session.post(url, data=_body, timeout=timeout)
            response.raise_for_status()
            # Some Subsonic servers (Navidrome included) return HTTP 200 with an
            # EMPTY body for mutation endpoints like updatePlaylist — an empty
            # body is the success response, not an error.  Treat it as ok.
            if not response.content or not response.text.strip():
                return {"status": "ok"}
            return response.json().get("subsonic-response", {}) or {}
        except Exception as exc:
            _log_throttled_error(endpoint, exc, prefix=f"Navidrome {endpoint} POST failed")
            return {}

    # ------------------------------------------------------------------
    # Library read endpoints
    # ------------------------------------------------------------------

    def get_artists(self, artist_ids: list[str] | None = None) -> list[dict[str, Any]]:
        try:
            data = self._get_subsonic_response("getArtists", timeout=60)
            index_groups = data.get("artists", {}).get("index", []) or []
            filter_ids = set(artist_ids or [])
            artists: list[dict[str, Any]] = []
            
            for group in index_groups:
                for artist in group.get("artist", []) or []:
                    if filter_ids and artist.get("id") not in filter_ids:
                        continue
                    artists.append(artist)
            return artists
        except Exception as exc:
            logger.error("Failed to fetch artists", error=str(exc))
            return []

    def get_albums(self, artist_id: str | None = None, page_size: int = 200) -> list[dict[str, Any]]:
        if artist_id:
            return self.fetch_artist_albums(artist_id)

        albums: list[dict[str, Any]] = []
        offset = 0
        size = min(int(page_size or 200), 500)

        while True:
            try:
                data = self._get_subsonic_response(
                    "getAlbumList2",
                    timeout=90,
                    type="alphabeticalByName",
                    size=size,
                    offset=offset,
                )
                page = data.get("albumList2", {}).get("album", []) or []
                if not page:
                    break
                albums.extend(page)
                if len(page) < size:
                    break
                offset += size
            except Exception as exc:
                logger.error("Failed to fetch album list", offset=offset, error=str(exc))
                break

        return albums

    def get_album_list2_page(self, list_type: str = "newest", size: int = 200, offset: int = 0) -> list[dict[str, Any]]:
        try:
            data = self._get_subsonic_response(
                "getAlbumList2",
                timeout=90,
                type=list_type,
                size=min(int(size or 200), 500),
                offset=offset,
            )
            return data.get("albumList2", {}).get("album", []) or []
        except Exception as exc:
            logger.error("Failed to fetch album list page", list_type=list_type, offset=offset, error=str(exc))
            return []

    def fetch_artist_albums(self, artist_id: str) -> list[dict[str, Any]]:
        try:
            data = self._get_subsonic_response("getArtist", timeout=60, id=artist_id)
            return data.get("artist", {}).get("album", []) or []
        except Exception as exc:
            logger.error("Failed to fetch albums for artist", artist_id=artist_id, error=str(exc))
            return []

    def fetch_album_tracks(self, album_id: str) -> dict[str, Any]:
        empty = {"tracks": [], "artist": "", "artistId": "", "name": "", "id": ""}
        try:
            data = self._get_subsonic_response("getAlbum", timeout=60, id=album_id)
            album = data.get("album", {}) or {}
            return {
                "tracks": album.get("song", []) or [],
                "artist": album.get("artist", "") or "",
                "artistId": album.get("artistId", "") or "",
                "name": album.get("name", "") or "",
                "id": album.get("id", "") or "",
            }
        except Exception as exc:
            logger.error("Failed to fetch tracks for album", album_id=album_id, error=str(exc))
            return empty

    def get_song(self, song_id: str) -> dict[str, Any]:
        try:
            data = self._get_subsonic_response("getSong", timeout=10, id=song_id)
            return data.get("song", {}) or {}
        except Exception as exc:
            logger.debug("Failed to fetch extended song metadata", song_id=song_id, error=str(exc))
            return {}

    def get_songs(self, offset: int = 0, size: int = 500, modified: Any = None) -> list[dict[str, Any]]:
        try:
            params: dict[str, Any] = {"offset": offset, "size": size}
            ts = _coerce_modified_ts(modified)
            if ts is not None:
                params["modified"] = ts
            data = self._get_subsonic_response("getSongs", timeout=60, **params)
            return data.get("songs", {}).get("song", []) or []
        except Exception as exc:
            logger.debug("Failed to fetch songs", offset=offset, error=str(exc))
            return []

    def get_indexes(self, if_modified_since: Any = None) -> dict[str, Any]:
        try:
            params: dict[str, Any] = {}
            ts = _coerce_modified_ts(if_modified_since)
            if ts is not None:
                params["ifModifiedSince"] = ts * 1000
            data = self._get_subsonic_response("getIndexes", timeout=60, **params)
            return data.get("indexes", {}) or {}
        except Exception as exc:
            logger.debug("Failed to fetch indexes", if_modified_since=if_modified_since, error=str(exc))
            return {}

    # ------------------------------------------------------------------
    # Playlist endpoints
    # ------------------------------------------------------------------

    @staticmethod
    def _is_smart_playlist(playlist: dict[str, Any]) -> bool:
        if not isinstance(playlist, dict):
            return False
        if playlist.get("smart") in (True, "true", "True", 1, "1"):
            return True
        if playlist.get("isSmart") in (True, "true", "True", 1, "1"):
            return True
        if playlist.get("criteria"):
            return True
        return str(playlist.get("type") or "").strip().lower() == "smart"

    def fetch_all_playlists(self) -> list[dict[str, Any]]:
        try:
            data = self._get_subsonic_response("getPlaylists", timeout=30)
            playlists = data.get("playlists", {}).get("playlist", []) or []
            for playlist in playlists:
                playlist["type"] = "smart" if self._is_smart_playlist(playlist) else "regular"
            return playlists
        except Exception as exc:
            logger.error("Failed to fetch playlists", error=str(exc))
            return []

    def fetch_playlist(self, playlist_id: str) -> dict[str, Any]:
        try:
            data = self._get_subsonic_response("getPlaylist", timeout=30, id=playlist_id)
            playlist = data.get("playlist", {}) or {}
            playlist["type"] = "smart" if self._is_smart_playlist(playlist) else "regular"
            playlist["tracks"] = playlist.pop("entry", []) or []
            return playlist
        except Exception as exc:
            logger.error("Failed to fetch playlist", playlist_id=playlist_id, error=str(exc))
            return {}

    def find_playlist_by_name(self, name: str) -> dict[str, Any] | None:
        wanted = str(name or "").strip().lower()
        for playlist in self.fetch_all_playlists():
            if str(playlist.get("name") or "").strip().lower() == wanted:
                return playlist
        return None

    def delete_playlist(self, playlist_id: str) -> bool:
        try:
            data = self._get_subsonic_response("deletePlaylist", timeout=30, id=playlist_id)
            return data.get("status") == "ok"
        except Exception as exc:
            logger.error("Failed to delete playlist", playlist_id=playlist_id, error=str(exc))
            return False

    def update_playlist_public(self, playlist_id: str, public: bool = True) -> bool:
        try:
            data = self._get_subsonic_response(
                "updatePlaylist",
                timeout=30,
                playlistId=playlist_id,
                public="true" if public else "false",
            )
            return data.get("status") == "ok"
        except Exception as exc:
            logger.error("Failed to update playlist public status", playlist_id=playlist_id, public=public, error=str(exc))
            return False

    def upload_playlist_cover(self, playlist_id: str, image_bytes: bytes, mime_type: str = "image/jpeg") -> bool:
        if not playlist_id or not image_bytes:
            return False
        url = f"{self.base_url}/rest/updatePlaylist"
        try:
            response = self.session.post(
                url,
                params=self._build_params(playlistId=playlist_id),
                files={"coverArt": ("cover.jpg", image_bytes, mime_type)},
                timeout=30,
            )
            data = response.json()
            ok = bool(data.get("subsonic-response", {}).get("status") == "ok")
            if not ok:
                logger.warning("Playlist cover upload rejected", playlist_id=playlist_id, response=data)
            return ok
        except Exception as exc:
            logger.warning("Playlist cover upload failed", playlist_id=playlist_id, error=str(exc))
            return False

    def rename_playlist(self, playlist_id: str, name: str) -> bool:
        try:
            data = self._get_subsonic_response(
                "updatePlaylist",
                timeout=30,
                playlistId=playlist_id,
                name=name,
            )
            return data.get("status") == "ok"
        except Exception as exc:
            logger.error("Failed to rename playlist", playlist_id=playlist_id, error=str(exc))
            return False

    def update_playlist_songs(self, playlist_id: str, song_ids: list[str]) -> bool:
        try:
            _current = self.fetch_playlist(playlist_id) or {}
            _count = len(_current.get("tracks") or []) or 0
            params: dict[str, Any] = {"playlistId": playlist_id}
            
            if _count > 0:
                params["songIndexToRemove"] = list(range(0, _count))
            if song_ids:
                params["songIdToAdd"] = list(song_ids)

            data = self._post_subsonic_response("updatePlaylist", timeout=120, **params)
            ok = data.get("status") == "ok"
            if not ok:
                logger.warning("updatePlaylist songs rejected", playlist_id=playlist_id, response=data)
            return ok
        except Exception as exc:
            logger.error("Failed to update playlist songs", playlist_id=playlist_id, error=str(exc))
            return False

    def create_playlist(self, name: str, song_ids: list[str]) -> dict[str, Any]:
        try:
            params: dict[str, Any] = {"name": str(name or "")}
            _ids = [str(s) for s in (song_ids or []) if str(s or "").strip()]
            if _ids:
                params["songId"] = _ids
            return self._post_subsonic_response("createPlaylist", timeout=120, **params)
        except Exception as exc:
            logger.error("Failed to create playlist", name=name, error=str(exc))
            return {}

    # ------------------------------------------------------------------
    # Additional Standard Endpoints (Scrobble, GetStarred, etc)
    # ------------------------------------------------------------------

    def get_artist_info(self, artist_id: str) -> dict[str, Any]:
        empty = {"biography": "", "similarArtist": [], "musicBrainzId": "", "lastFmUrl": ""}
        try:
            data = self._get_subsonic_response("getArtistInfo2", timeout=30, id=artist_id)
            info = data.get("artistInfo2", {}) or {}
            return {
                "biography": info.get("biography", "") or "",
                "similarArtist": info.get("similarArtist", []) or [],
                "musicBrainzId": info.get("musicBrainzId", "") or "",
                "lastFmUrl": info.get("lastFmUrl", "") or "",
            }
        except Exception as exc:
            logger.debug("Failed to fetch artist info", artist_id=artist_id, error=str(exc))
            return empty

    def get_starred_items(self) -> dict[str, list[dict[str, Any]]]:
        try:
            data = self._get_subsonic_response("getStarred2", timeout=60)
            starred = data.get("starred2", {}) or {}
            return {
                "tracks": starred.get("song", []) or [],
                "albums": starred.get("album", []) or [],
                "artists": starred.get("artist", []) or [],
            }
        except Exception as exc:
            logger.debug("getStarred2 failed, falling back to getStarred", error=str(exc))
            
        try:
            data = self._get_subsonic_response("getStarred", timeout=60)
            starred = data.get("starred", {}) or {}
            return {
                "tracks": starred.get("song", []) or [],
                "albums": starred.get("album", []) or [],
                "artists": starred.get("artist", []) or [],
            }
        except Exception as exc:
            logger.error("Failed to fetch starred items", error=str(exc))
            return {"tracks": [], "albums": [], "artists": []}

    def star_track(self, track_id: str) -> bool:
        try:
            data = self._get_subsonic_response("star", timeout=30, id=track_id)
            return data.get("status") == "ok"
        except Exception as exc:
            logger.error("Failed to star track", track_id=track_id, error=str(exc))
            return False

    def unstar_track(self, track_id: str) -> bool:
        try:
            data = self._get_subsonic_response("unstar", timeout=30, id=track_id)
            return data.get("status") == "ok"
        except Exception as exc:
            logger.error("Failed to unstar track", track_id=track_id, error=str(exc))
            return False

    def start_scan(self) -> bool:
        try:
            data = self._get_subsonic_response("startScan", timeout=10)
            return data.get("status") == "ok"
        except Exception as exc:
            logger.error("Failed to start Navidrome scan", error=str(exc))
            return False

    def get_scan_status(self) -> dict[str, Any]:
        try:
            data = self._get_subsonic_response("getScanStatus", timeout=10)
            scan_status = data.get("scanStatus", {}) or {}
            return {
                "success": True,
                "scanning": scan_status.get("scanning", False),
                "count": scan_status.get("count", 0),
                "lastScan": scan_status.get("lastScan"),
                "folderCount": scan_status.get("folderCount"),
            }
        except Exception as exc:
            logger.error("Failed to get Navidrome scan status", error=str(exc))
            return {"success": False, "error": str(exc)}

    def ping(self) -> bool:
        try:
            data = self._get_subsonic_response("ping", timeout=10)
            return data.get("status") == "ok"
        except Exception as exc:
            logger.debug("Navidrome ping failed", error=str(exc))
            return False

    def set_rating(self, track_id: str, rating: int) -> bool:
        try:
            rating = max(0, min(5, int(rating)))
            data = self._get_subsonic_response("setRating", timeout=15, id=track_id, rating=rating)
            return data.get("status") == "ok"
        except Exception as exc:
            logger.error("Failed to set rating for track", track_id=track_id, error=str(exc))
            return False

    def scrobble(self, track_id: str, time_stamp: int | None = None, submission: bool = True) -> bool:
        try:
            params: dict[str, Any] = {"id": track_id, "submission": str(submission).lower()}
            if time_stamp is not None:
                params["time"] = time_stamp
            data = self._get_subsonic_response("scrobble", timeout=15, **params)
            return data.get("status") == "ok"
        except Exception as exc:
            logger.error("Failed to scrobble track", track_id=track_id, error=str(exc))
            return False

    def search(self, query: str, artist_count: int = 5, album_count: int = 5, song_count: int = 20) -> dict[str, list[dict[str, Any]]]:
        try:
            data = self._get_subsonic_response(
                "search3",
                timeout=30,
                query=query,
                artistCount=artist_count,
                albumCount=album_count,
                songCount=song_count,
            )
            result = data.get("searchResult3", {}) or {}
            return {
                "artists": result.get("artist", []) or [],
                "albums": result.get("album", []) or [],
                "songs": result.get("song", []) or [],
            }
        except Exception as exc:
            logger.error("Failed to search Navidrome", query=query[:50], error=str(exc))
            return {"artists": [], "albums": [], "songs": []}

    def get_genres(self) -> list[dict[str, Any]]:
        try:
            data = self._get_subsonic_response("getGenres", timeout=15)
            return data.get("genres", {}).get("genre", []) or []
        except Exception as exc:
            logger.error("Failed to fetch genres", error=str(exc))
            return []

    def get_random_songs(self, size: int = 50, genre: str | None = None) -> list[dict[str, Any]]:
        try:
            params: dict[str, Any] = {"size": max(1, min(500, int(size)))}
            if genre:
                params["genre"] = genre
            data = self._get_subsonic_response("getRandomSongs", timeout=30, **params)
            return data.get("randomSongs", {}).get("song", []) or []
        except Exception as exc:
            logger.error("Failed to fetch random songs", error=str(exc))
            return []

    def get_cover_art_url(self, track_or_album_id: str, size: int = 300) -> str:
        params = self._build_params(id=track_or_album_id, size=size)
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{self.base_url}/rest/getCoverArt?{qs}"

    def get_cover_art_bytes(self, track_or_album_id: str, size: int = 600) -> bytes | None:
        try:
            url = self.get_cover_art_url(track_or_album_id, size=size)
            resp = self.session.get(url, timeout=15)
            if resp.status_code != 200:
                return None
            content_type = resp.headers.get("content-type", "")
            if content_type and not content_type.startswith("image/"):
                return None
            return resp.content or None
        except Exception as exc:
            logger.debug("Failed to fetch cover art bytes", track_or_album_id=track_or_album_id, error=str(exc))
            return None

    def get_stream_url(self, song_id: str, max_bitrate: int | None = None) -> str:
        params = self._build_params(id=song_id)
        if max_bitrate:
            params["maxBitRate"] = max_bitrate
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{self.base_url}/rest/stream?{qs}"

    def get_supported_extensions(self) -> list[dict[str, Any]]:
        try:
            data = self._get_subsonic_response("getOpenSubsonicExtensions", timeout=10)
            return data.get("openSubsonicExtensions", {}).get("extension", []) or []
        except Exception as exc:
            logger.debug("Could not query OpenSubsonic extensions", error=str(exc))
            return []

    def supports_opensubsonic(self) -> bool:
        exts = self.get_supported_extensions()
        return len(exts) > 0

    # ------------------------------------------------------------------
    # Compatibility forwarding methods
    # ------------------------------------------------------------------

    def build_artist_index(self) -> dict[str, dict[str, Any]]:
        from services.scanning.navidrome_service import build_artist_index
        return build_artist_index(self)

    def get_library_stats(self) -> dict[str, int]:
        from services.scanning.navidrome_service import get_library_stats
        return get_library_stats(self)

    def extract_track_metadata(self, track: dict[str, Any]) -> dict[str, Any]:
        from services.scanning.metadata_extractor import extract_track_metadata
        return extract_track_metadata(track, get_song=self.get_song)
