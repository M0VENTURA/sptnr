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
import re
import time
from typing import Any

from api_clients import session
from api_clients.http_utils import create_retry_client

logger = logging.getLogger(__name__)

# Dedicated session for Navidrome with its OWN connection pool.
#
# The shared ``session`` serves every provider (MusicBrainz, Last.fm,
# ListenBrainz, Discogs, Wikipedia, …) concurrently from the scan's thread
# pool.  During a full library scan that pool is constantly saturated, and
# Navidrome calls queued behind it fail with ``httpx.PoolTimeout`` — each
# request burns ~47s across the transport's retry attempts, so rating syncs
# ("5/5 Navidrome rating syncs failed"), scan-status polls and search
# lookups starve for the whole scan.  Navidrome traffic is low-volume and
# mostly sequential, so a dedicated pool never contends with the other
# providers and a slow Navidrome can never be blamed on the scanner.
navidrome_session = create_retry_client(
    retries=1,
    backoff=0.5,
    status_forcelist=(429, 502, 503, 504),
    timeout=15.0,
)

# The shared ``session`` (``create_retry_client(retries=3)``) already retries
# connection errors and retryable statuses internally with backoff.  Keep the
# client-level retry count at 0 so we never double-retry (up to 9 attempts)
# during high-contention windows (e.g. DB pool exhaustion cascades).  The
# loop below still handles non-retryable statuses (fail fast) and surfaces
# the final error in the log.
_DEFAULT_RETRIES = 0
_RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})
# Permanently-unavailable endpoints (e.g. `getSongs`, which Navidrome does not
# implement) must fail fast instead of retrying and spamming the log.
_NON_RETRYABLE_STATUSES = frozenset({400, 401, 403, 404, 405, 409, 410})

