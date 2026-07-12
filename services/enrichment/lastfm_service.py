"""Last.fm enrichment service.

This module owns Last.fm application behaviour:
- safer multi-artist candidate handling
- featured-artist stripping
- track/artist match scoring
- recommendation cache policy
- album/title-track/single interpretation

Raw HTTP is handled by ``api_clients.lastfm_http``.
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from api_clients.lastfm_http import LastFmHttpClient, retry_with_backoff
from helpers.config_helpers import get_lastfm_config
from services.popularity.popularity_sources import (
    get_aggregated_lastfm_popularity,
)
from services.popularity.popularity_matching import (
    choose_best_provider_counts,
    get_primary_artist_preserve_case,
    get_artist_lookup_candidates,
    make_artist_match_key,
    make_track_match_key,
)

logger = logging.getLogger(__name__)

# Load Last.fm configuration from centralized config
LASTFM_CONFIG = get_lastfm_config()


class RecommendationCache:
    """Simple JSON cache for Last.fm recommendation payloads.
    
    This cache stores API responses to reduce redundant calls to Last.fm.
    Entries are automatically expired based on TTL (Time To Live) configuration.
    
    Attributes:
        cache_dir: Directory path where cache files are stored.
        cache_file: Path to the JSON cache file.
        
    Cache Structure:
        {
            "cache_key": {
                "data": {...},  # Cached recommendation data
                "timestamp": 1234567890.123  # Unix timestamp when cached
            }
        }
        
    Example:
        >>> cache = RecommendationCache()
        >>> cache.set("artist:the-beatles", {"tracks": [...]})
        >>> data = cache.get("artist:the-beatles")
    """

    def __init__(self, cache_dir: str | None = None):
        """Initialize the recommendation cache.
        
        Args:
            cache_dir: Optional custom directory for cache storage.
                Defaults to ~/.cache/sptnr if not provided.
        """
        self.cache_dir = Path(cache_dir) if cache_dir else Path.home() / ".cache" / "sptnr"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_file = self.cache_dir / "lastfm_recommendations.json"

    def get(self, key: str) -> dict[str, Any] | None:
        """Retrieve a cached recommendation by key.
        
        Args:
            key: Cache lookup key (e.g., "artist:the-beatles").
            
        Returns:
            Cached data dictionary if found and not expired, None otherwise.
            
        Note:
            Expired entries are automatically removed from the cache file
            during retrieval to prevent cache bloat.
        """
        try:
            if not self.cache_file.exists():
                return None
            cache = json.loads(self.cache_file.read_text(encoding="utf-8"))
            entry = cache.get(key)
            if not entry:
                return None
            age_hours = (time.time() - entry["timestamp"]) / 3600
            if age_hours > LASTFM_CONFIG["CACHE_TTL_HOURS"]:
                cache.pop(key, None)
                self._save(cache)
                return None
            return entry.get("data")
        except Exception as exc:
            logger.debug("Last.fm recommendation cache read failed for %s: %s", key, exc)
            return None

    def set(self, key: str, value: dict[str, Any]) -> None:
        """Store a recommendation in the cache.
        
        Args:
            key: Cache key to store under (e.g., "artist:the-beatles").
            value: Recommendation data to cache.
            
        Note:
            Uses atomic write (write to temp file, then rename) to prevent
            cache corruption if the process is interrupted during write.
        """
        try:
            cache = json.loads(self.cache_file.read_text(encoding="utf-8")) if self.cache_file.exists() else {}
            cache[key] = {"data": value, "timestamp": time.time()}
            self._save(cache)
        except Exception as exc:
            logger.debug("Last.fm recommendation cache write failed for %s: %s", key, exc)

    def _save(self, cache: dict[str, Any]) -> None:
        """Save cache to disk atomically.
        
        Uses a temporary file and atomic rename to ensure cache integrity.
        
        Args:
            cache: The complete cache dictionary to persist.
        """
        tmp = self.cache_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, indent=2), encoding="utf-8")
        tmp.replace(self.cache_file)


class LastFmService:
    """Application-level Last.fm behaviour."""

    _FEATURE_RE = re.compile(r"\s+(?:feat\.?|featuring|ft\.?)\s+", re.IGNORECASE)

    def __init__(self, api_key: str, username: str | None = None, http_client: LastFmHttpClient | None = None, db_connection=None):
        self.api_key = api_key or ""
        self.username = username
        self.http = http_client or LastFmHttpClient(api_key=api_key)
        self.cache = RecommendationCache()
        self.db_connection = db_connection

        self.mb_client = None

    @staticmethod
    def clean_spaces(text: str) -> str:
        """Collapse whitespace in a string, preserving everything else."""
        return re.sub(r"\s+", " ", (text or "")).strip()

    @staticmethod
    def extract_artist_name(artist_field) -> str:
        """Handle Last.fm's inconsistent artist payload shapes."""
        if isinstance(artist_field, str):
            return artist_field.strip()
        if isinstance(artist_field, dict):
            name = (artist_field.get("name") or artist_field.get("#text") or "").strip()
            if name:
                return name
            nested = artist_field.get("artist")
            if isinstance(nested, dict):
                return (nested.get("name") or nested.get("#text") or "").strip()
            if isinstance(nested, str):
                return nested.strip()
        return ""

    @classmethod
    def strip_featured_artist(cls, artist: str) -> str:
        """Remove featured-artist suffix (feat., ft., featuring) from an artist string."""
        if not artist:
            return ""
        return cls.clean_spaces(cls._FEATURE_RE.split(artist, maxsplit=1)[0])

    @classmethod
    def normalize_artist_for_compare(cls, artist: str) -> str:
        """Normalise artist name for fuzzy matching — lowercase, collapse separators."""
        if not artist:
            return ""
        value = cls.clean_spaces(artist).lower()
        value = cls._FEATURE_RE.split(value, maxsplit=1)[0]
        value = re.sub(r"\s*(?:\+|&|/|×|\bx\b|\bvs\b|\bwith\b)\s*", " & ", value, flags=re.IGNORECASE)
        return cls.clean_spaces(value)

    @classmethod
    def build_artist_lookup_candidates(cls, artist: str) -> list[str]:
        """Build conservative Last.fm artist lookup candidates."""
        candidates: list[str] = []
        seen: set[str] = set()

        def add(value: str):
            value = cls.clean_spaces(value)
            key = value.lower()
            if value and key not in seen:
                candidates.append(value)
                seen.add(key)

        original = cls.clean_spaces(artist)
        primary = cls.strip_featured_artist(artist)
        add(original)
        add(primary)
        add(re.sub(r"\s*(?:\+|&|/|×|\bx\b|\bvs\b|\bwith\b)\s*", " & ", primary, flags=re.IGNORECASE))
        return candidates

    @classmethod
    def artist_match_score(cls, query_artist: str, returned_artist: str) -> int:
        """Score returned Last.fm artist against requested artist."""
        q_raw = cls.clean_spaces(query_artist).lower()
        r_raw = cls.clean_spaces(returned_artist).lower()
        q_norm = cls.normalize_artist_for_compare(query_artist)
        r_norm = cls.normalize_artist_for_compare(returned_artist)
        q_primary = cls.strip_featured_artist(query_artist).lower()
        r_primary = cls.strip_featured_artist(returned_artist).lower()
        if q_raw and r_raw and q_raw == r_raw:
            return 100
        if q_norm and r_norm and q_norm == r_norm:
            return 90
        if q_primary and r_primary and q_primary == r_primary:
            return 70
        if q_primary and r_raw and q_primary == r_raw:
            return 60
        if q_norm and r_raw and q_norm == r_raw:
            return 60
        return 0

    def _get_track_info_once(self, artist: str, title: str, track_mbid: str | None = None) -> dict[str, Any]:
        """Perform one track.getInfo request."""
        params: dict[str, Any] = {"autocorrect": 1}
        if track_mbid:
            params["mbid"] = track_mbid
        else:
            params["artist"] = artist
            params["track"] = title

        try:
            data = self.http.get_json("track.getInfo", timeout=(5, 10), **params)
            if "error" in data:
                return self._empty_track_info(artist, title)

            track = data.get("track", {}) if isinstance(data, dict) else {}
            returned_artist = self.extract_artist_name(track.get("artist"))
            album_title = ""
            if isinstance(track.get("album"), dict):
                album_title = track["album"].get("title") or track["album"].get("name") or ""

            return {
                "track_play": int(track.get("playcount", 0) or 0),
                "listeners": int(track.get("listeners", 0) or 0),
                "toptags": track.get("toptags", {}) or {},
                "lookup_artist": artist,
                "returned_artist": returned_artist,
                "track_name": track.get("name", title),
                "url": track.get("url", ""),
                "album": album_title,
            }
        except Exception as exc:
            logger.debug("Last.fm track.getInfo failed for %s / %s: %s", artist, title, exc)
            return self._empty_track_info(artist, title)

    @staticmethod
    def _empty_track_info(artist: str, title: str) -> dict[str, Any]:
        return {"track_play": 0, "listeners": 0, "toptags": {}, "lookup_artist": artist, "returned_artist": "", "track_name": title, "url": "", "album": ""}

    def get_track_info(self, artist: str, title: str, track_mbid: str | None = None) -> dict[str, Any]:
        """Fetch track listeners/playcount with safer multi-artist handling."""
        if not self.api_key:
            return self._empty_track_info(artist, title)

        best_result: dict[str, Any] | None = None
        best_tuple = (-1, -1, -1)

        if track_mbid:
            mbid_result = self._get_track_info_once(artist, title, track_mbid=track_mbid)
            best_result = mbid_result
            best_tuple = (self.artist_match_score(artist, mbid_result.get("returned_artist", "")), int(mbid_result.get("listeners", 0) or 0), int(mbid_result.get("track_play", 0) or 0))
            if best_tuple[0] >= 90 and (best_tuple[1] > 0 or best_tuple[2] > 0):
                return self._normalise_track_result(best_result, artist)

        for candidate_artist in self.build_artist_lookup_candidates(artist):
            candidate = self._get_track_info_once(candidate_artist, title)
            candidate_tuple = (self.artist_match_score(artist, candidate.get("returned_artist", "")), int(candidate.get("listeners", 0) or 0), int(candidate.get("track_play", 0) or 0))
            if candidate_tuple > best_tuple:
                best_tuple = candidate_tuple
                best_result = candidate

        return self._normalise_track_result(best_result or self._empty_track_info(artist, title), artist)

    @staticmethod
    def _normalise_track_result(result: dict[str, Any], fallback_artist: str) -> dict[str, Any]:
        return {
            "track_play": int(result.get("track_play", 0) or 0),
            "listeners": int(result.get("listeners", 0) or 0),
            "toptags": result.get("toptags", {}) or {},
            "lookup_artist": result.get("lookup_artist", fallback_artist),
            "returned_artist": result.get("returned_artist", ""),
            "url": result.get("url", ""),
            "album": result.get("album", ""),
        }

    def search_track(self, artist: str, title: str, limit: int = 10) -> list[dict[str, Any]]:
        """Search tracks and rank by artist match score."""
        if not self.api_key:
            return []
        try:
            data = self.http.get_json("track.search", timeout=(5, 10), track=title, limit=limit)
            tracks = data.get("results", {}).get("trackmatches", {}).get("track", [])
            if isinstance(tracks, dict):
                tracks = [tracks]
            output = []
            for track in tracks:
                track_artist = self.extract_artist_name(track.get("artist"))
                score = self.artist_match_score(artist, track_artist)
                if score >= 60:
                    output.append({"name": track.get("name", ""), "artist": track_artist, "listeners": int(track.get("listeners", 0) or 0), "url": track.get("url", ""), "_score": score})
            output.sort(key=lambda item: (item["_score"], item["listeners"]), reverse=True)
            for item in output:
                item.pop("_score", None)
            return output
        except Exception as exc:
            logger.debug("Last.fm track.search failed for %s / %s: %s", artist, title, exc)
            return []

    def _album_get_info(self, artist: str, album: str) -> dict[str, Any]:
        """Try album.getInfo across artist variants."""
        if not self.api_key:
            return {}
        for lookup_artist in self.build_artist_lookup_candidates(artist):
            try:
                payload = self.http.get_json("album.getInfo", timeout=(5, 10), artist=lookup_artist, album=album)
                album_payload = payload.get("album")
                if isinstance(album_payload, dict):
                    album_payload["_lookup_artist"] = lookup_artist
                    return album_payload
            except Exception as exc:
                logger.debug("Last.fm album.getInfo failed for %s / %s: %s", lookup_artist, album, exc)
        return {}

    def get_album_track_count(self, artist: str, album: str) -> int:
        """Fetch album track count from Last.fm."""
        album_data = self._album_get_info(artist, album)
        tracks = album_data.get("tracks", {}) if isinstance(album_data, dict) else {}
        if isinstance(tracks, dict):
            track_list = tracks.get("track", [])
            if isinstance(track_list, dict):
                return 1
            if isinstance(track_list, list):
                return len(track_list)
        if isinstance(tracks, list):
            return len(tracks)
        return 0

    def check_track_as_single(self, artist: str, track_title: str) -> bool:
        """Return True when Last.fm has album payload with same name and < 6 tracks."""
        album_data = self._album_get_info(artist, track_title)
        if not album_data:
            return False
        if (album_data.get("name") or "").lower().strip() != track_title.lower().strip():
            return False
        return 0 < self.get_album_track_count(artist, track_title) < 6

    def get_track_temporal_data(self, artist: str, title: str, track_mbid: str | None = None) -> dict[str, Any]:
        """Return all-time Last.fm data; Last.fm standard API does not expose rolling breakdown here."""
        info = self.get_track_info(artist, title, track_mbid=track_mbid)
        return {
            "all_time_listeners": int(info.get("listeners", 0) or 0),
            "all_time_playcount": int(info.get("track_play", 0) or 0),
            "7day_listeners": None,
            "365day_listeners": None,
            "momentum_score": 1.0,
            "popularity_trend": "unknown",
            "data_source": "standard_api_only" if self.api_key else "unavailable",
        }

    def get_artist_info(self, artist: str) -> dict[str, Any]:
        """Fetch Last.fm artist biography/image."""
        if not self.api_key:
            return {"bio": "", "bio_text": "", "image": "", "similar": []}
        for lookup_artist in self.build_artist_lookup_candidates(artist):
            try:
                data = self.http.get_json("artist.getInfo", timeout=(5, 10), artist=lookup_artist).get("artist", {})
                if not data:
                    continue
                image_url = ""
                if isinstance(data.get("image"), list):
                    for image in reversed(data["image"]):
                        if image.get("#text"):
                            image_url = image.get("#text", "")
                            break
                bio = data.get("bio", {}) if isinstance(data.get("bio"), dict) else {}
                return {"bio": bio.get("content", ""), "bio_text": bio.get("summary", "") or bio.get("content", ""), "image": image_url, "similar": []}
            except Exception as exc:
                logger.debug("Last.fm artist.getInfo failed for %s: %s", lookup_artist, exc)
        return {"bio": "", "bio_text": "", "image": "", "similar": []}

    def get_artist_top_tags(self, artist: str, limit: int = 10) -> list[dict[str, Any]]:
        """Fetch top tags for an artist."""
        for lookup_artist in self.build_artist_lookup_candidates(artist):
            try:
                data = self.http.get_json("artist.getTopTags", timeout=(5, 10), artist=lookup_artist, limit=max(1, min(100, limit)))
                tags = data.get("toptags", {}).get("tag", [])
                if isinstance(tags, dict):
                    tags = [tags]
                result = [{"name": tag.get("name", ""), "count": int(tag.get("count", 0) or 0)} for tag in tags if isinstance(tag, dict)]
                if result:
                    return result
            except Exception:
                continue
        return []

    def get_recommendations(self) -> dict[str, list[dict[str, Any]]]:
        """Fetch simple Last.fm recommendations/top items."""
        if not self.api_key:
            return {"artists": [], "albums": [], "tracks": []}
        cache_key = f"recommendations_{self.username or 'global'}" if not self.db_connection else None
        if cache_key:
            cached = self.cache.get(cache_key)
            if cached:
                return cached
        result = {"artists": self._get_recommended_artists(), "albums": self._get_recommended_albums(), "tracks": self._get_recommended_tracks()}
        if cache_key:
            self.cache.set(cache_key, result)
        return result

    def _get_recommended_artists(self) -> list[dict[str, Any]]:
        method = "user.getTopArtists" if self.username else "chart.getTopArtists"
        kwargs = {"user": self.username, "limit": 20, "period": "6month"} if self.username else {"limit": 20}
        try:
            response = retry_with_backoff(lambda: self.http.request(method, timeout=(5, 10), **kwargs))
            if response is None:
                return []
            response.raise_for_status()
            data = response.json()
            artists = data.get("topartists", {}).get("artist", []) if self.username else data.get("artists", {}).get("artist", [])
            return [{"name": item.get("name", ""), "listeners": item.get("listeners", 0), "match": 1.0, "playcount": item.get("playcount", 0), "url": item.get("url", "")} for item in artists if isinstance(item, dict)][:20]
        except Exception:
            return []

    def _get_recommended_albums(self) -> list[dict[str, Any]]:
        if not self.username:
            return []
        try:
            response = retry_with_backoff(lambda: self.http.request("user.getTopAlbums", timeout=(5, 10), user=self.username, limit=12, period="6month"))
            if response is None:
                return []
            response.raise_for_status()
            albums = response.json().get("topalbums", {}).get("album", [])
            return [{"name": item.get("name", ""), "artist": self.extract_artist_name(item.get("artist")), "playcount": item.get("playcount", 0), "url": item.get("url", ""), "similarity": 1.0} for item in albums if isinstance(item, dict)][:12]
        except Exception:
            return []

    def _get_recommended_tracks(self) -> list[dict[str, Any]]:
        method = "user.getTopTracks" if self.username else "chart.getTopTracks"
        kwargs = {"user": self.username, "limit": 20, "period": "6month"} if self.username else {"limit": 20}
        try:
            response = retry_with_backoff(lambda: self.http.request(method, timeout=(5, 10), **kwargs))
            if response is None:
                return []
            response.raise_for_status()
            tracks = response.json().get("toptracks", {}).get("track", [])
            return [{"name": item.get("name", ""), "artist": self.extract_artist_name(item.get("artist")), "playcount": item.get("playcount", 0), "url": item.get("url", ""), "similarity": 1.0} for item in tracks if isinstance(item, dict)][:20]
        except Exception:
            return []


_lastfm_service: LastFmService | None = None


def get_lastfm_track_info(artist: str, title: str, api_key: str = "") -> dict[str, Any]:
    global _lastfm_service
    if _lastfm_service is None or _lastfm_service.api_key != api_key:
        _lastfm_service = LastFmService(api_key)
    return _lastfm_service.get_track_info(artist, title)


def get_lastfm_recommendations(api_key: str, username: str | None = None, db_connection=None) -> dict[str, Any]:
    return LastFmService(api_key, username=username, db_connection=db_connection).get_recommendations()
