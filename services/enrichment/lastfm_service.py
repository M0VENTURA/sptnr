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
from helpers.normalization_service import (
    FEAT_SUFFIX_RE,
    strip_cover_attribution,
    strip_featured_guest_suffix,
)
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
                Defaults to ~/.cache/popularr if not provided.
        """
        self.cache_dir = Path(cache_dir) if cache_dir else Path.home() / ".cache" / "popularr"
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

    # Uses the canonical FEAT_SUFFIX_RE from helpers.normalization_service
    # (handles both plain "feat. Guest" and bracket "[feat. Guest]" notation).

    def __init__(self, api_key: str, username: str | None = None, http_client: LastFmHttpClient | None = None, db_connection=None):
        self.api_key = api_key or ""
        self.username = username
        self.http = http_client or LastFmHttpClient(api_key=api_key)
        self.cache = RecommendationCache()
        self.db_connection = db_connection

        self.mb_client = None

    def get_artist_top_tracks(self, artist: str, limit: int = 100) -> list[dict[str, Any]]:
        """Fetch an artist's top tracks from Last.fm.

        Returns raw track dicts (``name``, ``listeners``, ``playcount``).
        Used by the popularity pipeline to aggregate listener counts across
        title variants (e.g. "Song" vs "Song (feat. Guest)").
        """
        if not self.api_key or not artist:
            return []
        try:
            data = self.http.get_json(
                "artist.getTopTracks",
                artist=artist,
                limit=max(1, min(int(limit), 200)),
            )
            tracks = (data.get("toptracks") or {}).get("track") or []
            if isinstance(tracks, dict):
                tracks = [tracks]
            return [t for t in tracks if isinstance(t, dict)]
        except Exception as exc:
            logger.debug("Last.fm artist top tracks failed for %s: %s", artist, exc)
            return []

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
        return cls.clean_spaces(FEAT_SUFFIX_RE.split(artist, maxsplit=1)[0])

    @classmethod
    def normalize_artist_for_compare(cls, artist: str) -> str:
        """Normalise artist name for fuzzy matching — lowercase, collapse separators."""
        if not artist:
            return ""
        value = cls.clean_spaces(artist).lower()
        value = FEAT_SUFFIX_RE.split(value, maxsplit=1)[0]
        value = re.sub(r"\s*(?:\+|&|/|×|\bx\b|\bvs\b|\bwith\b)\s*", " & ", value, flags=re.IGNORECASE)
        return cls.clean_spaces(value)

    @classmethod
    def _strip_bracketed_content(cls, value: str) -> str:
        """Remove bracketed/parenthesized content from an artist string."""
        return re.sub(r"\s*[\[\(][^\]\)]*[\]\)]", "", value or "").strip()

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

        # Also try with ALL bracketed content stripped — Last.fm often returns
        # artist names like "D'artagnan [feat. Melissa Bonny]" or similar.
        no_brackets = cls._strip_bracketed_content(artist)
        if no_brackets:
            add(no_brackets)
            no_brackets_primary = cls.strip_featured_artist(no_brackets)
            if no_brackets_primary:
                add(no_brackets_primary)

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
        """Fetch track listeners/playcount with safer multi-artist handling.

        Tries multiple artist name variants (including bracket-stripped versions)
        and falls back to ``track.search`` when direct ``track.getInfo`` returns
        no useful data.
        """
        if not self.api_key:
            return self._empty_track_info(artist, title)

        # Strip cover attributions so direct lookups target the canonical Last.fm
        # row. Last.fm titles a cover as just the song name ("Gangnam Style"),
        # while local files add the attribution ("Gangnam Style (PSY Cover)") —
        # querying with the raw title lands on a low-listen album-only row.
        title = strip_cover_attribution(title) or title

        best_result: dict[str, Any] | None = None
        best_tuple = (-1, -1, -1)

        if track_mbid:
            mbid_result = self._get_track_info_once(artist, title, track_mbid=track_mbid)
            best_result = mbid_result
            best_tuple = (self.artist_match_score(artist, mbid_result.get("returned_artist", "")), int(mbid_result.get("listeners", 0) or 0), int(mbid_result.get("track_play", 0) or 0))
            if best_tuple[0] >= 90 and (best_tuple[1] > 0 or best_tuple[2] > 0):
                return self._normalise_track_result(best_result, artist)

        # Try all artist candidate variants with the given (normalised) title
        for candidate_artist in self.build_artist_lookup_candidates(artist):
            candidate = self._get_track_info_once(candidate_artist, title)
            artist_score = self.artist_match_score(artist, candidate.get("returned_artist", ""))
            # Never accept a result from a DIFFERENT artist. Last.fm autocorrect
            # can return another band's song that happens to share the title;
            # without this guard a wrong-artist match with a handful of
            # listeners gets cached as a "fresh" popularity value for a week.
            if artist_score < 60:
                continue
            candidate_tuple = (artist_score, int(candidate.get("listeners", 0) or 0), int(candidate.get("track_play", 0) or 0))
            if candidate_tuple > best_tuple:
                best_tuple = candidate_tuple
                best_result = candidate

        # If all direct lookups returned empty, fall back to track.search
        # which does broader matching (handles cases where Last.fm has the
        # feat. in the title, e.g. "Herzblut (feat. Melissa Bonny)").  Every
        # matching version of the song is summed — the album version and the
        # single version are separate Last.fm tracks with separate counts,
        # and the album one is usually the low-listen entry.
        if best_result is None or (best_result.get("listeners", 0) == 0 and best_result.get("track_play", 0) == 0):
            search_results = self.search_track(artist, title, limit=20)
            if search_results:
                try:
                    from services.popularity.popularity_matching import normalize_for_aggregation as _nfa
                    _target = _nfa(title)
                except Exception:
                    _target = None
                total_listeners = 0
                total_play = 0
                best_name = title
                best_url = ""
                best_artist = ""
                _seen_urls: set[str] = set()
                for track in search_results:
                    track_name = track.get("name", "")
                    if _target and _nfa(track_name) != _target:
                        continue
                    _url = str(track.get("url") or "").strip()
                    _dedupe_key = _url or f"{track_name.lower()}|{track.get('artist', '')}".strip().lower()
                    if _dedupe_key in _seen_urls:
                        continue
                    _seen_urls.add(_dedupe_key)
                    track_listeners = int(track.get("listeners", 0) or 0)
                    track_play = int(track.get("playcount", 0) or 0)
                    total_listeners += track_listeners
                    total_play += track_play
                    if track_listeners > 0:
                        best_name = track_name
                        best_url = _url
                        best_artist = track.get("artist", "")
                if total_listeners > 0:
                    best_result = {
                        "track_play": total_listeners,
                        "listeners": total_listeners,
                        "toptags": {},
                        "lookup_artist": artist,
                        "returned_artist": best_artist,
                        "track_name": best_name,
                        "url": best_url,
                        "album": "",
                    }

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
        """Search tracks and rank by artist match score.

        Passes both *artist* and *track* to the Last.fm API so results are
        narrowed to the correct artist from the start, rather than searching
        by title alone and then filtering client-side.
        """
        if not self.api_key:
            return []
        try:
            data = self.http.get_json("track.search", timeout=(5, 10), artist=artist, track=title, limit=limit)
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

    def search_album(self, album: str, artist: str = "", limit: int = 10) -> list[dict[str, Any]]:
        """Search Last.fm albums, keeping matches whose artist matches ``artist``.

        ``album.search`` accepts no artist filter, so the returned list is
        filtered client-side with the same artist-match scoring as track
        searches (feat. credits resolve to the primary artist).
        """
        if not self.api_key:
            return []
        try:
            data = self.http.get_json(
                "album.search", timeout=(5, 10), album=album, limit=max(1, min(limit, 100))
            )
            albums = data.get("results", {}).get("albummatches", {}).get("album", [])
            if isinstance(albums, dict):
                albums = [albums]
            output = []
            for item in albums or []:
                if not isinstance(item, dict):
                    continue
                item_artist = self.extract_artist_name(item.get("artist"))
                if artist and self.artist_match_score(artist, item_artist) < 60:
                    continue
                output.append(item)
            return output
        except Exception as exc:
            logger.debug("Last.fm album.search failed for '%s': %s", album, exc)
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

    def has_title_track(self, artist: str, album: str) -> bool:
        """Check whether the album's tracklist contains its own title track.

        Ported from the legacy client — the single-detection EP rule treats a
        4-6 track album WITH a title track as an EP, so this method must
        exist on the facade (a missing method previously fell into the
        caller's ``except`` and defaulted every 4-6 track album to "EP")."""
        album_data = self._album_get_info(artist, album)
        if not album_data:
            return False
        album_name = (album_data.get("name") or album).lower().strip()
        tracks = album_data.get("tracks", {})
        track_list: list = []
        if isinstance(tracks, dict):
            track_data = tracks.get("track", [])
            if isinstance(track_data, dict):
                track_list = [track_data]
            elif isinstance(track_data, list):
                track_list = track_data
        elif isinstance(tracks, list):
            track_list = tracks
        for track in track_list:
            if isinstance(track, dict):
                track_name = (track.get("name") or "").lower().strip()
                if track_name == album_name:
                    return True
        return False

    @staticmethod
    def _is_genuine_release(album_data: dict[str, Any]) -> bool:
        """Scrobble-derived albums (users tagging files/YouTube with the track
        name as the album) carry no release metadata; genuine releases on
        Last.fm expose a MusicBrainz ID, a real release date, or a wiki
        publication date.  Last.fm's "unknown date" placeholder
        (``14 Jun 2005, 00:00``) is not evidence."""
        if album_data.get("mbid"):
            return True
        released = str(album_data.get("releasedate") or "").strip()
        if released and released.lower() not in ("14 jun 2005, 00:00", "14 jun 2005"):
            return True
        wiki = album_data.get("wiki")
        if isinstance(wiki, dict) and str(wiki.get("published") or "").strip():
            return True
        return False

    def check_track_as_single(self, artist: str, track_title: str) -> bool:
        """Return True when Last.fm has a genuine release named after the
        track with < 6 tracks.

        Two junk filters guard the legacy name+count check:
        - the returned album must actually belong to ``artist`` — Last.fm's
          autocorrect can resolve a missing "artist/track-title" album to a
          DIFFERENT artist's album with the same name;
        - the album must carry release metadata (MBID / real release date /
          wiki date) — scrobble-derived albums named after popular tracks
          (Last.fm albums are user-scrobble entities, not an authoritative
          release database) are not single evidence.
        """
        album_data = self._album_get_info(artist, track_title)
        if not album_data:
            return False
        # The Last.fm single may carry the guest credit ("Herzblut (feat.
        # Melissa Bonny)") while the local track is the plain title
        # ("Herzblut") — strip the featured-guest suffix from BOTH sides
        # before comparing so the single still confirms (mirrors the search
        # fallback in single_detection_service._detect_lastfm).
        album_name = (album_data.get("name") or "").lower().strip()
        track_name = track_title.lower().strip()
        if strip_featured_guest_suffix(album_name) != strip_featured_guest_suffix(track_name):
            return False
        returned_artist = self.extract_artist_name(album_data.get("artist"))
        if not returned_artist or self.artist_match_score(artist, returned_artist) < 90:
            return False
        if not self._is_genuine_release(album_data):
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

    def get_similar_artists(self, artist: str, limit: int = 10) -> list[dict[str, Any]]:
        """Fetch similar artists from Last.fm for a given artist.

        Mirrors the legacy ``artist.getSimilar`` lookup: tries each artist
        lookup candidate (full credit, feat.-stripped primary, bracket-stripped)
        and returns ``[{"name": ..., "match": ...}]`` for the first candidate
        that returns results.
        """
        if not self.api_key or not artist:
            return []
        for lookup_artist in self.build_artist_lookup_candidates(artist):
            try:
                data = self.http.get_json(
                    "artist.getSimilar",
                    timeout=(5, 10),
                    artist=lookup_artist,
                    limit=max(1, min(int(limit), 100)),
                )
                if "error" in data:
                    continue
                similar_artists = (data.get("similarartists") or {}).get("artist", [])
                if isinstance(similar_artists, dict):
                    similar_artists = [similar_artists]
                result: list[dict[str, Any]] = []
                for artist_obj in similar_artists or []:
                    if not isinstance(artist_obj, dict):
                        continue
                    name = artist_obj.get("name", "")
                    if not name:
                        continue
                    try:
                        match = float(artist_obj.get("match", 0.0))
                    except Exception:
                        match = 0.0
                    result.append({"name": name, "match": match})
                if result:
                    return result
            except Exception as exc:
                logger.debug("Last.fm similar artists failed for '%s': %s", lookup_artist, exc)
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
