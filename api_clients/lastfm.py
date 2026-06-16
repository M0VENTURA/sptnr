"""Last.fm API client module with enhanced discovery features and safer multi-artist handling."""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Optional

from requests.exceptions import ConnectionError, HTTPError, RequestException, Timeout

from . import session

logger = logging.getLogger(__name__)

# Configuration for DiscoveryLastFM-inspired features
LASTFM_CONFIG = {
    "MIN_ARTIST_PLAYS": 20,
    "MIN_SIMILARITY_SCORE": 0.46,
    "MAX_SIMILAR_PER_ARTIST": 5,
    "MAX_ALBUMS_PER_ARTIST": 5,
    "RECENT_MONTHS": 3,
    "CACHE_TTL_HOURS": 24,
    "MAX_RETRIES": 3,
    "RETRY_BACKOFF": 1.5,
    "RATE_LIMIT_DELAY": 0.5,
}


class RecommendationCache:
    """Simple JSON-based cache for recommendations with TTL."""

    def __init__(self, cache_dir: str | None = None):
        if cache_dir is None:
            cache_dir = Path.home() / ".cache" / "sptnr"

        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_file = self.cache_dir / "lastfm_recommendations.json"

    def get(self, key: str) -> dict | None:
        try:
            if not self.cache_file.exists():
                return None

            with open(self.cache_file, "r", encoding="utf-8") as f:
                cache = json.load(f)

            if key not in cache:
                return None

            entry = cache[key]
            age_hours = (time.time() - entry["timestamp"]) / 3600

            if age_hours > LASTFM_CONFIG["CACHE_TTL_HOURS"]:
                del cache[key]
                self._save_cache(cache)
                return None

            return entry["data"]
        except Exception as e:
            logger.debug("Cache read error for %s: %s", key, e)
            return None

    def set(self, key: str, value: dict) -> None:
        try:
            cache = {}
            if self.cache_file.exists():
                with open(self.cache_file, "r", encoding="utf-8") as f:
                    cache = json.load(f)

            cache[key] = {
                "data": value,
                "timestamp": time.time(),
            }

            self._save_cache(cache)
        except Exception as e:
            logger.debug("Cache write error for %s: %s", key, e)

    def _save_cache(self, cache: dict) -> None:
        try:
            temp_file = self.cache_file.with_suffix(".tmp")
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(cache, f, indent=2)
            temp_file.replace(self.cache_file)
        except Exception as e:
            logger.debug("Cache save failed: %s", e)


def retry_with_backoff(
    func,
    max_retries: int = 3,
    backoff_factor: float = 1.5,
    rate_limit_delay: float = 0.5,
):
    """Retry a function with exponential backoff."""
    import random

    for attempt in range(max_retries):
        try:
            time.sleep(rate_limit_delay)
            result = func()

            if hasattr(result, "status_code") and result.status_code == 429:
                if attempt == max_retries - 1:
                    logger.error("Rate limited (429) - max retries exceeded after %s attempts", attempt + 1)
                    result.raise_for_status()

                retry_after = result.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait_time = float(retry_after)
                        logger.warning("Rate limited (429) - waiting %ss as per Retry-After", wait_time)
                        time.sleep(wait_time)
                    except ValueError:
                        wait_time = (backoff_factor**attempt) * 2
                        logger.warning("Rate limited (429) - exponential backoff for %.2fs", wait_time)
                        time.sleep(wait_time)
                else:
                    wait_time = (backoff_factor**attempt) * 2
                    logger.warning("Rate limited (429) - exponential backoff for %.2fs", wait_time)
                    time.sleep(wait_time)
                continue

            return result

        except (ConnectionError, ConnectionResetError) as e:
            if attempt == max_retries - 1:
                logger.error("Connection error after %s attempts: %s", attempt + 1, e)
                raise

            wait_time = (backoff_factor**attempt) + random.uniform(0, 1)
            logger.warning(
                "Connection error (attempt %s/%s), retrying after %.2fs: %s",
                attempt + 1,
                max_retries,
                wait_time,
                e,
            )
            time.sleep(wait_time)

        except Timeout as e:
            if attempt == max_retries - 1:
                logger.error("Request timeout after %s attempts: %s", attempt + 1, e)
                raise

            wait_time = (backoff_factor**attempt) + random.uniform(0, 1)
            logger.warning(
                "Request timeout (attempt %s/%s), retrying after %.2fs: %s",
                attempt + 1,
                max_retries,
                wait_time,
                e,
            )
            time.sleep(wait_time)

        except HTTPError as e:
            if hasattr(e.response, "status_code") and e.response.status_code >= 500:
                if attempt == max_retries - 1:
                    logger.error("Server error %s after %s attempts: %s", e.response.status_code, attempt + 1, e)
                    raise

                wait_time = (backoff_factor**attempt) + random.uniform(0, 1)
                logger.warning(
                    "Server error %s (attempt %s/%s), retrying after %.2fs",
                    e.response.status_code,
                    attempt + 1,
                    max_retries,
                    wait_time,
                )
                time.sleep(wait_time)
            else:
                logger.error(
                    "Non-retryable error %s: %s",
                    e.response.status_code if hasattr(e.response, "status_code") else "unknown",
                    e,
                )
                raise

        except RequestException as e:
            if attempt == max_retries - 1:
                logger.error("Request error after %s attempts: %s", attempt + 1, e)
                raise

            wait_time = (backoff_factor**attempt) + random.uniform(0, 1)
            logger.warning(
                "Request error (attempt %s/%s), retrying after %.2fs: %s",
                attempt + 1,
                max_retries,
                wait_time,
                e,
            )
            time.sleep(wait_time)

        except Exception as e:
            if attempt == max_retries - 1:
                logger.error("Unexpected error after %s attempts: %s", attempt + 1, e)
                raise

            wait_time = (backoff_factor**attempt) + random.uniform(0, 1)
            logger.debug("Retry attempt %s/%s after %.2fs: %s", attempt + 1, max_retries, wait_time, e)
            time.sleep(wait_time)

    return None


