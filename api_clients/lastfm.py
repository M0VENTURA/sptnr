"""Compatibility facade for Last.fm.

Low-level HTTP now lives in ``api_clients.lastfm_http`` and application logic
lives in ``services.enrichment.lastfm_service``.
"""

from __future__ import annotations

from typing import Any

from api_clients.lastfm_http import LastFmHttpClient, retry_with_backoff


class LastFmClient:
    """Backward-compatible Last.fm facade."""

    def __init__(self, api_key: str, username: str = None, http_session=None, db_connection=None):
        from services.enrichment.lastfm_service import LastFmService
        self.api_key = api_key
        self.username = username
        self.http = LastFmHttpClient(api_key=api_key, http_session=http_session)
        self.service = LastFmService(api_key=api_key, username=username, http_client=self.http, db_connection=db_connection)
        self.session = self.http.session
        self.base_url = self.http.base_url
        self.cache = self.service.cache
        self.db_connection = db_connection

    def get_track_info(self, artist: str, title: str, track_mbid: str | None = None) -> dict[str, Any]:
        return self.service.get_track_info(artist, title, track_mbid=track_mbid)

    def get_artist_top_tracks(self, artist: str, limit: int = 100) -> list[dict[str, Any]]:
        return self.service.get_artist_top_tracks(artist, limit=limit)

    def search_track(self, artist: str, title: str, limit: int = 10) -> list[dict[str, Any]]:
        return self.service.search_track(artist, title, limit=limit)

    def search_album(self, album: str, artist: str = "", limit: int = 10) -> list[dict[str, Any]]:
        return self.service.search_album(album, artist=artist, limit=limit)

    def get_album_track_count(self, artist: str, album: str) -> int:
        return self.service.get_album_track_count(artist, album)

    def check_track_as_single(self, artist: str, track_title: str) -> bool:
        return self.service.check_track_as_single(artist, track_title)

    def get_track_temporal_data(self, artist: str, title: str, track_mbid: str | None = None) -> dict[str, Any]:
        return self.service.get_track_temporal_data(artist, title, track_mbid=track_mbid)

    def get_artist_info(self, artist: str) -> dict[str, Any]:
        return self.service.get_artist_info(artist)

    def get_artist_top_tags(self, artist: str, limit: int = 10) -> list[dict[str, Any]]:
        return self.service.get_artist_top_tags(artist, limit=limit)

    def get_recommendations(self) -> dict[str, Any]:
        return self.service.get_recommendations()


__all__ = [
    "LASTFM_CONFIG",
    "RecommendationCache",
    "retry_with_backoff",
    "LastFmClient",
    "get_lastfm_track_info",
    "get_lastfm_recommendations",
]
