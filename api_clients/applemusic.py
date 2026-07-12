"""Apple Music / iTunes Search API client."""

from __future__ import annotations

import logging
import time
from typing import Any

from api_clients import session

logger = logging.getLogger(__name__)

_APPLE_MUSIC_LAST_REQUEST_TIME = 0.0
_APPLE_MUSIC_MIN_INTERVAL = 0.1


def _throttle_apple_music() -> None:
    global _APPLE_MUSIC_LAST_REQUEST_TIME
    elapsed = time.time() - _APPLE_MUSIC_LAST_REQUEST_TIME
    if elapsed < _APPLE_MUSIC_MIN_INTERVAL:
        time.sleep(_APPLE_MUSIC_MIN_INTERVAL - elapsed)
    _APPLE_MUSIC_LAST_REQUEST_TIME = time.time()


class AppleMusicClient:
    """iTunes Search API wrapper for artist, track and album artwork."""

    def __init__(self, http_session=None, enabled: bool = True):
        self.session = http_session or session
        self.enabled = enabled
        self.base_url = "https://itunes.apple.com/search"
        self.headers = {"User-Agent": "sptnr-cli/1.0"}

    def _search(self, term: str, entity: str, limit: int, timeout: tuple[int, int] | int) -> list[dict[str, Any]]:
        if not self.enabled or not term:
            return []
        try:
            _throttle_apple_music()
            response = self.session.get(self.base_url, params={"term": term, "entity": entity, "limit": limit}, headers=self.headers, timeout=timeout)
            response.raise_for_status()
            results = response.json().get("results", [])
            return results if isinstance(results, list) else []
        except Exception as exc:
            logger.debug("Apple Music/iTunes search failed for %s: %s", term, exc)
            return []

    def search_artist(self, artist: str, limit: int = 5, timeout: tuple[int, int] | int = (5, 10)) -> list[dict[str, Any]]:
        return [item for item in self._search(artist, "allArtist", limit, timeout) if item.get("wrapperType") == "artist"]

    def search_track(self, title: str, artist: str, limit: int = 10, timeout: tuple[int, int] | int = (5, 10)) -> list[dict[str, Any]]:
        return self._search(f"{artist} {title}".strip(), "song", limit, timeout)

    def search_album(self, album: str, artist: str, limit: int = 10, timeout: tuple[int, int] | int = (5, 10)) -> list[dict[str, Any]]:
        return self._search(f"{artist} {album}".strip(), "album", limit, timeout)

    @staticmethod
    def _resize_artwork(url: str, size: int) -> str:
        return url.replace("100x100bb", f"{size}x{size}bb") if url else ""

    def get_artist_artwork(self, artist: str, size: int = 500, timeout: tuple[int, int] | int = (5, 10)) -> str:
        results = self.search_artist(artist, limit=1, timeout=timeout)
        return self._resize_artwork(results[0].get("artworkUrl100", ""), size) if results else ""

    def get_track_artwork(self, title: str, artist: str, size: int = 600, timeout: tuple[int, int] | int = (5, 10)) -> str:
        results = self.search_track(title, artist, limit=1, timeout=timeout)
        return self._resize_artwork(results[0].get("artworkUrl100", ""), size) if results else ""

    def get_album_artwork(self, album: str, artist: str, size: int = 600, timeout: tuple[int, int] | int = (5, 10)) -> str:
        results = self.search_album(album, artist, limit=1, timeout=timeout)
        return self._resize_artwork(results[0].get("artworkUrl100", ""), size) if results else ""


_apple_music_client: AppleMusicClient | None = None


def _get_apple_music_client(enabled: bool = True) -> AppleMusicClient:
    global _apple_music_client
    if _apple_music_client is None:
        _apple_music_client = AppleMusicClient(enabled=enabled)
    return _apple_music_client


def get_artist_artwork(artist: str, size: int = 500, enabled: bool = True, timeout: tuple[int, int] | int = (5, 10)) -> str:
    return _get_apple_music_client(enabled).get_artist_artwork(artist, size, timeout)


def get_track_artwork(title: str, artist: str, size: int = 600, enabled: bool = True, timeout: tuple[int, int] | int = (5, 10)) -> str:
    return _get_apple_music_client(enabled).get_track_artwork(title, artist, size, timeout)


def get_album_artwork(album: str, artist: str, size: int = 600, enabled: bool = True, timeout: tuple[int, int] | int = (5, 10)) -> str:
    return _get_apple_music_client(enabled).get_album_artwork(album, artist, size, timeout)