class LastFmClient:
    """Last.fm API wrapper with safer multi-artist handling."""

    _FEATURE_RE = re.compile(r"\s+(?:feat\.?|featuring|ft\.?)\s+", re.IGNORECASE)

    def __init__(self, api_key: str, username: str = None, http_session=None, db_connection=None):
        self.api_key = api_key
        self.username = username
        self.session = http_session or session
        self.base_url = "https://ws.audioscrobbler.com/2.0/"
        self.cache = RecommendationCache()
        self.db_connection = db_connection

        try:
            from .musicbrainz import MusicBrainzClient
            self.mb_client = MusicBrainzClient()
        except Exception as e:
            logger.debug("MusicBrainz client not available for album filtering: %s", e)
            self.mb_client = None

    # -------------------------------------------------------------------------
    # Artist-string helpers
    # -------------------------------------------------------------------------

    @staticmethod
    def _clean_spaces(text: str) -> str:
        return re.sub(r"\s+", " ", (text or "")).strip()

    @staticmethod
    def _extract_artist_name(artist_field) -> str:
        """Handle Last.fm's inconsistent artist payload shapes (#text vs name vs string)."""
        if isinstance(artist_field, str):
            return artist_field.strip()
        if isinstance(artist_field, dict):
            name = (artist_field.get("name") or artist_field.get("#text") or "").strip()
            if name:
                return name
            nested_artist = artist_field.get("artist")
            if isinstance(nested_artist, dict):
                return (nested_artist.get("name") or nested_artist.get("#text") or "").strip()
            if isinstance(nested_artist, str):
                return nested_artist.strip()
        return ""

    @classmethod
    def _strip_featured_artist(cls, artist: str) -> str:
        """Return canonical primary artist by removing feat./ft./featuring suffixes."""
        if not artist:
            return ""
        primary = cls._FEATURE_RE.split(artist, maxsplit=1)[0]
        return cls._clean_spaces(primary)

    @classmethod
    def _extract_featured_artists(cls, artist: str) -> list[str]:
        """Extract featured artists from the feat./ft./featuring suffix only."""
        if not artist:
            return []

        parts = cls._FEATURE_RE.split(artist, maxsplit=1)
        if len(parts) < 2:
            return []

        featured_part = cls._clean_spaces(parts[1])
        featured = re.split(r"\s*(?:&|and|,|/)\s*", featured_part, flags=re.IGNORECASE)
        return [cls._clean_spaces(x) for x in featured if cls._clean_spaces(x)]

    @classmethod
    def _normalize_artist_for_compare(cls, artist: str) -> str:
        """
        Comparison-only normalization.

        Important:
        - strips featured suffix
        - lowercases
        - normalizes common collaboration separators
        - preserves commas instead of blindly splitting them
        """
        if not artist:
            return ""

        s = cls._clean_spaces(artist).lower()
        s = cls._FEATURE_RE.split(s, maxsplit=1)[0]
        s = re.sub(r"\s*(?:\+|&|/|×|\bx\b|\bvs\b|\bwith\b)\s*", " & ", s, flags=re.IGNORECASE)
        s = cls._clean_spaces(s)
        return s

    @classmethod
    def _build_artist_lookup_candidates(cls, artist: str) -> list[str]:
        """
        Build a conservative ordered set of lookup candidates.
        We intentionally do NOT promote featured artists as standalone primary candidates.
        """
        if not artist:
            return []

        candidates = []
        seen = set()

        def add(value: str):
            value = cls._clean_spaces(value)
            if value and value.lower() not in seen:
                candidates.append(value)
                seen.add(value.lower())

        original = cls._clean_spaces(artist)
        primary = cls._strip_featured_artist(artist)

        add(original)
        add(primary)

        normalized_primary = re.sub(
            r"\s*(?:\+|&|/|×|\bx\b|\bvs\b|\bwith\b)\s*",
            " & ",
            primary,
            flags=re.IGNORECASE,
        )
        add(normalized_primary)

        return candidates

    @classmethod
    def _artist_match_score(cls, query_artist: str, returned_artist: str) -> int:
        """
        Score how well the returned artist string matches the requested artist string.
        Higher = better.
        """
        q_raw = cls._clean_spaces(query_artist).lower()
        r_raw = cls._clean_spaces(returned_artist).lower()

        q_norm = cls._normalize_artist_for_compare(query_artist)
        r_norm = cls._normalize_artist_for_compare(returned_artist)

        q_primary = cls._strip_featured_artist(query_artist).lower()
        r_primary = cls._strip_featured_artist(returned_artist).lower()

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

    # -------------------------------------------------------------------------
    # Internal DB helpers
    # -------------------------------------------------------------------------

    def _album_exists(self, artist: str, album: str) -> bool:
        if not self.db_connection:
            return False

        try:
            if callable(self.db_connection):
                conn = self.db_connection()
            else:
                conn = self.db_connection

            cursor = conn.cursor()
            placeholder = "%s"

            cursor.execute(
                f"SELECT 1 FROM tracks WHERE LOWER(artist) = LOWER({placeholder}) AND LOWER(album) = LOWER({placeholder}) LIMIT 1",
                (artist, album),
            )
            result = cursor.fetchone()

            if callable(self.db_connection):
                try:
                    conn.close()
                except Exception:
                    pass

            return result is not None
        except Exception as e:
            logger.debug("Error checking if album exists in database: %s", e)
            return False

    def _is_studio_album(self, artist: str, album: str) -> bool:
        if not self.mb_client:
            return True

        try:
            # NOTE: This assumes your MusicBrainzClient has search_releases().
            releases = self.mb_client.search_releases(f'artist:"{artist}" AND release:"{album}"')

            if not releases:
                return True

            first_release = releases[0] if isinstance(releases, list) else releases
            album_type = (first_release.get("primaryType") or "").lower()
            secondary_types = [t.lower() for t in (first_release.get("secondaryTypes") or [])]

            if album_type != "album":
                logger.debug("Filtering out %s by %s: type=%s", album, artist, album_type)
                return False

            excluded = {"compilation", "live", "remix", "ep"}
            if any(t in excluded for t in secondary_types):
                logger.debug("Filtering out %s by %s: secondary_types=%s", album, artist, secondary_types)
                return False

            media = first_release.get("media", [])
            total_tracks = 0
            for disc in media:
                tracks = disc.get("tracks", [])
                total_tracks += len(tracks)

            if total_tracks <= 3:
                logger.debug("Filtering out %s by %s: only %s tracks", album, artist, total_tracks)
                return False

            return True
        except Exception as e:
            logger.debug("MusicBrainz filtering failed for %s/%s: %s", album, artist, e)
            return True

    # -------------------------------------------------------------------------
    # Last.fm core helpers
    # -------------------------------------------------------------------------

    def _request(self, params: dict[str, Any], timeout: tuple[int, int] = (5, 10)):
        return self.session.get(self.base_url, params=params, timeout=timeout)

    def _build_basic_params(self, method: str, **kwargs) -> dict[str, Any]:
        params = {
            "method": method,
            "api_key": self.api_key,
            "format": "json",
        }
        params.update(kwargs)
        return params

    # -------------------------------------------------------------------------
    # Track lookups
    # -------------------------------------------------------------------------

    def _get_track_info_once(
        self,
        artist: str,
        title: str,
        track_mbid: str | None = None,
    ) -> dict[str, Any]:
        """Perform a single Last.fm track.getInfo call."""
        params = self._build_basic_params("track.getInfo", autocorrect=1)

        if track_mbid:
            params["mbid"] = track_mbid
        else:
            params["artist"] = artist
            params["track"] = title

        try:
            res = self._request(params, timeout=(5, 10))
            res.raise_for_status()
            response_data = res.json()

            if "error" in response_data:
                error_code = response_data.get("error")
                error_msg = response_data.get("message", "Unknown error")
                logger.warning(
                    "Last.fm API error %s for '%s' by '%s': %s",
                    error_code, title, artist, error_msg
                )
                return {
                    "track_play": 0,
                    "listeners": 0,
                    "toptags": {},
                    "lookup_artist": artist,
                    "returned_artist": "",
                    "track_name": title,
                    "url": "",
                    "album": "",
                }

            data = response_data.get("track", {})
            returned_artist = self._extract_artist_name(data.get("artist"))
            album_title = ""
            album_data = data.get("album")
            if isinstance(album_data, dict):
                album_title = album_data.get("title") or album_data.get("name") or ""

            track_play = int(data.get("playcount", 0) or 0)
            listeners = int(data.get("listeners", 0) or 0)
            toptags = data.get("toptags", {}) or {}

            return {
                "track_play": track_play,
                "listeners": listeners,
                "toptags": toptags,
                "lookup_artist": artist,
                "returned_artist": returned_artist,
                "track_name": data.get("name", title),
                "url": data.get("url", ""),
                "album": album_title,
            }

        except (ConnectionError, ConnectionResetError) as e:
            logger.error("Connection error fetching track '%s' by '%s': %s", title, artist, e)
        except Timeout as e:
            logger.error("Timeout fetching track '%s' by '%s': %s", title, artist, e)
        except HTTPError as e:
            status_code = e.response.status_code if hasattr(e.response, "status_code") else "unknown"
            logger.error("HTTP error %s fetching track '%s' by '%s': %s", status_code, title, artist, e)
        except Exception as e:
            logger.error("Failed to fetch Last.fm info for '%s' by '%s': %s", title, artist, e)

        return {
            "track_play": 0,
            "listeners": 0,
            "toptags": {},
            "lookup_artist": artist,
            "returned_artist": "",
            "track_name": title,
            "url": "",
            "album": "",
        }

    def get_track_info(self, artist: str, title: str, track_mbid: str | None = None) -> dict[str, Any]:
        """
        Fetch track listeners, playcount, and metadata from Last.fm.

        Strategy:
        1. If a track MBID exists, try MBID lookup first.
        2. Try a conservative set of artist lookup candidates.
        3. Rank candidates by artist match score first, listeners/playcount second.
        """
        if not self.api_key:
            logger.warning("Last.fm API key missing. Skipping lookup.")
            return {
                "track_play": 0,
                "listeners": 0,
                "toptags": {},
                "lookup_artist": artist,
                "returned_artist": "",
            }

        best_result = None
        best_tuple = (-1, -1, -1)  # (artist_match_score, listeners, playcount)

        # MBID-first if available
        if track_mbid:
            mbid_result = self._get_track_info_once(artist=artist, title=title, track_mbid=track_mbid)
            mbid_match_score = self._artist_match_score(artist, mbid_result.get("returned_artist", ""))
            mbid_tuple = (
                mbid_match_score,
                int(mbid_result.get("listeners", 0) or 0),
                int(mbid_result.get("track_play", 0) or 0),
            )
            best_result = mbid_result
            best_tuple = mbid_tuple

            if mbid_match_score >= 90 and (
                mbid_result.get("listeners", 0) > 0 or mbid_result.get("track_play", 0) > 0
            ):
                return {
                    "track_play": int(best_result.get("track_play", 0) or 0),
                    "listeners": int(best_result.get("listeners", 0) or 0),
                    "toptags": best_result.get("toptags", {}) or {},
                    "lookup_artist": best_result.get("lookup_artist", artist),
                    "returned_artist": best_result.get("returned_artist", ""),
                    "url": best_result.get("url", ""),
                    "album": best_result.get("album", ""),
                }

        candidates = self._build_artist_lookup_candidates(artist)

        for candidate_artist in candidates:
            candidate = self._get_track_info_once(candidate_artist, title)
            candidate_tuple = (
                self._artist_match_score(artist, candidate.get("returned_artist", "")),
                int(candidate.get("listeners", 0) or 0),
                int(candidate.get("track_play", 0) or 0),
            )

            if candidate_tuple > best_tuple:
                best_tuple = candidate_tuple
                best_result = candidate
                logger.debug(
                    "Best Last.fm candidate for '%s': query='%s', returned='%s', score=%s, listeners=%s, playcount=%s",
                    title,
                    candidate_artist,
                    candidate.get("returned_artist", ""),
                    candidate_tuple[0],
                    candidate_tuple[1],
                    candidate_tuple[2],
                )

        if not best_result:
            best_result = {
                "track_play": 0,
                "listeners": 0,
                "toptags": {},
                "lookup_artist": artist,
                "returned_artist": "",
                "url": "",
                "album": "",
            }

        return {
            "track_play": int(best_result.get("track_play", 0) or 0),
            "listeners": int(best_result.get("listeners", 0) or 0),
            "toptags": best_result.get("toptags", {}) or {},
            "lookup_artist": best_result.get("lookup_artist", artist),
            "returned_artist": best_result.get("returned_artist", ""),
            "url": best_result.get("url", ""),
            "album": best_result.get("album", ""),
        }

    def search_track(self, artist: str, title: str, limit: int = 10) -> list[dict]:
        """Search for tracks on Last.fm and rank artist matches safely."""
        if not self.api_key:
            logger.debug("Last.fm API key missing. Skipping search.")
            return []

        params = self._build_basic_params("track.search", track=title, limit=limit)

        try:
            res = self._request(params, timeout=(5, 10))
            res.raise_for_status()
            data = res.json()

            results = data.get("results", {})
            trackmatches = results.get("trackmatches", {})
            tracks = trackmatches.get("track", [])

            if isinstance(tracks, dict):
                tracks = [tracks]

            filtered_tracks = []

            for track in tracks:
                track_artist = self._extract_artist_name(track.get("artist"))
                score = self._artist_match_score(artist, track_artist)

                if score >= 60:
                    try:
                        listeners = int(track.get("listeners", 0) or 0)
                    except Exception:
                        listeners = 0

                    filtered_tracks.append(
                        {
                            "name": track.get("name", ""),
                            "artist": track_artist,
                            "listeners": listeners,
                            "url": track.get("url", ""),
                            "_score": score,
                        }
                    )

            filtered_tracks.sort(key=lambda t: (t["_score"], t["listeners"]), reverse=True)

            for item in filtered_tracks:
                item.pop("_score", None)

            return filtered_tracks

        except (ConnectionError, ConnectionResetError) as e:
            logger.debug("Connection error searching for '%s' by '%s': %s", title, artist, e)
            return []
        except Timeout as e:
            logger.debug("Timeout searching for '%s' by '%s': %s", title, artist, e)
            return []
        except HTTPError as e:
            status_code = e.response.status_code if hasattr(e.response, "status_code") else "unknown"
            logger.debug("HTTP error %s searching for '%s' by '%s': %s", status_code, title, artist, e)
            return []
        except Exception as e:
            logger.debug("Failed to search Last.fm for '%s' by '%s': %s", title, artist, e)
            return []

    # -------------------------------------------------------------------------
    # Album lookups
    # -------------------------------------------------------------------------

    def _album_lookup_candidates(self, artist: str) -> list[str]:
        """Conservative candidate order for album-related lookups."""
        return self._build_artist_lookup_candidates(artist)

    def _album_get_info(self, artist: str, album: str) -> dict:
        """Try album.getInfo across artist variants and return the first successful album payload."""
        if not self.api_key:
            return {}

        for lookup_artist in self._album_lookup_candidates(artist):
            params = self._build_basic_params("album.getInfo", artist=lookup_artist, album=album)
            try:
                res = self._request(params, timeout=(5, 10))
                res.raise_for_status()
                payload = res.json()
                if "album" in payload and isinstance(payload["album"], dict):
                    payload["album"]["_lookup_artist"] = lookup_artist
                    return payload["album"]
            except HTTPError as e:
                status_code = e.response.status_code if hasattr(e.response, "status_code") else "unknown"
                if status_code == 404:
                    logger.debug("Album '%s' not found for '%s' (404)", album, lookup_artist)
                    continue
                logger.debug("HTTP error %s fetching album '%s' by '%s': %s", status_code, album, lookup_artist, e)
            except (ConnectionError, ConnectionResetError) as e:
                logger.debug("Connection error fetching album '%s' by '%s': %s", album, lookup_artist, e)
            except Timeout as e:
                logger.debug("Timeout fetching album '%s' by '%s': %s", album, lookup_artist, e)
            except Exception as e:
                logger.debug("Failed to fetch album '%s' by '%s': %s", album, lookup_artist, e)

        return {}

    def get_album_track_count(self, artist: str, album: str) -> int:
        """Fetch album track count from Last.fm."""
        if not self.api_key:
            logger.debug("Last.fm API key missing. Skipping album lookup.")
            return 0

        album_data = self._album_get_info(artist, album)
        if not album_data:
            return 0

        tracks = album_data.get("tracks", {})
        if isinstance(tracks, dict):
            track_list = tracks.get("track", [])
            if isinstance(track_list, dict):
                return 1
            if isinstance(track_list, list):
                return len(track_list)
        elif isinstance(tracks, list):
            return len(tracks)

        return 0

    def has_title_track(self, artist: str, album: str) -> bool:
        """Check if the album has a title track."""
        if not self.api_key:
            logger.debug("Last.fm API key missing. Skipping album lookup.")
            return False

        album_data = self._album_get_info(artist, album)
        if not album_data:
            return False

        album_name = (album_data.get("name") or album).lower().strip()
        tracks = album_data.get("tracks", {})
        track_list = []

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
                    logger.debug("Found title track '%s' matching album '%s'", track_name, album_name)
                    return True

        return False

    def check_track_as_single(self, artist: str, track_title: str) -> bool:
        """
        Check if a track exists as a single/album release on Last.fm.

        Returns True only if an album with the same name exists and has < 6 tracks.
        """
        if not self.api_key:
            logger.debug("Last.fm API key missing. Skipping single lookup.")
            return False

        album_data = self._album_get_info(artist, track_title)
        if not album_data:
            return False

        album_name = (album_data.get("name") or "").lower().strip()
        normalized_track = track_title.lower().strip()

        if album_name != normalized_track:
            return False

        tracks_data = album_data.get("tracks", {})
        track_count = 0

        if isinstance(tracks_data, dict):
            track_list = tracks_data.get("track", [])
            if isinstance(track_list, dict):
                track_count = 1
            elif isinstance(track_list, list):
                track_count = len(track_list)
        elif isinstance(tracks_data, list):
            track_count = len(tracks_data)

        if 0 < track_count < 6:
            logger.debug(
                "Found single/album '%s' matching track '%s' with %s tracks",
                album_name, track_title, track_count
            )
            return True

        logger.debug(
            "Found album '%s' matching track '%s' but has %s tracks (>=6)",
            album_name, track_title, track_count
        )
        return False

    # -------------------------------------------------------------------------
    # Temporal data
    # -------------------------------------------------------------------------

    def get_track_temporal_data(self, artist: str, title: str, track_mbid: str | None = None) -> dict:
        """
        Fetch available all-time popularity data from track.getInfo.
        Standard Last.fm API does not expose 7-day / 365-day breakdown here.
        """
        if not self.api_key:
            logger.debug("Last.fm API key missing. Skipping temporal lookup.")
            return {
                "all_time_listeners": 0,
                "all_time_playcount": 0,
                "7day_listeners": None,
                "365day_listeners": None,
                "momentum_score": 1.0,
                "popularity_trend": "unknown",
                "data_source": "unavailable",
            }

        try:
            info = self.get_track_info(artist, title, track_mbid=track_mbid)
            return {
                "all_time_listeners": int(info.get("listeners", 0) or 0),
                "all_time_playcount": int(info.get("track_play", 0) or 0),
                "7day_listeners": None,
                "365day_listeners": None,
                "momentum_score": 1.0,
                "popularity_trend": "unknown",
                "data_source": "standard_api_only",
            }
        except Exception as e:
            logger.debug("Failed to fetch temporal data for '%s' by '%s': %s", title, artist, e)
            return {
                "all_time_listeners": 0,
                "all_time_playcount": 0,
                "7day_listeners": None,
                "365day_listeners": None,
                "momentum_score": 1.0,
                "popularity_trend": "unknown",
                "data_source": "error",
            }

    # -------------------------------------------------------------------------
    # Similar artists / tags / artist info
    # -------------------------------------------------------------------------

    def get_similar_artists(self, artist: str, limit: int = 10) -> list:
        """Fetch similar artists from Last.fm for a given artist."""
        if not self.api_key:
            logger.debug("Last.fm API key missing. Skipping similar artists lookup.")
            return []

        limit = max(1, min(100, limit))

        # Prefer stripped primary artist for artist-level endpoints
        lookup_candidates = self._build_artist_lookup_candidates(artist)

        for lookup_artist in lookup_candidates:
            params = self._build_basic_params("artist.getSimilar", artist=lookup_artist, limit=limit)
            try:
                res = self._request(params, timeout=(5, 10))
                res.raise_for_status()
                data = res.json()

                if "error" in data:
                    logger.debug("Last.fm error for '%s': %s", lookup_artist, data.get("message", "unknown"))
                    continue

                similar_artists = data.get("similarartists", {}).get("artist", [])
                if isinstance(similar_artists, dict):
                    similar_artists = [similar_artists]

                result = []
                for artist_obj in similar_artists:
                    if isinstance(artist_obj, dict):
                        name = artist_obj.get("name", "")
                        try:
                            match = float(artist_obj.get("match", 0.0))
                        except Exception:
                            match = 0.0
                        if name:
                            result.append({"name": name, "match": match})

                if result:
                    logger.debug("Fetched %s similar artists for '%s' from Last.fm", len(result), lookup_artist)
                    return result

            except (ConnectionError, ConnectionResetError) as e:
                logger.debug("Connection error fetching similar artists for '%s': %s", lookup_artist, e)
            except Timeout as e:
                logger.debug("Timeout fetching similar artists for '%s': %s", lookup_artist, e)
            except Exception as e:
                logger.debug("Failed to fetch similar artists for '%s': %s", lookup_artist, e)

        return []

    def get_track_tags(self, artist: str, title: str, limit: int = 10) -> list:
        """Extract and format track tags from Last.fm using track.getTopTags."""
        if not self.api_key:
            logger.debug("Last.fm API key missing. Skipping tags lookup.")
            return []

        candidates = self._build_artist_lookup_candidates(artist)
        best_result = []
        best_score = (-1, -1)  # (artist_match_score, tag_count)

        for lookup_artist in candidates:
            try:
                params = self._build_basic_params("track.getTopTags", artist=lookup_artist, track=title)
                res = self._request(params, timeout=(5, 10))
                res.raise_for_status()
                data = res.json()

                toptags_data = data.get("toptags", {})
                tag_list = toptags_data.get("tag", [])

                # Some responses include artist info in @attr
                returned_artist = ""
                attr = toptags_data.get("@attr", {})
                if isinstance(attr, dict):
                    returned_artist = attr.get("artist", "") or lookup_artist

                if isinstance(tag_list, dict):
                    tag_list = [tag_list]

                current = []
                for tag_obj in tag_list[:limit]:
                    if isinstance(tag_obj, dict):
                        name = tag_obj.get("name", "")
                        count = tag_obj.get("count", 0)
                        if name:
                            try:
                                count = int(count) if count else 0
                            except Exception:
                                count = 0
                            current.append({"name": name, "count": count})

                score = (
                    self._artist_match_score(artist, returned_artist),
                    len(current),
                )

                if score > best_score:
                    best_score = score
                    best_result = current

            except Exception as e:
                logger.debug("Failed to fetch tags for '%s' by '%s': %s", title, lookup_artist, e)

        return best_result

    def get_artist_info(self, artist: str) -> dict:
        """Fetch artist bio and info from Last.fm."""
        if not self.api_key:
            logger.debug("Last.fm API key missing. Skipping artist info lookup.")
            return {"bio": "", "bio_text": "", "image": "", "similar": []}

        for lookup_artist in self._build_artist_lookup_candidates(artist):
            params = self._build_basic_params("artist.getInfo", artist=lookup_artist)
            try:
                res = self._request(params, timeout=(5, 10))
                res.raise_for_status()
                data = res.json().get("artist", {})

                if not data:
                    continue

                bio_html = data.get("bio", {}).get("content", "")
                bio_text = data.get("bio", {}).get("summary", "") or bio_html

                image_url = ""
                if isinstance(data.get("image"), list):
                    for img in reversed(data["image"]):
                        if img.get("#text"):
                            image_url = img.get("#text", "")
                            break

                return {
                    "bio": bio_html,
                    "bio_text": bio_text,
                    "image": image_url,
                    "similar": [],
                }
            except Exception as e:
                logger.debug("Failed to fetch artist info from Last.fm for '%s': %s", lookup_artist, e)

        return {"bio": "", "bio_text": "", "image": "", "similar": []}

    def get_artist_top_tags(self, artist: str, limit: int = 10) -> list:
        """Fetch top tags for an artist from Last.fm."""
        if not self.api_key:
            logger.debug("Last.fm API key missing. Skipping artist tags lookup.")
            return []

        limit = max(1, min(100, limit))

        for lookup_artist in self._build_artist_lookup_candidates(artist):
            params = self._build_basic_params("artist.getTopTags", artist=lookup_artist, limit=limit)
            try:
                res = self._request(params, timeout=(5, 10))
                res.raise_for_status()
                data = res.json()

                if "error" in data:
                    logger.debug("Last.fm error for '%s' tags: %s", lookup_artist, data.get("message", "unknown"))
                    continue

                tag_list = data.get("toptags", {}).get("tag", [])
                if isinstance(tag_list, dict):
                    tag_list = [tag_list]

                result = []
                for tag_obj in tag_list:
                    if isinstance(tag_obj, dict):
                        try:
                            count = int(tag_obj.get("count", 0))
                        except Exception:
                            count = 0
                        result.append({
                            "name": tag_obj.get("name", ""),
                            "count": count,
                        })

                if result:
                    return result

            except Exception as e:
                logger.debug("Failed to fetch artist top tags from Last.fm for '%s': %s", lookup_artist, e)

        return []

    def get_album_top_tags(self, artist: str, album: str, limit: int = 10) -> list:
        """Fetch top tags for an album from Last.fm."""
        if not self.api_key:
            logger.debug("Last.fm API key missing. Skipping album tags lookup.")
            return []

        limit = max(1, min(100, limit))

        for lookup_artist in self._album_lookup_candidates(artist):
            params = self._build_basic_params("album.getTopTags", artist=lookup_artist, album=album, limit=limit)
            try:
                res = self._request(params, timeout=(5, 10))
                res.raise_for_status()
                data = res.json()

                if "error" in data:
                    logger.debug(
                        "Last.fm error for album tags '%s' by '%s': %s",
                        album, lookup_artist, data.get("message", "unknown")
                    )
                    continue

                tag_list = data.get("toptags", {}).get("tag", [])
                if isinstance(tag_list, dict):
                    tag_list = [tag_list]

                result = []
                for tag_obj in tag_list:
                    if isinstance(tag_obj, dict):
                        try:
                            count = int(tag_obj.get("count", 0))
                        except Exception:
                            count = 0
                        result.append({
                            "name": tag_obj.get("name", ""),
                            "count": count,
                        })

                if result:
                    return result

            except Exception as e:
                logger.debug("Failed to fetch album top tags from Last.fm for '%s' by '%s': %s", album, lookup_artist, e)

        return []

    # -------------------------------------------------------------------------
    # Recommendations
    # -------------------------------------------------------------------------

    def get_recommendations(self) -> dict:
        """
        Fetch personalized recommendations from Last.fm for the current user.

        Uses cached results when DB filtering is not active.
        """
        if not self.api_key:
            logger.warning("Last.fm API key missing. Skipping recommendations.")
            return {"artists": [], "albums": [], "tracks": []}

        use_cache = not self.db_connection
        cache_key = f"recommendations_{self.username or 'global'}" if use_cache else None

        if use_cache and cache_key:
            cached_result = self.cache.get(cache_key)
            if cached_result:
                logger.info("Using cached recommendations for %s", self.username or "global")
                return cached_result

        try:
            recommendations = {
                "artists": self._get_recommended_artists(),
                "albums": self._get_recommended_albums(),
                "tracks": self._get_recommended_tracks(),
            }

            if use_cache and cache_key:
                self.cache.set(cache_key, recommendations)

            if not any([recommendations["artists"], recommendations["albums"], recommendations["tracks"]]):
                logger.warning(
                    "Last.fm recommendations returned empty for %s",
                    self.username or "global",
                )

            return recommendations

        except (ConnectionError, ConnectionResetError) as e:
            logger.error("Connection error fetching Last.fm recommendations for %s: %s", self.username or "global", e)
            return {"artists": [], "albums": [], "tracks": []}
        except Timeout as e:
            logger.error("Timeout fetching Last.fm recommendations for %s: %s", self.username or "global", e)
            return {"artists": [], "albums": [], "tracks": []}
        except HTTPError as e:
            status_code = e.response.status_code if hasattr(e.response, "status_code") else "unknown"
            logger.error(
                "HTTP error %s fetching Last.fm recommendations for %s: %s",
                status_code,
                self.username or "global",
                e,
            )
            return {"artists": [], "albums": [], "tracks": []}
        except Exception as e:
            logger.error("Failed to fetch Last.fm recommendations for %s: %s", self.username or "global", e, exc_info=True)
            return {"artists": [], "albums": [], "tracks": []}

    def _get_recommended_artists(self) -> list:
        recommended_artists = {}

        try:
            if self.username:
                params = self._build_basic_params(
                    "user.getTopArtists",
                    user=self.username,
                    limit=20,
                    period="6month",
                )
            else:
                params = self._build_basic_params("chart.getTopArtists", limit=20)

            def fetch():
                return self._request(params, timeout=(5, 10))

            res = retry_with_backoff(
                fetch,
                max_retries=LASTFM_CONFIG["MAX_RETRIES"],
                backoff_factor=LASTFM_CONFIG["RETRY_BACKOFF"],
                rate_limit_delay=LASTFM_CONFIG["RATE_LIMIT_DELAY"],
            )
            res.raise_for_status()

            if self.username:
                artists = res.json().get("topartists", {}).get("artist", [])
            else:
                artists = res.json().get("artists", {}).get("artist", [])

            for artist in artists:
                name = artist.get("name", "")
                if not name or name in recommended_artists:
                    continue

                image_url = ""
                if isinstance(artist.get("image"), list):
                    for img in reversed(artist["image"]):
                        if img.get("#text"):
                            image_url = img.get("#text", "")
                            break

                recommended_artists[name] = {
                    "name": name,
                    "listeners": artist.get("listeners", 0),
                    "match": 1.0,
                    "playcount": artist.get("playcount", 0),
                    "image": image_url,
                    "url": artist.get("url", ""),
                }

            return list(recommended_artists.values())[:20]

        except Exception as e:
            logger.error("Failed to fetch Last.fm recommended artists: %s", e)
            return []

    def _get_recommended_albums(self) -> list:
        recommended_albums = {}

        try:
            if self.username:
                params = self._build_basic_params(
                    "user.getTopAlbums",
                    user=self.username,
                    limit=12,
                    period="6month",
                )
            else:
                params = self._build_basic_params("chart.getTopArtists", limit=12)

            def fetch():
                return self._request(params, timeout=(5, 10))

            res = retry_with_backoff(
                fetch,
                max_retries=LASTFM_CONFIG["MAX_RETRIES"],
                backoff_factor=LASTFM_CONFIG["RETRY_BACKOFF"],
                rate_limit_delay=LASTFM_CONFIG["RATE_LIMIT_DELAY"],
            )
            res.raise_for_status()

            albums = []

            if self.username:
                albums = res.json().get("topalbums", {}).get("album", [])
            else:
                # No native recommended albums endpoint here; fallback remains minimal.
                return []

            for album in albums:
                album_name = album.get("name", "")
                artist_name = self._extract_artist_name(album.get("artist", {}))

                if not album_name or not artist_name:
                    continue

                album_key = (artist_name.lower(), album_name.lower())
                if album_key in recommended_albums:
                    continue

                image_url = ""
                if isinstance(album.get("image"), list):
                    for img in reversed(album["image"]):
                        if img.get("#text"):
                            image_url = img.get("#text", "")
                            break

                recommended_albums[album_key] = {
                    "name": album_name,
                    "artist": artist_name,
                    "playcount": album.get("playcount", 0),
                    "image": image_url,
                    "url": album.get("url", ""),
                    "similarity": 1.0,
                }

            return list(recommended_albums.values())[:12]

        except Exception as e:
            logger.error("Failed to fetch Last.fm recommended albums: %s", e)
            return []

    def _get_recommended_tracks(self) -> list:
        recommended_tracks = {}

        try:
            if self.username:
                params = self._build_basic_params(
                    "user.getTopTracks",
                    user=self.username,
                    limit=20,
                    period="6month",
                )
            else:
                params = self._build_basic_params("chart.getTopTracks", limit=20)

            def fetch():
                return self._request(params, timeout=(5, 10))

            res = retry_with_backoff(
                fetch,
                max_retries=LASTFM_CONFIG["MAX_RETRIES"],
                backoff_factor=LASTFM_CONFIG["RETRY_BACKOFF"],
                rate_limit_delay=LASTFM_CONFIG["RATE_LIMIT_DELAY"],
            )
            res.raise_for_status()

            tracks = res.json().get("toptracks", {}).get("track", [])

            for track in tracks:
                track_name = track.get("name", "")
                artist_name = self._extract_artist_name(track.get("artist"))

                if not track_name or not artist_name:
                    continue

                track_key = (artist_name.lower(), track_name.lower())
                if track_key in recommended_tracks:
                    continue

                image_url = ""
                if isinstance(track.get("image"), list):
                    for img in reversed(track["image"]):
                        if img.get("#text"):
                            image_url = img.get("#text", "")
                            break

                recommended_tracks[track_key] = {
                    "name": track_name,
                    "artist": artist_name,
                    "playcount": track.get("playcount", 0),
                    "image": image_url,
                    "url": track.get("url", ""),
                    "similarity": 1.0,
                }

            return list(recommended_tracks.values())[:20]

        except Exception as e:
            logger.error("Failed to fetch Last.fm recommended tracks: %s", e)
            return []


# Backward-compatible module functions
_lastfm_client = None


def _get_lastfm_client(api_key: str):
    """Get or create singleton Last.fm client."""
    global _lastfm_client
    if _lastfm_client is None:
        _lastfm_client = LastFmClient(api_key)
    return _lastfm_client


def get_lastfm_track_info(artist: str, title: str, api_key: str = "") -> dict:
    """Backward-compatible wrapper."""
    client = _get_lastfm_client(api_key)
    return client.get_track_info(artist, title)


def get_lastfm_recommendations(api_key: str, username: str | None = None, db_connection=None) -> dict:
    """Fetch Last.fm recommendations."""
    client = LastFmClient(api_key, username=username, db_connection=db_connection)
    return client.get_recommendations()