# Per-endpoint throttle for the failure log — a misconfigured / unreachable
# Navidrome must not spam ERROR for every poll attempt.  The first failure in
# each window logs at ERROR; repeats within the window log at DEBUG.
_nav_error_log_ts: dict[str, float] = {}
_NAV_ERROR_LOG_COOLDOWN_SECONDS = 60.0


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
        # Tolerate scheme-less config values ("localhost:4533") — httpx
        # rejects URLs without an explicit protocol, which surfaced as
        # "Request URL is missing an 'http://' or 'https://' protocol".
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

        # Throttle repeated failures per endpoint: a misconfigured / unreachable
        # Navidrome must not spam the error log on every poll attempt.  The
        # first failure in each window logs at ERROR WITH the exception TYPE —
        # an empty ``str(exc)`` (e.g. a bare httpx timeout) is otherwise
        # impossible to diagnose; repeats within the window log at DEBUG.
        _now = time.time()
        _last = _nav_error_log_ts.get(endpoint, 0.0)
        if _now - _last >= _NAV_ERROR_LOG_COOLDOWN_SECONDS:
            _nav_error_log_ts[endpoint] = _now
            logger.error(
                "Navidrome %s failed after %s attempts: %s (%s)",
                endpoint, retries + 1, last_error, type(last_error).__name__,
            )
        else:
            logger.debug(
                "Navidrome %s failed again (suppressed): %s (%s)",
                endpoint, last_error, type(last_error).__name__,
            )
        return {}

    def _post_subsonic_response(self, endpoint: str, *, timeout: int = 60, **params) -> dict[str, Any]:
        """Call a Navidrome endpoint with a FORM-ENCODED POST body.

        Subsonic's REST endpoints accept both GET (params in the URL query)
        and POST (params in the request body); Navidrome implements both.
        The POST body is the only safe path for LARGE parameter sets —
        repeated ``songIdToAdd`` / ``songIdToRemove`` / ``songId`` values for
        a 1000+ song playlist exceed the URL length limit as query params
        (``URL component 'query' too long``) but fit comfortably in a body.

        Repeated list values are sent as repeated (key, value) form pairs
        (``songIdToAdd=id1&songIdToAdd=id2&...``) — the standard Subsonic
        repeatable-param encoding.

        Returns the parsed ``subsonic-response`` dict, or {} on failure.
        """
        _body_pairs: list[tuple[str, str]] = []
        for _k, _v in (self._build_params(**params) or {}).items():
            if isinstance(_v, (list, tuple)):
                for _item in _v:
                    _body_pairs.append((_k, str(_item)))
            else:
                _body_pairs.append((_k, str(_v)))
        url = f"{self.base_url}/rest/{endpoint}"
        try:
            response = self.session.post(url, data=_body_pairs, timeout=timeout)
            response.raise_for_status()
            return response.json().get("subsonic-response", {}) or {}
        except Exception as exc:
            _now = time.time()
            _last = _nav_error_log_ts.get(endpoint, 0.0)
            if _now - _last >= _NAV_ERROR_LOG_COOLDOWN_SECONDS:
                _nav_error_log_ts[endpoint] = _now
                logger.error(
                    "Navidrome %s POST failed after %s: %s (%s)",
                    endpoint, timeout, exc, type(exc).__name__,
                )
            else:
                logger.debug(
                    "Navidrome %s POST failed again (suppressed): %s", endpoint, exc,
                )
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
        artists whose content changed after the given time.  Navidrome's
        implementation is coarse: it returns the full album-artist index when
        a library scan completed after the timestamp, and an empty index
        otherwise.

        Returns:
            The raw ``indexes`` dict (``index`` list + ``lastModified``) or
            an empty dict on failure.
        """
        try:
            params: dict[str, Any] = {}
            ts = _coerce_modified_ts(if_modified_since)
            if ts is not None:
                # Subsonic spec (and Navidrome's req.TimeOr) read
                # ``ifModifiedSince`` as epoch MILLISECONDS.
                params["ifModifiedSince"] = ts * 1000
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

    def update_playlist_songs(self, playlist_id: str, song_ids: list[str]) -> bool:
        """Replace a playlist's song list IN PLACE via ``updatePlaylist``.

        Subsonic's ``updatePlaylist`` edits an existing playlist rather than
        recreating it: ``songIndexToRemove`` (repeatable) removes the current
        entries and ``songIdToAdd`` (repeatable) appends the new set.  This
        keeps the playlist's identity (id, name, cover, created date) intact —
        recreating a playlist on every scan is what left duplicate entries in
        Navidrome's UI (each rewrite of a watch-folder ``.m3u`` was imported
        as a NEW playlist).

        The current track count is fetched first so ``songIndexToRemove``
        uses only VALID indices (0..N-1) — Subsonic implementations reject
        or ignore out-of-range indices, which would leave stale entries.

        When ``song_ids`` is empty, every existing entry is removed and the
        playlist is emptied (not deleted).

        LARGE PLAYLISTS: the song list is sent as a form-encoded POST body,
        NOT query parameters.  Subsonic's REST endpoints accept both GET and
        POST with params in the body; Navidrome implements it.  Sending 1000+
        ``songIdToAdd`` values as query params blew past the URL length limit
        (``URL component 'query' too long``) — a 1119-song "Nu Metal - Top
        Tracks" playlist failed every sync.  The POST body has no such limit.

        Returns True when Navidrome acknowledged the update.
        """
        try:
            # Fetch the current entry count so removal indices are valid.
            _current = self.fetch_playlist(playlist_id) or {}
            _count = len(_current.get("tracks") or []) or 0
            params: dict[str, Any] = {"playlistId": playlist_id}
            if _count > 0:
                params["songIndexToRemove"] = list(range(0, _count))
            if song_ids:
                params["songIdToAdd"] = list(song_ids)

            # Form-encoded POST body — the song list can exceed the URL query
            # length limit (a 1119-song playlist failed every sync with
            # ``URL component 'query' too long``); the body has no such cap.
            data = self._post_subsonic_response(
                "updatePlaylist",
                timeout=120,
                **params,
            )
            ok = data.get("status") == "ok"
            if not ok:
                logger.warning(
                    "[NAVIDROME] updatePlaylist songs rejected for %s: %s",
                    playlist_id, data,
                )
            return ok
        except Exception as exc:
            logger.error("Failed to update playlist %s songs: %s", playlist_id, exc)
            return False

    def create_playlist(self, name: str, song_ids: list[str]) -> dict[str, Any]:
        """Create a playlist via ``createPlaylist`` with a form-encoded POST.

        Sends ``name`` + the repeatable ``songId`` list in the request body —
        the same URL-length-safe path as ``update_playlist_songs`` (a fresh
        1000+ song "New Music" / genre playlist would otherwise exceed the
        query limit).

        Returns the parsed ``subsonic-response`` dict (status + the created
        playlist, when acknowledged).
        """
        try:
            params: dict[str, Any] = {"name": str(name or "")}
            _ids = [str(s) for s in (song_ids or []) if str(s or "").strip()]
            if _ids:
                params["songId"] = _ids
            return self._post_subsonic_response("createPlaylist", timeout=120, **params)
        except Exception as exc:
            logger.error("Failed to create playlist '%s': %s", name, exc)
            return {}

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
            # ``_get_subsonic_response`` already unwraps the outer
            # ``subsonic-response`` key, so ``data`` is the inner dict
            # (e.g. ``{"status": "ok", "version": ...}``).  Reading the
            # ``subsonic-response`` key again here would always miss and
            # make ping() return False even for valid credentials.
            data = self._get_subsonic_response("ping", timeout=10)
            status = data.get("status")
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
