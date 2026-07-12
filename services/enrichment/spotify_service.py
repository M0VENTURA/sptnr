"""Spotify enrichment service.

Owns Spotify application behaviour:
- artist ID cache
- artist single track ID detection
- playlist and metadata wrappers
- search fallback handling
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from api_clients.spotify_http import SpotifyHttpClient

logger = logging.getLogger(__name__)


class SpotifyService:
    MAX_PAGINATION_ITERATIONS = 100

    def __init__(self, client_id: str, client_secret: str, http_client: SpotifyHttpClient | None = None, worker_threads: int = 4):
        self.http = http_client or SpotifyHttpClient(client_id, client_secret)
        self.worker_threads = worker_threads
        self._artist_id_cache: dict[str, str] = {}
        self._artist_singles_cache: dict[str, set[str]] = {}

    def get_artist_id(self, artist_name: str) -> str | None:
        key = (artist_name or "").strip().lower()
        if key in self._artist_id_cache:
            return self._artist_id_cache[key]
        try:
            payload = self.http.search(f'artist:"{artist_name}"', "artist", limit=1)
            items = payload.get("artists", {}).get("items", [])
            if items:
                artist_id = items[0].get("id")
                self._artist_id_cache[key] = artist_id
                return artist_id
        except Exception as exc:
            logger.debug("Spotify artist search failed for %s: %s", artist_name, exc)
        return None

    def get_artist_singles(self, artist_id: str) -> set[str]:
        if not artist_id:
            return set()
        if artist_id in self._artist_singles_cache:
            return self._artist_singles_cache[artist_id]
        singles_album_ids = []
        url = f"artists/{artist_id}/albums"
        params = {"include_groups": "single", "limit": 50}
        try:
            for _ in range(self.MAX_PAGINATION_ITERATIONS):
                payload = self.http.get_json(url, params=params, timeout=(5, 12), default={})
                singles_album_ids.extend([album.get("id") for album in payload.get("items", []) if album.get("id")])
                next_url = payload.get("next")
                if not next_url:
                    break
                url, params = next_url, None
        except Exception as exc:
            logger.debug("Spotify singles album fetch failed for %s: %s", artist_id, exc)

        def fetch_album_tracks(album_id: str) -> list[str]:
            try:
                payload = self.http.get_json(f"albums/{album_id}/tracks", params={"limit": 50}, timeout=(5, 12), default={})
                return [track.get("id") for track in payload.get("items", []) if track.get("id")]
            except Exception:
                return []

        single_track_ids = set()
        with ThreadPoolExecutor(max_workers=self.worker_threads) as pool:
            for result in pool.map(fetch_album_tracks, singles_album_ids[:250]):
                single_track_ids.update(result or [])
        self._artist_singles_cache[artist_id] = single_track_ids
        return single_track_ids

    def search_track(self, title: str, artist: str, album: str | None = None) -> list:
        queries = [f"{title} artist:{artist} album:{album}" if album else None, f"{title} artist:{artist}"]
        results = []
        for query in filter(None, queries):
            try:
                payload = self.http.search(query, "track", limit=10)
                results.extend(payload.get("tracks", {}).get("items", []) or [])
            except Exception:
                continue
        return results

    def get_playlist_tracks(self, playlist_id: str) -> list[dict]:
        tracks, url, params = [], f"playlists/{playlist_id}/tracks", {"limit": 100}
        while url:
            payload = self.http.get_json(url, params=params, timeout=(5, 15), default={})
            for item in payload.get("items", []) or []:
                track = item.get("track", {}) or {}
                if track.get("id"):
                    tracks.append({
                        "title": track.get("name", ""),
                        "artist": ", ".join([a.get("name", "") for a in track.get("artists", [])]),
                        "album": track.get("album", {}).get("name", ""),
                        "spotify_uri": track.get("uri", ""),
                        "spotify_id": track.get("id", ""),
                        "isrc": track.get("external_ids", {}).get("isrc", ""),
                        "duration_ms": track.get("duration_ms", 0),
                    })
            url, params = payload.get("next"), None
        return tracks

    def get_audio_features(self, track_id: str) -> dict | None:
        if not track_id:
            return None
        try:
            return self.http.get_json(f"audio-features/{track_id}", timeout=(5, 10), default={})
        except Exception:
            return None

    def get_audio_features_batch(self, track_ids: list[str]) -> dict[str, dict]:
        if not track_ids:
            return {}
        try:
            payload = self.http.get_json("audio-features", params={"ids": ",".join(track_ids[:100])}, timeout=(5, 15), default={})
            return {item["id"]: item for item in payload.get("audio_features", []) if item and item.get("id")}
        except Exception:
            return {}

    def get_artist_metadata(self, artist_id: str) -> dict | None:
        try:
            return self.http.get_json(f"artists/{artist_id}", timeout=(5, 10), default={}) if artist_id else None
        except Exception:
            return None

    def get_album_metadata(self, album_id: str) -> dict | None:
        try:
            return self.http.get_json(f"albums/{album_id}", timeout=(5, 10), default={}) if album_id else None
        except Exception:
            return None

    def get_track_metadata(self, track_id: str) -> dict | None:
        try:
            return self.http.get_json(f"tracks/{track_id}", timeout=(5, 10), default={}) if track_id else None
        except Exception:
            return None


def select_best_spotify_match(results: list[dict], track_title: str, album_context: dict | None = None) -> dict:
    """Select the best Spotify search result, filtering out undesirable versions.

    Prioritises singles over album tracks, and filters remixes/live versions
    unless the album context explicitly permits them.

    Args:
        results: List of Spotify track search results.
        track_title: Original track title for version validation.
        album_context: Optional dict with ``is_live`` / ``is_unplugged`` flags.

    Returns:
        Best matching track dict, or ``{"popularity": 0}`` if none match.
    """
    from helpers.normalization_service import is_valid_version

    allow_live_remix = bool(album_context and (album_context.get("is_live") or album_context.get("is_unplugged")))
    filtered = [r for r in results if is_valid_version(r.get("name", ""), allow_live_remix=allow_live_remix)]
    if not filtered:
        return {"popularity": 0}

    singles = [r for r in filtered if (r.get("album", {}).get("album_type", "").lower() == "single")]
    if singles:
        return max(singles, key=lambda r: r.get("popularity", 0))
    return max(filtered, key=lambda r: r.get("popularity", 0))
