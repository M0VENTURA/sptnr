"""Discogs enrichment service."""

from __future__ import annotations

import logging
import re
from typing import Any, TypedDict, List, Dict, Optional

from api_clients.discogs_http import DiscogsHttpClient
from helpers.normalization_service import (
    normalize_title_for_lookup,
    strip_parentheses,
    strip_featured_artist,
    clean_discogs_biography,
)

logger = logging.getLogger(__name__)

# --- TYPES ---
class DiscogsTrack(TypedDict):
    number: str
    title: str
    artist: str
    duration: int | None
    isrc: str

class DiscogsArtistProfile(TypedDict):
    profile: str
    real_name: str | None
    urls: List[str]
    images: List[Dict[str, Any]]

# --- HELPERS ---
def _parse_discogs_duration(duration_str: str) -> int | None:
    if not duration_str: return None
    try:
        parts = duration_str.split(":")
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
    except Exception: pass
    return None

# --- SERVICE CLASS ---
class DiscogsService:
    def __init__(self, token: str, http_client: DiscogsHttpClient | None = None, enabled: bool = True):
        self.token = token or ""
        self.enabled = enabled
        self.http = http_client or DiscogsHttpClient(token=token)
        self._single_cache: dict[tuple[str, str], bool] = {}

    def _normalize_title(self, title: str) -> str:
        base = strip_parentheses(title)
        return normalize_title_for_lookup(base or title)

    def is_single(self, title: str, artist: str, album_context: dict[str, Any] | None = None) -> bool:
        if not self.enabled or not self.token or not title or not artist: return False
        if album_context and album_context.get("is_special_edition"): return False

        artist_key = artist.lower()
        title_key = self._normalize_title(title)
        cache_key = (artist_key, title_key)

        if cache_key in self._single_cache: return self._single_cache[cache_key]

        # FIXED: Use search_database with specific params
        results = self.http.search_database({"q": f"{strip_featured_artist(artist)} {title_key}", "type": "release", "per_page": 5})
        
        for result in results:
            formats = " ".join(result.get("format", [])).lower()
            if ("single" in formats or "ep" in formats) and title_key in (result.get("title") or "").lower():
                self._single_cache[cache_key] = True
                return True
        self._single_cache[cache_key] = False
        return False

    def get_genres(self, title: str, artist: str) -> list[str]:
        if not self.enabled or not self.token: return []
        
        # FIXED: Use search_database
        results = self.http.search_database({"q": f"{artist} {title}", "type": "release", "per_page": 5})
        
        genres = []
        for r in results:
            genres.extend(r.get("genre", []))
            genres.extend(r.get("style", []))
        return genres

    def get_artist_biography(self, artist: str) -> DiscogsArtistProfile:
        # FIXED: Use search_database
        results = self.http.search_database({"q": artist, "type": "artist", "per_page": 1})
        if not results:
            return {"profile": "", "real_name": None, "urls": [], "images": []}
        
        artist_id = results[0].get("id")
        data = self.http.get_artist(artist_id) if artist_id else {}
        return {
            "profile": clean_discogs_biography(data.get("profile", "")),
            "real_name": data.get("realname"),
            "urls": data.get("urls", []),
            "images": data.get("images", []),
        }

    def get_release_tracks(self, release_id: str) -> List[DiscogsTrack]:
        if not self.enabled or not self.token or not release_id: return []
        release = self.http.get_release(release_id)
        if not isinstance(release, dict): return []
        
        tracks = []
        for track in release.get("tracklist", []):
            tracks.append({
                "number": track.get("position", ""),
                "title": track.get("title", ""),
                "artist": track.get("artist", track.get("artists", [{}])[0].get("name", "")),
                "duration": _parse_discogs_duration(track.get("duration", "")),
                "isrc": ""
            })
        return tracks

    def has_official_video(self, title: str, artist: str) -> bool:
        return False 

# --- BRIDGE FUNCTIONS ---
_DEFAULT_SERVICE: DiscogsService | None = None

def _get_service(token: str) -> DiscogsService:
    global _DEFAULT_SERVICE
    if _DEFAULT_SERVICE is None or _DEFAULT_SERVICE.token != token:
        _DEFAULT_SERVICE = DiscogsService(token=token)
    return _DEFAULT_SERVICE

def is_discogs_single(title: str, artist: str, token: str = "", album_context: dict | None = None) -> bool:
    return _get_service(token).is_single(title, artist, album_context=album_context)

def get_discogs_genres(title: str, artist: str, token: str = "") -> list[str]:
    return _get_service(token).get_genres(title, artist)

def get_discogs_artist_biography(artist: str, token: str = "") -> DiscogsArtistProfile:
    return _get_service(token).get_artist_biography(artist)

def has_discogs_video(title: str, artist: str, token: str = "") -> bool:
    return _get_service(token).has_official_video(title, artist)


def lookup_discogs_album(artist: str, album: str) -> dict:
    """Search Discogs for an album and return release candidates."""
    from api_clients.discogs_http import DiscogsHttpClient
    from helpers.config_helpers import get_config
    cfg = get_config() or {}
    token = cfg.get("api_integrations", {}).get("discogs", {}).get("token", "") or ""
    if not token:
        return {"success": False, "error": "Discogs token not configured"}
    try:
        http = DiscogsHttpClient(token=token)
        results = http.search_database({"q": f"{artist} {album}", "type": "release", "per_page": 5})
        return {"success": True, "results": results}
    except Exception as exc:
        return {"success": False, "error": str(exc)}