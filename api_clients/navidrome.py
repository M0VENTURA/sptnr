"""Navidrome API client.

This module is now intentionally HTTP-focused.

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

OpenSubsonic:
  Navidrome supports the OpenSubsonic extensions (response fields and
  endpoints).  This client strips the ``.view`` suffix from endpoints and
  parses the extra fields (musicBrainzId, genres, isrc, bpm, moods, …)
  where available.  Call ``supports_opensubsonic()`` to detect support.
"""

from __future__ import annotations

import hashlib
import logging
import os
import random
import time
from typing import Any

from api_clients import session

logger = logging.getLogger(__name__)

_DEFAULT_RETRIES = 2
_RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})
# Permanently-unavailable endpoints (e.g. `getSongs`, which Navidrome does not
# implement) must fail fast instead of retrying and spamming the log.
_NON_RETRYABLE_STATUSES = frozenset({400, 401, 403, 404, 405, 409, 410})


def _md5_hex(value: str) -> str:
    """Return the hex MD5 digest of a string."""
    return hashlib.md5(value.encode("utf-8")).hexdigest()


def _coerce_modified_ts(value: Any) -> int | None:
    """Normalise a timestamp into epoch seconds for ``modified``/``ifModifiedSince``.

    Accepts epoch-seconds (int/float), ISO-8601 strings, and datetime objects.
    Returns None when the value cannot be interpreted (so callers can skip the
    parameter rather than sending a malformed request).
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        # Numeric epoch string
        try:
            return int(float(s))
        except (TypeError, ValueError):
            pass
        # ISO-8601 (with or without timezone)
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return int(dt.timestamp())
        except (TypeError, ValueError):
            return None
    try:
        # datetime-like object
        return int(value.timestamp())
    except (AttributeError, TypeError):
        return None


class NavidromeClient:
    """HTTP client for Navidrome's Subsonic/OpenSubsonic API."""

    def __init__(self, base_url: str, username: str, password: str, http_session=None, use_token_auth: bool = True):
        """Create a Navidrome client.

        Args:
            base_url: Base Navidrome URL, e.g. ``http://localhost:4533``.
            username: Navidrome user name.
            password: Navidrome password.
            http_session: Optional httpx session.
            use_token_auth: When True (default), use token-based auth
                (``t=`` + ``s=``) instead of plaintext password (``p=``).
                Token auth sends ``md5(password + salt)`` instead of the
                raw password over the wire.
        """
        self.base_url = str(base_url or "").rstrip("/")
        self.username = username or ""
        self.password = password or ""
        self.session = http_session or session
        self.use_token_auth = use_token_auth
        self._stats_cache: dict[str, Any] | None = None
        self._last_stats_time = 0.0

    # ------------------------------------------------------------------
    # Core request helpers
    # ------------------------------------------------------------------

    def _build_params(self, **kwargs) -> dict[str, Any]:
        """Build standard Subsonic/OpenSubsonic API parameters.

        Uses Subsonic API v1.16.1.
        Authentication: token-based (``t=`` + ``s=``) when enabled,
        otherwise password-based (``p=``).
        """
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

    def _get_subsonic_response(self, endpoint: str, *, timeout: int = 30, retries: int = _DEFAULT_RETRIES, **params) -> dict[str, Any]:
        """Call a Navidrome endpoint and return the ``subsonic-response`` dict.

        Does NOT add a ``.view`` suffix — Navidrome supports both with and
        without, and OpenSubsonic favours bare endpoint names.

        Args:
            endpoint: API endpoint name (e.g. ``"getArtists"``).
            timeout: Request timeout in seconds.
            retries: Number of retries for transient HTTP errors.
            **params: Additional query parameters.

        Returns:
            Parsed ``subsonic-response`` dict, or empty dict on failure.
        """
        url = f"{self.base_url}/rest/{endpoint}"
        last_error: Exception | None = None
        retry = True

        for attempt in range(1 + max(0, retries)):
            try:
                response = self.session.get(url, params=self._build_params(**params), timeout=timeout)
                if response.status_code in _RETRYABLE_STATUSES and attempt < retries:
                    wait = 0.5 * (attempt + 1)
                    logger.debug("Navidrome %s returned HTTP %s, retrying in %.1fs", endpoint, response.status_code, wait)
                    time.sleep(wait)
                    continue
                if response.status_code in _NON_RETRYABLE_STATUSES:
                    # Endpoint does not exist / permanent client error — do not
                    # waste 3 retries (e.g. `getSongs`, which Navidrome 404s).
                    retry = False
                    response.raise_for_status()
                response.raise_for_status()
                result = response.json().get("subsonic-response", {}) or {}
                if not result:
                    logger.warning("Navidrome %s returned empty subsonic-response", endpoint)
                return result
            except Exception as exc:
                last_error = exc
                if retry and attempt < retries:
                    wait = 0.5 * (attempt + 1)
                    logger.debug("Navidrome %s failed, retrying in %.1fs: %s", endpoint, wait, exc)
                    time.sleep(wait)
                    continue

        logger.error("Navidrome %s failed after %s attempts: %s", endpoint, retries + 1, last_error)
        return {}

    # ------------------------------------------------------------------
    # Library read endpoints
    # ------------------------------------------------------------------

    def get_artists(self, artist_ids: list[str] | None = None) -> list[dict[str, Any]]:
        """Return flattened artists from getArtists, optionally filtered by IDs."""
        try:
            data = self._get_subsonic_response("getArtists", timeout=60)
            index_groups = data.get("artists", {}).get("index", []) or []
            filter_ids = set(artist_ids or [])
            artists: list[dict[str, Any]] = []
            for group in index_groups:
                for artist in group.get("artist", []) or []:
                    artist_id = artist.get("id")
                    if filter_ids and artist_id not in filter_ids:
                        continue
                    artists.append(artist)
            return artists
        except Exception as exc:
            logger.error("Failed to fetch artists: %s", exc)
            return []

    def get_albums(self, artist_id: str | None = None, page_size: int = 200) -> list[dict[str, Any]]:
        """Fetch all albums or albums for a specific artist."""
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
                logger.error("Failed to fetch album list at offset %s: %s", offset, exc)
                break

        return albums

    def get_album_list2_page(
        self,
        list_type: str = "newest",
        size: int = 200,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Fetch a single page of albums for a ``getAlbumList2`` list type.

        Used by the delta-scan helpers to bound the crawl to recently
        added/changed albums instead of paging the whole library.
        """
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
            logger.error("Failed to fetch album list page (%s, offset %s): %s", list_type, offset, exc)
            return []

    def fetch_artist_albums(self, artist_id: str) -> list[dict[str, Any]]:
        """Fetch all albums for a Navidrome artist ID."""
        try:
            data = self._get_subsonic_response("getArtist", timeout=60, id=artist_id)
            return data.get("artist", {}).get("album", []) or []
        except Exception as exc:
            logger.error("Failed to fetch albums for artist %s: %s", artist_id, exc)
            return []

    def fetch_album_tracks(self, album_id: str) -> dict[str, Any]:
        """Fetch all tracks for an album plus album-level metadata."""
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
            logger.error("Failed to fetch tracks for album %s: %s", album_id, exc)
            return empty

    def get_song(self, song_id: str) -> dict[str, Any]:
        """Fetch detailed metadata for one song via getSong."""
        try:
            data = self._get_subsonic_response("getSong", timeout=10, id=song_id)
            return data.get("song", {}) or {}
        except Exception as exc:
            logger.debug("Failed to fetch extended song metadata for %s: %s", song_id, exc)
            return {}

    def get_songs(self, offset: int = 0, size: int = 500, modified: Any = None) -> list[dict[str, Any]]:
        """Fetch a paged list of songs from Navidrome.

        Args:
            offset: Page offset.
            size: Page size (max 500).
            modified: Optional OpenSubsonic ``modified`` filter — only songs
                whose file changed after this timestamp are returned. Accepts
                an epoch-seconds int, an ISO-8601 string, or None (no filter).
        """
        try:
            params: dict[str, Any] = {"offset": offset, "size": size}
            if modified is not None:
                ts = _coerce_modified_ts(modified)
                if ts is not None:
                    params["modified"] = ts
            data = self._get_subsonic_response("getSongs", timeout=60, **params)
            return data.get("songs", {}).get("song", []) or []
        except Exception as exc:
            logger.debug("Failed to fetch songs at offset %s: %s", offset, exc)
            return []

    def get_indexes(self, if_modified_since: Any = None) -> dict[str, Any]:
        """Fetch the artist index, optionally restricted to changes after a timestamp.

        Uses the standard Subsonic ``getIndexes`` endpoint with
        ``ifModifiedSince`` so servers that honour the parameter only return
        artists whose content changed after the given time.

        Returns:
            The raw ``indexes`` dict (``index`` list + ``lastModified``) or
            an empty dict on failure.
        """
        try:
            params: dict[str, Any] = {}
            ts = _coerce_modified_ts(if_modified_since)
            if ts is not None:
                params["ifModifiedSince"] = ts
            data = self._get_subsonic_response("getIndexes", timeout=60, **params)
            return data.get("indexes", {}) or {}
        except Exception as exc:
            logger.debug("Failed to fetch indexes (ifModifiedSince=%s): %s", if_modified_since, exc)
            return {}

    # ------------------------------------------------------------------
    # Playlist endpoints
    # ------------------------------------------------------------------

    @staticmethod
    def _is_smart_playlist(playlist: dict[str, Any]) -> bool:
        """Return True when playlist metadata indicates a smart playlist."""
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
        """Fetch all playlists and add a normalized ``type`` field."""
        try:
            data = self._get_subsonic_response("getPlaylists", timeout=30)
            playlists = data.get("playlists", {}).get("playlist", []) or []
            for playlist in playlists:
                playlist["type"] = "smart" if self._is_smart_playlist(playlist) else "regular"
            return playlists
        except Exception as exc:
            logger.error("Failed to fetch playlists: %s", exc)
            return []

    def fetch_playlist(self, playlist_id: str) -> dict[str, Any]:
        """Fetch one playlist and normalize its track list to ``tracks``."""
        try:
            data = self._get_subsonic_response("getPlaylist", timeout=30, id=playlist_id)
            playlist = data.get("playlist", {}) or {}
            playlist["type"] = "smart" if self._is_smart_playlist(playlist) else "regular"
            playlist["tracks"] = playlist.pop("entry", []) or []
            return playlist
        except Exception as exc:
            logger.error("Failed to fetch playlist %s: %s", playlist_id, exc)
            return {}

    def find_playlist_by_name(self, name: str) -> dict[str, Any] | None:
        """Return the first playlist with a case-insensitive name match."""
        wanted = str(name or "").strip().lower()
        for playlist in self.fetch_all_playlists():
            if str(playlist.get("name") or "").strip().lower() == wanted:
                return playlist
        return None

    def delete_playlist(self, playlist_id: str) -> bool:
        """Delete a playlist via deletePlaylist.view."""
        try:
            data = self._get_subsonic_response("deletePlaylist", timeout=30, id=playlist_id)
            return data.get("status") == "ok"
        except Exception as exc:
            logger.error("Failed to delete playlist %s: %s", playlist_id, exc)
            return False

    def update_playlist_public(self, playlist_id: str, public: bool = True) -> bool:
        """Set a playlist public/shared flag."""
        try:
            data = self._get_subsonic_response(
                "updatePlaylist",
                timeout=30,
                playlistId=playlist_id,
                public="true" if public else "false",
            )
            return data.get("status") == "ok"
        except Exception as exc:
            logger.error("Failed to update playlist %s public=%s: %s", playlist_id, public, exc)
            return False

    def upload_playlist_cover(self, playlist_id: str, image_bytes: bytes, mime_type: str = "image/jpeg") -> bool:
        """Upload custom cover art for a playlist (Navidrome OpenSubsonic).

        POSTs the image bytes as a multipart ``coverArt`` field to
        ``updatePlaylist`` — Navidrome accepts playlist artwork uploads
        through this endpoint.  Best-effort: returns False (never raises)
        when the upload is rejected or the server lacks support.
        """
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
                logger.warning(
                    "[NAVIDROME] Playlist cover upload rejected for %s: %s", playlist_id, data,
                )
            return ok
        except Exception as exc:
            logger.warning("[NAVIDROME] Playlist cover upload failed for %s: %s", playlist_id, exc)
            return False

    def rename_playlist(self, playlist_id: str, name: str) -> bool:
        """Rename a playlist via the Subsonic ``updatePlaylist`` endpoint."""
        try:
            data = self._get_subsonic_response(
                "updatePlaylist",
                timeout=30,
                playlistId=playlist_id,
                name=name,
            )
            return data.get("status") == "ok"
        except Exception as exc:
            logger.error("Failed to rename playlist %s: %s", playlist_id, exc)
            return False

    # ------------------------------------------------------------------
    # Artist info (OpenSubsonic — requires external integration)
    # ------------------------------------------------------------------

    def get_artist_info(self, artist_id: str) -> dict[str, Any]:
        """Fetch extended artist info (biography, similar artists, etc.).

        Uses ``getArtistInfo2`` (OpenSubsonic) which may return ``biography``,
        ``similarArtist``, ``musicBrainzId``, etc. if Navidrome has external
        integration configured (Last.fm).

        Returns:
            Dict with keys like ``biography``, ``similarArtist``, ``lastFmUrl``.
        """
        empty = {"biography": "", "similarArtist": [], "musicBrainzId": ""}
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
            logger.debug("Failed to fetch artist info for %s: %s", artist_id, exc)
            return empty

    # ------------------------------------------------------------------
    # Star endpoints
    # ------------------------------------------------------------------

    def get_starred_items(self) -> dict[str, list[dict[str, Any]]]:
        """Fetch starred tracks, albums, and artists for the current user.

        Uses ``getStarred2`` (OpenSubsonic) for richer AlbumID3/ArtistID3
        objects that include ``musicBrainzId``, ``genres``, etc.
        Falls back to ``getStarred`` if the server doesn't support it.
        """
        try:
            data = self._get_subsonic_response("getStarred2", timeout=60)
            starred = data.get("starred2", {}) or {}
            return {
                "tracks": starred.get("song", []) or [],
                "albums": starred.get("album", []) or [],
                "artists": starred.get("artist", []) or [],
            }
        except Exception as exc:
            logger.debug("getStarred2 failed, falling back to getStarred: %s", exc)
        # Fallback
        try:
            data = self._get_subsonic_response("getStarred", timeout=60)
            starred = data.get("starred", {}) or {}
            return {
                "tracks": starred.get("song", []) or [],
                "albums": starred.get("album", []) or [],
                "artists": starred.get("artist", []) or [],
            }
        except Exception as exc:
            logger.error("Failed to fetch starred items: %s", exc)
            return {"tracks": [], "albums": [], "artists": []}

    def star_track(self, track_id: str) -> bool:
        """Star a track in Navidrome."""
        try:
            data = self._get_subsonic_response("star", timeout=30, id=track_id)
            return data.get("status") == "ok"
        except Exception as exc:
            logger.error("Failed to star track %s: %s", track_id, exc)
            return False

    def unstar_track(self, track_id: str) -> bool:
        """Unstar a track in Navidrome."""
        try:
            data = self._get_subsonic_response("unstar", timeout=30, id=track_id)
            return data.get("status") == "ok"
        except Exception as exc:
            logger.error("Failed to unstar track %s: %s", track_id, exc)
            return False

    # ------------------------------------------------------------------
    # Navidrome scan endpoints
    # ------------------------------------------------------------------

    def start_scan(self) -> bool:
        """Trigger a Navidrome library scan."""
        try:
            data = self._get_subsonic_response("startScan", timeout=10)
            return data.get("status") == "ok"
        except Exception as exc:
            logger.error("Failed to start Navidrome scan: %s", exc)
            return False

    def get_scan_status(self) -> dict[str, Any]:
        """Return current Navidrome library scan status."""
        try:
            data = self._get_subsonic_response("getScanStatus", timeout=10)
            scan_status = data.get("scanStatus", {}) or {}
            return {
                "success": True,
                "scanning": scan_status.get("scanning", False),
                "count": scan_status.get("count", 0),
                "lastScan": scan_status.get("lastScan"),  # OpenSubsonic extra field
                "folderCount": scan_status.get("folderCount"),
            }
        except Exception as exc:
            logger.error("Failed to get Navidrome scan status: %s", exc)
            return {"success": False, "error": str(exc)}

    # ------------------------------------------------------------------
    # Connection test
    # ------------------------------------------------------------------

    def ping(self) -> bool:
        """Test the connection to Navidrome.

        Calls Subsonic ``ping`` endpoint which returns
        ``{"subsonic-response": {"status": "ok"}}`` on success.

        Returns:
            True when the server responds and credentials are valid.
        """
        try:
            data = self._get_subsonic_response("ping", timeout=10)
            status = data.get("subsonic-response", {}).get("status")
            return status == "ok"
        except Exception as exc:
            logger.debug("Navidrome ping failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Rating endpoint
    # ------------------------------------------------------------------

    def set_rating(self, track_id: str, rating: int) -> bool:
        """Set a track rating in Navidrome (1-5 stars, 0 to clear).

        Args:
            track_id: Navidrome track ID.
            rating: Rating value 0-5 (Subsonic API uses 0-5 integer).

        Returns:
            True on success.
        """
        try:
            rating = max(0, min(5, int(rating)))
            data = self._get_subsonic_response("setRating", timeout=15, id=track_id, rating=rating)
            return data.get("status") == "ok"
        except Exception as exc:
            logger.error("Failed to set rating for track %s: %s", track_id, exc)
            return False

    # ------------------------------------------------------------------
    # Scrobble endpoint
    # ------------------------------------------------------------------

    def scrobble(self, track_id: str, time_stamp: int | None = None, submission: bool = True) -> bool:
        """Scrobble a track play (or mark as played) in Navidrome.

        Args:
            track_id: Navidrome track ID.
            time_stamp: Unix timestamp of the play (default: now).
            submission: True to record a play, False for "now playing".

        Returns:
            True on success.
        """
        try:
            params: dict[str, Any] = {"id": track_id, "submission": str(submission).lower()}
            if time_stamp is not None:
                params["time"] = time_stamp
            data = self._get_subsonic_response("scrobble", timeout=15, **params)
            return data.get("status") == "ok"
        except Exception as exc:
            logger.error("Failed to scrobble track %s: %s", track_id, exc)
            return False

    # ------------------------------------------------------------------
    # Search endpoint
    # ------------------------------------------------------------------

    def search(self, query: str, artist_count: int = 5, album_count: int = 5, song_count: int = 20) -> dict[str, list[dict[str, Any]]]:
        """Search Navidrome using search3.

        Args:
            query: Search query string.
            artist_count: Max artists to return.
            album_count: Max albums to return.
            song_count: Max songs to return.

        Returns:
            Dict with keys ``artists``, ``albums``, ``songs``.
        """
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
            logger.error("Failed to search Navidrome for '%s': %s", query[:50], exc)
            return {"artists": [], "albums": [], "songs": []}

    # ------------------------------------------------------------------
    # Genre endpoint
    # ------------------------------------------------------------------

    def get_genres(self) -> list[dict[str, Any]]:
        """Fetch all genres from Navidrome with song/album counts."""
        try:
            data = self._get_subsonic_response("getGenres", timeout=15)
            return data.get("genres", {}).get("genre", []) or []
        except Exception as exc:
            logger.error("Failed to fetch genres: %s", exc)
            return []

    # ------------------------------------------------------------------
    # Random songs endpoint
    # ------------------------------------------------------------------

    def get_random_songs(self, size: int = 50, genre: str | None = None) -> list[dict[str, Any]]:
        """Fetch random songs, optionally filtered by genre."""
        try:
            params: dict[str, Any] = {"size": max(1, min(500, int(size)))}
            if genre:
                params["genre"] = genre
            data = self._get_subsonic_response("getRandomSongs", timeout=30, **params)
            return data.get("randomSongs", {}).get("song", []) or []
        except Exception as exc:
            logger.error("Failed to fetch random songs: %s", exc)
            return []

    # ------------------------------------------------------------------
    # Cover art & streaming URL helpers
    # ------------------------------------------------------------------

    def get_cover_art_url(self, track_or_album_id: str, size: int = 300) -> str:
        """Return a URL to fetch cover art from Navidrome.

        Args:
            track_or_album_id: Navidrome track or album ID.
            size: Desired image size in pixels.

        Returns:
            Absolute URL to the cover art endpoint.
        """
        params = self._build_params(id=track_or_album_id, size=size)
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{self.base_url}/rest/getCoverArt?{qs}"

    def get_cover_art_bytes(
        self,
        track_or_album_id: str,
        size: int = 600,
    ) -> bytes | None:
        """Download cover art bytes from Navidrome.

        The auth token lives in the query string of ``get_cover_art_url``, so
        a plain GET of that URL returns the image bytes directly.

        Args:
            track_or_album_id: Navidrome track or album ID.
            size: Desired image size in pixels.

        Returns:
            Raw image bytes, or ``None`` when the server has no art.
        """
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
            logger.debug("Failed to fetch cover art bytes from Navidrome: %s", exc)
            return None

    def get_stream_url(self, song_id: str, max_bitrate: int | None = None) -> str:
        """Return a URL for streaming/downloading a song from Navidrome.

        Args:
            song_id: Navidrome song ID.
            max_bitrate: Optional max bitrate for transcoding.

        Returns:
            Absolute URL to the stream endpoint.
        """
        params = self._build_params(id=song_id)
        if max_bitrate:
            params["maxBitRate"] = max_bitrate
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{self.base_url}/rest/stream?{qs}"

    # ------------------------------------------------------------------
    # OpenSubsonic extension detection
    # ------------------------------------------------------------------

    def get_supported_extensions(self) -> list[dict[str, Any]]:
        """Query which OpenSubsonic extensions the server supports.

        Returns:
            List of extension dicts with ``name``, ``version`` keys.
        """
        try:
            data = self._get_subsonic_response("getOpenSubsonicExtensions", timeout=10)
            return data.get("openSubsonicExtensions", {}).get("extension", []) or []
        except Exception as exc:
            logger.debug("Could not query OpenSubsonic extensions: %s", exc)
            return []

    def supports_opensubsonic(self) -> bool:
        """Return True when the Navidrome server supports OpenSubsonic."""
        exts = self.get_supported_extensions()
        return len(exts) > 0

    # ------------------------------------------------------------------
    # Compatibility forwarding methods
    # ------------------------------------------------------------------

    def build_artist_index(self) -> dict[str, dict[str, Any]]:
        """Compatibility wrapper. Prefer services.scanning.navidrome_service."""
        from services.scanning.navidrome_service import build_artist_index
        return build_artist_index(self)

    def get_library_stats(self) -> dict[str, int]:
        """Compatibility wrapper. Prefer services.scanning.navidrome_service."""
        from services.scanning.navidrome_service import get_library_stats
        return get_library_stats(self)

    def extract_track_metadata(self, track: dict[str, Any]) -> dict[str, Any]:
        """Compatibility wrapper. Prefer services.scanning.metadata_extractor."""
        from services.scanning.metadata_extractor import extract_track_metadata
        return extract_track_metadata(track, get_song=self.get_song)
