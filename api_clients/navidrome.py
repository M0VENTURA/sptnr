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

Compatibility:
- ``NavidromeClient.extract_track_metadata`` remains as a thin forwarding
  wrapper so existing callers do not immediately break, but new code should
  import ``services.scanning.metadata_extractor.extract_track_metadata``.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from api_clients import session

logger = logging.getLogger(__name__)

# Number of retries for transient HTTP failures
_DEFAULT_RETRIES = 2
_RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})


class NavidromeClient:
    """Small HTTP client for Navidrome's Subsonic-compatible API."""

    def __init__(self, base_url: str, username: str, password: str, http_session=None):
        """Create a Navidrome client.

        Args:
            base_url: Base Navidrome URL, e.g. ``http://localhost:4533``.
            username: Navidrome user name.
            password: Navidrome password or token depending on deployment.
            http_session: Optional requests-like session. Defaults to the
                shared API session.
        """
        self.base_url = str(base_url or "").rstrip("/")
        self.username = username or ""
        self.password = password or ""
        self.session = http_session or session
        self._stats_cache: dict[str, Any] | None = None
        self._last_stats_time = 0.0

    # ------------------------------------------------------------------
    # Core request helpers
    # ------------------------------------------------------------------

    def _build_params(self, **kwargs) -> dict[str, Any]:
        """Build standard Subsonic API parameters for a request.

        Uses Subsonic API v1.16.1 (the latest supported by Navidrome).
        Authentication can be either:
        - Password-based (``p=``): clear-text password
        - Token-based (``t=`` + ``s=``): hex-encoded MD5(token+salt)
        """
        params: dict[str, Any] = {
            "u": self.username,
            "p": self.password,
            "v": "1.16.1",
            "c": "popularr",
            "f": "json",
        }
        params.update(kwargs)
        return params

    def _get_subsonic_response(self, endpoint: str, *, timeout: int = 30, retries: int = _DEFAULT_RETRIES, **params) -> dict[str, Any]:
        """Call a Navidrome endpoint and return the ``subsonic-response`` dict.

        Args:
            endpoint: API endpoint name (e.g. ``"getArtists.view"``).
            timeout: Request timeout in seconds.
            retries: Number of retries for transient HTTP errors.
            **params: Additional query parameters.

        Returns:
            Parsed ``subsonic-response`` dict, or empty dict on failure.
        """
        url = f"{self.base_url}/rest/{endpoint}"
        last_error: Exception | None = None

        for attempt in range(1 + max(0, retries)):
            try:
                response = self.session.get(url, params=self._build_params(**params), timeout=timeout)
                if response.status_code in _RETRYABLE_STATUSES and attempt < retries:
                    wait = 0.5 * (attempt + 1)
                    logger.debug("Navidrome %s returned HTTP %s, retrying in %.1fs (attempt %s/%s)",
                                 endpoint, response.status_code, wait, attempt + 1, retries)
                    time.sleep(wait)
                    continue
                response.raise_for_status()
                result = response.json().get("subsonic-response", {}) or {}
                if not result:
                    logger.warning("Navidrome %s returned empty subsonic-response", endpoint)
                return result
            except Exception as exc:
                last_error = exc
                if attempt < retries:
                    wait = 0.5 * (attempt + 1)
                    logger.debug("Navidrome %s failed, retrying in %.1fs (attempt %s/%s): %s",
                                 endpoint, wait, attempt + 1, retries, exc)
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
            data = self._get_subsonic_response("getArtists.view", timeout=60)
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
                    "getAlbumList2.view",
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

    def fetch_artist_albums(self, artist_id: str) -> list[dict[str, Any]]:
        """Fetch all albums for a Navidrome artist ID."""
        try:
            data = self._get_subsonic_response("getArtist.view", timeout=60, id=artist_id)
            return data.get("artist", {}).get("album", []) or []
        except Exception as exc:
            logger.error("Failed to fetch albums for artist %s: %s", artist_id, exc)
            return []

    def fetch_album_tracks(self, album_id: str) -> dict[str, Any]:
        """Fetch all tracks for an album plus album-level metadata."""
        empty = {"tracks": [], "artist": "", "artistId": "", "name": "", "id": ""}
        try:
            data = self._get_subsonic_response("getAlbum.view", timeout=60, id=album_id)
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
            data = self._get_subsonic_response("getSong.view", timeout=10, id=song_id)
            return data.get("song", {}) or {}
        except Exception as exc:
            logger.debug("Failed to fetch extended song metadata for %s: %s", song_id, exc)
            return {}

    def get_songs(self, offset: int = 0, size: int = 500) -> list[dict[str, Any]]:
        """Fetch a paged list of songs from Navidrome."""
        try:
            data = self._get_subsonic_response("getSongs.view", timeout=60, offset=offset, size=size)
            return data.get("songs", {}).get("song", []) or []
        except Exception as exc:
            logger.debug("Failed to fetch songs at offset %s: %s", offset, exc)
            return []

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
            data = self._get_subsonic_response("getPlaylists.view", timeout=30)
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
            data = self._get_subsonic_response("getPlaylist.view", timeout=30, id=playlist_id)
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
            data = self._get_subsonic_response("deletePlaylist.view", timeout=30, id=playlist_id)
            return data.get("status") == "ok"
        except Exception as exc:
            logger.error("Failed to delete playlist %s: %s", playlist_id, exc)
            return False

    def update_playlist_public(self, playlist_id: str, public: bool = True) -> bool:
        """Set a playlist public/shared flag."""
        try:
            data = self._get_subsonic_response(
                "updatePlaylist.view",
                timeout=30,
                playlistId=playlist_id,
                public="true" if public else "false",
            )
            return data.get("status") == "ok"
        except Exception as exc:
            logger.error("Failed to update playlist %s public=%s: %s", playlist_id, public, exc)
            return False

    # ------------------------------------------------------------------
    # Star endpoints
    # ------------------------------------------------------------------

    def get_starred_items(self) -> dict[str, list[dict[str, Any]]]:
        """Fetch starred tracks, albums, and artists for the current user."""
        try:
            data = self._get_subsonic_response("getStarred.view", timeout=60)
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
            data = self._get_subsonic_response("star.view", timeout=30, id=track_id)
            return data.get("status") == "ok"
        except Exception as exc:
            logger.error("Failed to star track %s: %s", track_id, exc)
            return False

    def unstar_track(self, track_id: str) -> bool:
        """Unstar a track in Navidrome."""
        try:
            data = self._get_subsonic_response("unstar.view", timeout=30, id=track_id)
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
            data = self._get_subsonic_response("setRating.view", timeout=15, id=track_id, rating=rating)
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
            data = self._get_subsonic_response("scrobble.view", timeout=15, **params)
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
                "search3.view",
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
            data = self._get_subsonic_response("getGenres.view", timeout=15)
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
            data = self._get_subsonic_response("getRandomSongs.view", timeout=30, **params)
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
        return f"{self.base_url}/rest/getCoverArt.view?{qs}"

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
        return f"{self.base_url}/rest/stream.view?{qs}"

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
