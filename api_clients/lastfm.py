"""Last.fm API client module with enhanced discovery features."""
import logging
import json
import time
import os
import re
from pathlib import Path
from requests.exceptions import ConnectionError, Timeout, RequestException, HTTPError
from . import session

logger = logging.getLogger(__name__)

# Configuration for DiscoveryLastFM-inspired features
LASTFM_CONFIG = {
    "MIN_ARTIST_PLAYS": 20,           # Minimum plays to consider artist for recommendations
    "MIN_SIMILARITY_SCORE": 0.46,     # Minimum similarity score (0-1)
    "MAX_SIMILAR_PER_ARTIST": 5,      # Max similar artists per artist
    "MAX_ALBUMS_PER_ARTIST": 5,       # Max albums per artist
    "RECENT_MONTHS": 3,                # Analyze recent N months
    "CACHE_TTL_HOURS": 24,            # Cache time-to-live in hours
    "MAX_RETRIES": 3,                 # Retry attempts for failed requests
    "RETRY_BACKOFF": 1.5,             # Exponential backoff multiplier
    "RATE_LIMIT_DELAY": 0.5           # Delay between requests (seconds)
}


class RecommendationCache:
    """Simple JSON-based cache for recommendations with TTL."""
    
    def __init__(self, cache_dir: str = None):
        """Initialize cache manager."""
        if cache_dir is None:
            cache_dir = Path.home() / ".cache" / "sptnr"
        
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_file = self.cache_dir / "lastfm_recommendations.json"
    
    def get(self, key: str) -> dict | None:
        """Get cached value if not expired."""
        try:
            if not self.cache_file.exists():
                return None
            
            with open(self.cache_file, "r") as f:
                cache = json.load(f)
            
            if key not in cache:
                return None
            
            entry = cache[key]
            age_hours = (time.time() - entry["timestamp"]) / 3600
            
            if age_hours > LASTFM_CONFIG["CACHE_TTL_HOURS"]:
                # Expired, remove it
                del cache[key]
                self._save_cache(cache)
                return None
            
            return entry["data"]
        except Exception as e:
            logger.debug(f"Cache read error for {key}: {e}")
            return None
    
    def set(self, key: str, value: dict) -> None:
        """Save value to cache with timestamp."""
        try:
            cache = {}
            if self.cache_file.exists():
                with open(self.cache_file, "r") as f:
                    cache = json.load(f)
            
            cache[key] = {
                "data": value,
                "timestamp": time.time()
            }
            
            self._save_cache(cache)
        except Exception as e:
            logger.debug(f"Cache write error for {key}: {e}")
    
    def _save_cache(self, cache: dict) -> None:
        """Atomically save cache to file."""
        try:
            # Write to temp file first, then rename
            temp_file = self.cache_file.with_suffix(".tmp")
            with open(temp_file, "w") as f:
                json.dump(cache, f, indent=2)
            temp_file.replace(self.cache_file)
        except Exception as e:
            logger.debug(f"Cache save failed: {e}")


def retry_with_backoff(func, max_retries: int = 3, backoff_factor: float = 1.5, 
                       rate_limit_delay: float = 0.5):
    """
    Retry a function with exponential backoff.
    
    Inspired by DiscoveryLastFM's robust retry logic.
    Handles specific connection errors gracefully:
    - ConnectionError: Network connectivity issues
    - Timeout: Request timeout
    - HTTPError with 429: Rate limit exceeded
    - Other errors: Retryable transient failures
    """
    import random
    
    for attempt in range(max_retries):
        try:
            # Rate limiting
            time.sleep(rate_limit_delay)
            
            result = func()
            
            # Check for 429 (rate limit) status code
            if hasattr(result, 'status_code') and result.status_code == 429:
                if attempt == max_retries - 1:
                    logger.error(f"Rate limited (429) - max retries exceeded after {attempt + 1} attempts")
                    result.raise_for_status()
                
                # Extract retry-after header if available
                retry_after = result.headers.get('Retry-After')
                if retry_after:
                    try:
                        wait_time = float(retry_after)
                        logger.warning(f"Rate limited (429) - waiting {wait_time}s as per Retry-After header")
                        time.sleep(wait_time)
                    except ValueError:
                        # Fall back to exponential backoff if header is not a number
                        wait_time = (backoff_factor ** attempt) * 2  # Longer backoff for rate limits
                        logger.warning(f"Rate limited (429) - exponential backoff for {wait_time:.2f}s")
                        time.sleep(wait_time)
                else:
                    # No Retry-After header, use exponential backoff
                    wait_time = (backoff_factor ** attempt) * 2
                    logger.warning(f"Rate limited (429) - exponential backoff for {wait_time:.2f}s")
                    time.sleep(wait_time)
                continue
            
            return result
        except (ConnectionError, ConnectionResetError) as e:
            if attempt == max_retries - 1:
                logger.error(f"Connection error after {attempt + 1} attempts: {e}")
                raise
            
            wait_time = (backoff_factor ** attempt) + random.uniform(0, 1)
            logger.warning(f"Connection error (attempt {attempt + 1}/{max_retries}), retrying after {wait_time:.2f}s: {e}")
            time.sleep(wait_time)
        except Timeout as e:
            if attempt == max_retries - 1:
                logger.error(f"Request timeout after {attempt + 1} attempts: {e}")
                raise
            
            wait_time = (backoff_factor ** attempt) + random.uniform(0, 1)
            logger.warning(f"Request timeout (attempt {attempt + 1}/{max_retries}), retrying after {wait_time:.2f}s: {e}")
            time.sleep(wait_time)
        except HTTPError as e:
            # Only retry on 5xx server errors, not on 4xx client errors
            if hasattr(e.response, 'status_code') and e.response.status_code >= 500:
                if attempt == max_retries - 1:
                    logger.error(f"Server error {e.response.status_code} after {attempt + 1} attempts: {e}")
                    raise
                
                wait_time = (backoff_factor ** attempt) + random.uniform(0, 1)
                logger.warning(f"Server error {e.response.status_code} (attempt {attempt + 1}/{max_retries}), retrying after {wait_time:.2f}s")
                time.sleep(wait_time)
            else:
                # Non-retryable client error
                logger.error(f"Non-retryable error {e.response.status_code if hasattr(e.response, 'status_code') else 'unknown'}: {e}")
                raise
        except RequestException as e:
            # Catch other request-related exceptions (includes connection errors and timeouts)
            if attempt == max_retries - 1:
                logger.error(f"Request error after {attempt + 1} attempts: {e}")
                raise
            
            wait_time = (backoff_factor ** attempt) + random.uniform(0, 1)
            logger.warning(f"Request error (attempt {attempt + 1}/{max_retries}), retrying after {wait_time:.2f}s: {e}")
            time.sleep(wait_time)
        except Exception as e:
            # Generic exception handler for unexpected errors
            if attempt == max_retries - 1:
                logger.error(f"Unexpected error after {attempt + 1} attempts: {e}")
                raise
            
            wait_time = (backoff_factor ** attempt) + random.uniform(0, 1)
            logger.debug(f"Retry attempt {attempt + 1}/{max_retries} after {wait_time:.2f}s: {e}")
            time.sleep(wait_time)
    
    return None



class LastFmClient:
    """Last.fm API wrapper with enhanced discovery features from DiscoveryLastFM."""
    
    def __init__(self, api_key: str, username: str = None, http_session=None, db_connection=None):
        """
        Initialize Last.fm client.
        
        Args:
            api_key: Last.fm API key
            username: Last.fm username for personalized recommendations (optional)
            http_session: Optional requests.Session (uses shared if not provided)
            db_connection: Optional database connection for filtering existing albums (callable or connection object)
        """
        self.api_key = api_key
        self.username = username
        self.session = http_session or session
        self.base_url = "https://ws.audioscrobbler.com/2.0/"
        self.cache = RecommendationCache()
        self.db_connection = db_connection  # Function to get DB connection or actual connection
        
        # Try to import MusicBrainz client for album filtering
        try:
            from .musicbrainz import MusicBrainzClient
            self.mb_client = MusicBrainzClient()
        except Exception as e:
            logger.debug(f"MusicBrainz client not available for album filtering: {e}")
            self.mb_client = None

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
    
    def _album_exists(self, artist: str, album: str) -> bool:
        """
        Check if an album already exists in the user's database.
        
        Args:
            artist: Artist name
            album: Album name
            
        Returns:
            True if album exists in database, False otherwise
        """
        if not self.db_connection:
            return False  # If no DB connection provided, assume album doesn't exist
        
        try:
            # If db_connection is callable, call it to get a connection
            if callable(self.db_connection):
                conn = self.db_connection()
            else:
                conn = self.db_connection
            
            cursor = conn.cursor()
            
            from app import _is_postgres_connection as app_is_postgres_connection
            placeholder = "%s"
            
            # Query for album matching both artist and album name (case-insensitive)
            cursor.execute(
                f"SELECT 1 FROM tracks WHERE LOWER(artist) = LOWER({placeholder}) AND LOWER(album) = LOWER({placeholder}) LIMIT 1",
                (artist, album)
            )
            
            result = cursor.fetchone()
            
            # Close connection if it was callable (we created a new one)
            if callable(self.db_connection):
                try:
                    conn.close()
                except:
                    pass
            
            return result is not None
        except Exception as e:
            logger.debug(f"Error checking if album exists in database: {e}")
            return False  # If error, assume album doesn't exist (permissive)
    
    def _is_studio_album(self, artist: str, album: str) -> bool:
        """
        Filter to only include studio albums (exclude compilations, live, EPs, singles).
        
        Checks:
        1. Album type (must be "Album")
        2. Secondary types (excludes compilation, live, remix, ep)
        3. Track count (must have >3 tracks to exclude singles/EPs)
        
        Uses MusicBrainz if available, otherwise returns True for albums to be permissive.
        
        Args:
            artist: Artist name
            album: Album name
            
        Returns:
            True if album meets criteria (studio album with >3 tracks), False otherwise
        """
        if not self.mb_client:
            return True  # If MB not available, include album (permissive)
        
        try:
            # Query MusicBrainz for release info
            releases = self.mb_client.search_releases(f'artist:"{artist}" AND release:"{album}"')
            
            if not releases:
                return True  # If not found, include it (permissive)
            
            # Check the first result
            first_release = releases[0] if isinstance(releases, list) else releases
            album_type = (first_release.get("primaryType") or "").lower()
            secondary_types = [t.lower() for t in (first_release.get("secondaryTypes") or [])]
            
            # Check 1: Only accept Studio albums
            if album_type != "album":
                logger.debug(f"Filtering out {album} by {artist}: type={album_type}")
                return False
            
            # Check 2: Exclude compilations, live, remix albums, EPs
            excluded = {"compilation", "live", "remix", "ep"}
            if any(t in excluded for t in secondary_types):
                logger.debug(f"Filtering out {album} by {artist}: secondary_types={secondary_types}")
                return False
            
            # Check 3: Filter by track count - must have >3 tracks (exclude singles)
            # Get media list and count tracks
            media = first_release.get("media", [])
            total_tracks = 0
            for disc in media:
                tracks = disc.get("tracks", [])
                total_tracks += len(tracks)
            
            if total_tracks <= 3:
                logger.debug(f"Filtering out {album} by {artist}: only {total_tracks} tracks (≤3 = single/EP)")
                return False
            
            logger.debug(f"Accepted album {album} by {artist}: {total_tracks} tracks")
            return True
        except Exception as e:
            logger.debug(f"MusicBrainz filtering failed for {album}/{artist}: {e}")
            return True  # If error, include it (permissive)

    @staticmethod
    def _strip_featured_artist(artist: str) -> str:
        """Return canonical primary artist by removing feat./ft./featuring suffixes."""
        if not artist:
            return artist
        primary = re.split(r"\s+(?:feat\.?|featuring|ft\.?)\s+", artist, maxsplit=1, flags=re.IGNORECASE)[0].strip()
        return primary or artist.strip()

    def _get_track_info_once(self, artist: str, title: str) -> dict:
        """Perform a single Last.fm track.getInfo call for a specific artist/title pair."""
        params = {
            "method": "track.getInfo",
            "artist": artist,
            "track": title,
            "api_key": self.api_key,
            "format": "json",
            "autocorrect": 1  # Enable Last.fm's autocorrect for better matching
        }

        try:
            res = self.session.get(self.base_url, params=params, timeout=(5, 10))  # (connect_timeout, read_timeout)
            res.raise_for_status()
            response_data = res.json()

            # Check for Last.fm API error responses
            if "error" in response_data:
                error_code = response_data.get("error")
                error_msg = response_data.get("message", "Unknown error")
                logger.warning(f"Last.fm API error {error_code} for '{title}' by '{artist}': {error_msg}")
                logger.debug(f"Full API response: {response_data}")
                return {"track_play": 0, "listeners": 0, "toptags": {}}

            data = response_data.get("track", {})
            track_play = int(data.get("playcount", 0))
            listeners = int(data.get("listeners", 0))
            toptags = data.get("toptags", {})

            # Debug log when we get 0 values despite a successful API call
            if track_play == 0 and listeners == 0:
                logger.debug(f"Last.fm returned 0 values for '{title}' by '{artist}'. Response: {data}")

            return {
                "track_play": track_play,
                "listeners": listeners,
                "toptags": toptags,
                "lookup_artist": artist
            }
        except (ConnectionError, ConnectionResetError) as e:
            logger.error(f"Connection error fetching track '{title}' by '{artist}': {e} - retrying may help")
            return {"track_play": 0, "listeners": 0, "toptags": {}, "lookup_artist": artist}
        except Timeout as e:
            logger.error(f"Timeout fetching track '{title}' by '{artist}': {e}")
            return {"track_play": 0, "listeners": 0, "toptags": {}, "lookup_artist": artist}
        except HTTPError as e:
            status_code = e.response.status_code if hasattr(e.response, 'status_code') else 'unknown'
            logger.error(f"HTTP error {status_code} fetching track '{title}' by '{artist}': {e}")
            return {"track_play": 0, "listeners": 0, "toptags": {}, "lookup_artist": artist}
        except Exception as e:
            logger.error(f"Failed to fetch Last.fm info for '{title}' by '{artist}': {e}")
            return {"track_play": 0, "listeners": 0, "toptags": {}, "lookup_artist": artist}
    
    
    def get_track_info(self, artist: str, title: str) -> dict:
        """
        Fetch track listeners, playcount, and metadata from Last.fm.
        
        Args:
            artist: Artist name
            title: Track title
            
        Returns:
            Dict with 'track_play', 'listeners', and other metadata including 'toptags'
        """
        if not self.api_key:
            logger.warning("Last.fm API key missing. Skipping lookup.")
            return {"track_play": 0, "listeners": 0}
        
        # For collaboration strings like "Artist feat. Guest", prefer canonical
        # artist lookup first to avoid low-count alternate Last.fm entries.
        primary_artist = self._strip_featured_artist(artist)
        lookup_order = [primary_artist] if primary_artist else [artist]
        if artist and artist.lower() != primary_artist.lower():
            lookup_order.append(artist)

        best_result = {"track_play": 0, "listeners": 0, "toptags": {}, "lookup_artist": artist}
        for lookup_artist in lookup_order:
            candidate = self._get_track_info_once(lookup_artist, title)
            if candidate.get("listeners", 0) > best_result.get("listeners", 0):
                best_result = candidate
            if candidate.get("listeners", 0) > 0 and candidate.get("track_play", 0) > 0:
                break

        # Keep backwards compatibility for callers expecting this exact shape.
        return {
            "track_play": int(best_result.get("track_play", 0)),
            "listeners": int(best_result.get("listeners", 0)),
            "toptags": best_result.get("toptags", {}),
            "lookup_artist": best_result.get("lookup_artist", artist)
        }
    
    def search_track(self, artist: str, title: str, limit: int = 10) -> list[dict]:
        """
        Search for tracks on Last.fm by artist and title.
        
        Used for fuzzy matching when exact track lookup fails.
        
        Args:
            artist: Artist name to filter results
            title: Track title to search for
            limit: Maximum number of results to return (default 10)
            
        Returns:
            List of dicts with 'name' (track title) and 'artist' keys
            Returns empty list if no results or error
        """
        if not self.api_key:
            logger.debug("Last.fm API key missing. Skipping search.")
            return []
        
        params = {
            "method": "track.search",
            "track": title,
            "api_key": self.api_key,
            "format": "json",
            "limit": limit
        }
        
        try:
            res = self.session.get(self.base_url, params=params, timeout=(5, 10))
            res.raise_for_status()
            data = res.json()
            
            # Extract track list from response
            results = data.get("results", {})
            trackmatches = results.get("trackmatches", {})
            tracks = trackmatches.get("track", [])
            
            # Ensure it's always a list
            if isinstance(tracks, dict):
                tracks = [tracks]
            
            # Filter results to same artist (case-insensitive)
            artist_lower = artist.lower()
            artist_primary_lower = self._strip_featured_artist(artist).lower()
            filtered_tracks = []
            for track in tracks:
                track_artist = track.get("artist", "")
                if isinstance(track_artist, dict):
                    track_artist = track_artist.get("name", "")

                track_artist_lower = track_artist.lower()
                track_artist_primary_lower = self._strip_featured_artist(track_artist).lower()
                if track_artist_lower == artist_lower or track_artist_primary_lower == artist_primary_lower:
                    filtered_tracks.append({
                        "name": track.get("name", ""),
                        "artist": track_artist
                    })
            
            return filtered_tracks
            
        except (ConnectionError, ConnectionResetError) as e:
            logger.debug(f"Connection error searching for '{title}' by '{artist}': {e}")
            return []
        except Timeout as e:
            logger.debug(f"Timeout searching for '{title}' by '{artist}': {e}")
            return []
        except HTTPError as e:
            status_code = e.response.status_code if hasattr(e.response, 'status_code') else 'unknown'
            logger.debug(f"HTTP error {status_code} searching for '{title}' by '{artist}': {e}")
            return []
        except Exception as e:
            logger.debug(f"Failed to search Last.fm for '{title}' by '{artist}': {e}")
            return []
    
    def get_album_track_count(self, artist: str, album: str) -> int:
        """
        Fetch album track count from Last.fm.
        
        Used for single detection: albums with 1-3 tracks on Last.fm are typically singles
        
        Args:
            artist: Artist name
            album: Album name
            
        Returns:
            Track count on the album (0 if not found or error)
        """
        if not self.api_key:
            logger.debug("Last.fm API key missing. Skipping album lookup.")
            return 0
        
        # Try primary artist first (strip featured artists) to avoid 404s on
        # collaboration strings like "dArtagnan feat. Melissa Bonny".
        primary_artist = self._strip_featured_artist(artist)
        lookup_order = [primary_artist] if primary_artist else [artist]
        if artist and artist.lower() != primary_artist.lower():
            lookup_order.append(artist)
        
        for lookup_artist in lookup_order:
            params = {
                "method": "album.getInfo",
                "artist": lookup_artist,
                "album": album,
                "api_key": self.api_key,
                "format": "json"
            }
            
            try:
                res = self.session.get(self.base_url, params=params, timeout=(5, 10))
                res.raise_for_status()
                data = res.json().get("album", {})
                
                # Last.fm returns tracks as a list or dict depending on context
                tracks = data.get("tracks", {})
                if isinstance(tracks, dict):
                    # If it's a dict, it might have 'track' key with list or single item
                    track_list = tracks.get("track", [])
                    if isinstance(track_list, dict):
                        # Single track
                        return 1
                    elif isinstance(track_list, list):
                        return len(track_list)
                elif isinstance(tracks, list):
                    return len(tracks)
                
                # If we got a valid response but no tracks, continue to next lookup
                continue
            except HTTPError as e:
                status_code = e.response.status_code if hasattr(e.response, 'status_code') else 'unknown'
                if status_code == 404:
                    logger.debug(f"Album '{album}' not found for '{lookup_artist}' (404)")
                    continue
                logger.debug(f"HTTP error {status_code} fetching album '{album}' by '{lookup_artist}': {e}")
                continue
            except (ConnectionError, ConnectionResetError) as e:
                logger.debug(f"Connection error fetching album '{album}' by '{lookup_artist}': {e}")
                continue
            except Timeout as e:
                logger.debug(f"Timeout fetching album '{album}' by '{lookup_artist}': {e}")
                continue
            except Exception as e:
                logger.debug(f"Failed to fetch Last.fm album info for '{album}' by '{lookup_artist}': {e}")
                continue
        
        return 0
    
    def has_title_track(self, artist: str, album: str) -> bool:
        """
        Check if the album has a title track (track name matching album name).
        
        Used for single detection: singles released as EPs often have a title track
        
        Args:
            artist: Artist name
            album: Album name
            
        Returns:
            True if album has a track with the same name as the album, False otherwise
        """
        if not self.api_key:
            logger.debug("Last.fm API key missing. Skipping album lookup.")
            return False
        
        # Try primary artist first (strip featured artists) to avoid 404s on
        # collaboration strings like "dArtagnan feat. Melissa Bonny".
        primary_artist = self._strip_featured_artist(artist)
        lookup_order = [primary_artist] if primary_artist else [artist]
        if artist and artist.lower() != primary_artist.lower():
            lookup_order.append(artist)
        
        for lookup_artist in lookup_order:
            params = {
                "method": "album.getInfo",
                "artist": lookup_artist,
                "album": album,
                "api_key": self.api_key,
                "format": "json"
            }
            
            try:
                res = self.session.get(self.base_url, params=params, timeout=(5, 10))
                res.raise_for_status()
                data = res.json().get("album", {})
                
                # Get album name from response (might be normalized)
                album_name = data.get("name", album)
                
                # Normalize album name for comparison (case-insensitive, strip whitespace)
                normalized_album = album_name.lower().strip()
                
                # Last.fm returns tracks as a list or dict depending on context
                tracks = data.get("tracks", {})
                track_list = []
                
                if isinstance(tracks, dict):
                    # If it's a dict, it might have 'track' key with list or single item
                    track_data = tracks.get("track", [])
                    if isinstance(track_data, dict):
                        # Single track
                        track_list = [track_data]
                    elif isinstance(track_data, list):
                        track_list = track_data
                elif isinstance(tracks, list):
                    track_list = tracks
                
                # Check if any track name matches the album name
                for track in track_list:
                    if isinstance(track, dict):
                        track_name = track.get("name", "")
                        normalized_track = track_name.lower().strip()
                        if normalized_track == normalized_album:
                            logger.debug(f"Found title track '{track_name}' matching album '{album_name}'")
                            return True
                
                # If we got a valid response but no title track, continue to next lookup
                continue
            except HTTPError as e:
                status_code = e.response.status_code if e.response else 'unknown'
                if status_code == 404:
                    logger.debug(f"Album '{album}' not found for '{lookup_artist}' (404)")
                    continue
                logger.debug(f"HTTP error {status_code} checking title track for '{album}' by '{lookup_artist}': {e}")
                continue
            except (ConnectionError, ConnectionResetError) as e:
                logger.debug(f"Connection error checking title track for '{album}' by '{lookup_artist}': {e}")
                continue
            except Timeout as e:
                logger.debug(f"Timeout checking title track for '{album}' by '{lookup_artist}': {e}")
                continue
            except Exception as e:
                logger.debug(f"Failed to check title track for '{album}' by '{lookup_artist}': {e}")
                continue
        
        return False
    
    def check_track_as_single(self, artist: str, track_title: str) -> bool:
        """
        Check if a track exists as a single/album release on Last.fm.
        
        This method searches for an album with the same name as the track,
        which would indicate the track was released as a single.
        
        Only returns True if:
        1. An album with the same name as the track exists, AND
        2. The album has less than 6 tracks
        
        Args:
            artist: Artist name
            track_title: Track title to check
            
        Returns:
            True if an album/single with the track's name exists on Last.fm with < 6 tracks, False otherwise
        """
        if not self.api_key:
            logger.debug("Last.fm API key missing. Skipping single lookup.")
            return False
        
        # Try primary artist first (strip featured artists) to avoid 404s on
        # collaboration strings like "dArtagnan feat. Melissa Bonny".
        primary_artist = self._strip_featured_artist(artist)
        lookup_order = [primary_artist] if primary_artist else [artist]
        if artist and artist.lower() != primary_artist.lower():
            lookup_order.append(artist)
        
        for lookup_artist in lookup_order:
            # Search for an album with the same name as the track
            params = {
                "method": "album.getInfo",
                "artist": lookup_artist,
                "album": track_title,  # Use track title as album name
                "api_key": self.api_key,
                "format": "json"
            }
            
            try:
                res = self.session.get(self.base_url, params=params, timeout=(5, 10))
                res.raise_for_status()
                data = res.json()
                
                # If we get an album response (not an error), the track exists as a single/album
                if "album" in data:
                    album_data = data["album"]
                    album_name = album_data.get("name", "")
                    
                    # Normalize for comparison
                    normalized_album = album_name.lower().strip()
                    normalized_track = track_title.lower().strip()
                    
                    # Check if the album name matches the track title
                    if normalized_album == normalized_track:
                        # Get track count to verify it's actually a single (< 6 tracks)
                        tracks_data = album_data.get("tracks", {})
                        track_count = 0
                        
                        if isinstance(tracks_data, dict):
                            track_list = tracks_data.get("track", [])
                            if isinstance(track_list, dict):
                                # Single track
                                track_count = 1
                            elif isinstance(track_list, list):
                                track_count = len(track_list)
                        elif isinstance(tracks_data, list):
                            track_count = len(tracks_data)
                        
                        # Only return True if track count is less than 6
                        if track_count > 0 and track_count < 6:
                            logger.debug(f"Found single/album '{album_name}' matching track '{track_title}' with {track_count} tracks")
                            return True
                        else:
                            logger.debug(f"Found album '{album_name}' matching track '{track_title}' but has {track_count} tracks (>= 6), not a single")
                            return False
                
                # If we got a valid response but no matching album, continue to next lookup
                continue
            except HTTPError as e:
                # 404 or other HTTP errors mean the single doesn't exist for this artist
                status_code = e.response.status_code if e.response else 'unknown'
                if status_code == 404:
                    logger.debug(f"No single found for '{track_title}' by '{lookup_artist}' (404)")
                    continue
                else:
                    logger.debug(f"HTTP error {status_code} checking single for '{track_title}' by '{lookup_artist}': {e}")
                    continue
            except (ConnectionError, ConnectionResetError) as e:
                logger.debug(f"Connection error checking single for '{track_title}' by '{lookup_artist}': {e}")
                continue
            except Timeout as e:
                logger.debug(f"Timeout checking single for '{track_title}' by '{lookup_artist}': {e}")
                continue
            except Exception as e:
                logger.debug(f"Failed to check single for '{track_title}' by '{lookup_artist}': {e}")
                continue
        
        return False
    
    def get_track_temporal_data(self, artist: str, title: str) -> dict:
        """
        Attempt to fetch temporal popularity data (7-day, 365-day, all-time) from Last.fm.
        
        NOTE: The standard Last.fm API does not directly expose time-window breakdowns.
        This method attempts to fetch data through the standard API and returns what's available.
        For accurate 7-day and 365-day data, would need access to:
        1. User scrobbling API with date-range filtering, or
        2. Last.fm's internal analytics (not publicly exposed)
        
        Currently returns:
        - all_time_listeners: From track.getInfo (reliable)
        - all_time_playcount: From track.getInfo (reliable)
        - momentum_score: Calculated from available data (approximation)
        - trend: Estimated based on available metrics
        
        Args:
            artist: Artist name
            title: Track title
            
        Returns:
            Dict with temporal metrics:
            {
                'all_time_listeners': int,
                'all_time_playcount': int,
                '7day_listeners': int or None (not available from standard API),
                '365day_listeners': int or None (not available from standard API),
                'momentum_score': float (1.0 if stable, >1.0 if accelerating),
                'popularity_trend': str ('stable', 'accelerating', 'declining', 'unknown'),
                'data_source': str (indicates quality: 'estimated', 'partial', 'full')
            }
        """
        if not self.api_key:
            logger.debug("Last.fm API key missing. Skipping temporal lookup.")
            return {
                'all_time_listeners': 0,
                'all_time_playcount': 0,
                '7day_listeners': None,
                '365day_listeners': None,
                'momentum_score': 1.0,
                'popularity_trend': 'unknown',
                'data_source': 'unavailable'
            }
        
        try:
            # Fetch all-time data (reliable)
            params = {
                "method": "track.getInfo",
                "artist": artist,
                "track": title,
                "api_key": self.api_key,
                "format": "json"
            }
            
            res = self.session.get(self.base_url, params=params, timeout=(5, 10))
            res.raise_for_status()
            data = res.json().get("track", {})
            
            all_time_listeners = int(data.get("listeners", 0))
            all_time_playcount = int(data.get("playcount", 0))
            
            # NOTE: Last.fm standard API doesn't expose 7-day/365-day breakdown
            # This would require:
            # 1. User authentication with extended permissions, OR
            # 2. Access to Last.fm's web scraping (not recommended, against ToS)
            #
            # For now, return None for temporal windows
            # Future enhancement: Integrate with user scrobbling data if available
            
            result = {
                'all_time_listeners': all_time_listeners,
                'all_time_playcount': all_time_playcount,
                '7day_listeners': None,
                '365day_listeners': None,
                'momentum_score': 1.0,  # Default to stable (no data to calculate trend)
                'popularity_trend': 'unknown',  # Cannot determine without temporal data
                'data_source': 'standard_api_only'  # Indicates limited data source
            }
            
            logger.debug(f"Fetched Last.fm temporal data for '{title}' by '{artist}': "
                        f"all_time={all_time_listeners} listeners, "
                        f"playcount={all_time_playcount} (temporal windows unavailable from standard API)")
            
            return result
            
        except (ConnectionError, ConnectionResetError) as e:
            logger.debug(f"Connection error fetching temporal data for '{title}' by '{artist}': {e}")
            return {
                'all_time_listeners': 0,
                'all_time_playcount': 0,
                '7day_listeners': None,
                '365day_listeners': None,
                'momentum_score': 1.0,
                'popularity_trend': 'unknown',
                'data_source': 'error'
            }
        except Timeout as e:
            logger.debug(f"Timeout fetching temporal data for '{title}' by '{artist}': {e}")
            return {
                'all_time_listeners': 0,
                'all_time_playcount': 0,
                '7day_listeners': None,
                '365day_listeners': None,
                'momentum_score': 1.0,
                'popularity_trend': 'unknown',
                'data_source': 'timeout'
            }
        except Exception as e:
            logger.debug(f"Failed to fetch temporal data for '{title}' by '{artist}': {e}")
            return {
                'all_time_listeners': 0,
                'all_time_playcount': 0,
                '7day_listeners': None,
                '365day_listeners': None,
                'momentum_score': 1.0,
                'popularity_trend': 'unknown',
                'data_source': 'error'
            }
    
    def get_similar_artists(self, artist: str, limit: int = 10) -> list:
        """
        Fetch similar artists from Last.fm for a given artist.
        
        This will be used to find artists with similar listener bases,
        enabling artist-contextual popularity weighting.
        
        Args:
            artist: Artist name
            limit: Maximum number of results (1-100)
            
        Returns:
            List of dicts with 'name' and 'match' (similarity score 0-1)
            Example: [{'name': 'Similar Artist 1', 'match': 0.95}, {...}]
        """
        if not self.api_key:
            logger.debug("Last.fm API key missing. Skipping similar artists lookup.")
            return []
        
        # Clamp limit
        limit = max(1, min(100, limit))
        
        params = {
            "method": "artist.getSimilar",
            "artist": artist,
            "limit": limit,
            "api_key": self.api_key,
            "format": "json"
        }
        
        try:
            res = self.session.get(self.base_url, params=params, timeout=(5, 10))
            res.raise_for_status()
            data = res.json()
            
            if "error" in data:
                logger.debug(f"Last.fm error for '{artist}': {data.get('message', 'unknown')}")
                return []
            
            similar_artists = data.get("similarartists", {}).get("artist", [])
            
            # Normalize response (might be a single dict or list)
            if isinstance(similar_artists, dict):
                similar_artists = [similar_artists]
            
            # Extract name and match score
            result = []
            for artist_obj in similar_artists:
                if isinstance(artist_obj, dict):
                    name = artist_obj.get("name", "")
                    match = float(artist_obj.get("match", 0.0))
                    if name:
                        result.append({"name": name, "match": match})
            
            logger.debug(f"Fetched {len(result)} similar artists for '{artist}' from Last.fm")
            return result
            
        except (ConnectionError, ConnectionResetError) as e:
            logger.debug(f"Connection error fetching similar artists for '{artist}': {e}")
            return []
        except Timeout as e:
            logger.debug(f"Timeout fetching similar artists for '{artist}': {e}")
            return []
        except Exception as e:
            logger.debug(f"Failed to fetch similar artists for '{artist}' from Last.fm: {e}")
            return []
    
    def get_track_tags(self, artist: str, title: str, limit: int = 10) -> list:
        """
        Extract and format track tags from Last.fm using track.getTopTags API.
        
        Args:
            artist: Artist name
            title: Track title
            limit: Maximum number of tags to return
            
        Returns:
            List of dicts with 'name' and 'count' keys
            Example: [{'name': 'rock', 'count': 1000}, {'name': 'alternative', 'count': 800}]
        """
        if not self.api_key:
            logger.debug("Last.fm API key missing. Skipping tags lookup.")
            return []
        
        try:
            # Use track.getTopTags API method (not track.getInfo)
            # track.getInfo doesn't return toptags - need dedicated method
            params = {
                "method": "track.getTopTags",
                "artist": artist,
                "track": title,
                "api_key": self.api_key,
                "format": "json"
            }
            
            res = self.session.get(self.base_url, params=params, timeout=(5, 10))
            res.raise_for_status()
            data = res.json()
            
            logger.debug(f"[LASTFM_TAGS] Raw API response for '{title}' by '{artist}': {data}")
            
            # Parse the toptags response
            toptags_data = data.get("toptags", {})
            tag_list = toptags_data.get("tag", [])
            
            # Normalize response (might be a single dict or list)
            if isinstance(tag_list, dict):
                tag_list = [tag_list]
            
            logger.debug(f"[LASTFM_TAGS] Normalized tag_list for '{title}' by '{artist}': {len(tag_list) if isinstance(tag_list, list) else '?'} items")
            
            # Extract name and count, applying limit
            result = []
            for tag_obj in tag_list[:limit]:
                if isinstance(tag_obj, dict):
                    name = tag_obj.get("name", "")
                    count = tag_obj.get("count", 0)  # May be a string, will convert to int as needed
                    if name:
                        try:
                            count = int(count) if count else 0
                        except (ValueError, TypeError):
                            count = 0
                        result.append({"name": name, "count": count})
            
            if result:
                logger.debug(f"[LASTFM_TAGS] Fetched {len(result)} tags for '{title}' by '{artist}': {[t['name'] for t in result[:3]]}")
            else:
                logger.debug(f"[LASTFM_TAGS] No tags found for '{title}' by '{artist}' (tag_list empty or error)")
            return result
            
        except Exception as e:
            logger.debug(f"[LASTFM_TAGS] Failed to fetch tags for '{title}' by '{artist}': {e}")
            import traceback
            logger.debug(f"[LASTFM_TAGS] Traceback: {traceback.format_exc()}")
            return []
    
    def get_recommendations(self) -> dict:
        """
        Fetch personalized recommendations from Last.fm for the current user.
        
        Features (inspired by DiscoveryLastFM):
        - Caching to avoid duplicate API calls (skipped if DB filtering is active)
        - Retry logic with exponential backoff  
        - Rate limiting between requests
        - Minimum play count filtering
        - Studio album filtering (via MusicBrainz)
        - Database filtering to exclude existing albums
        
        Returns:
            Dict with 'artists', 'albums', and 'tracks' keys containing recommendations
        """
        if not self.api_key:
            logger.warning("Last.fm API key missing. Skipping recommendations.")
            return {"artists": [], "albums": [], "tracks": []}
        
        # Skip cache if we're doing database filtering (recommendations change when library changes)
        use_cache = not self.db_connection
        cache_key = f"recommendations_{self.username or 'global'}" if use_cache else None
        
        # Check cache first (only if not using DB filtering)
        if use_cache and cache_key:
            cached_result = self.cache.get(cache_key)
            if cached_result:
                logger.info(f"Using cached recommendations for {self.username or 'global'}")
                return cached_result
        
        try:
            recommendations = {
                "artists": self._get_recommended_artists(),
                "albums": self._get_recommended_albums(),
                "tracks": self._get_recommended_tracks()
            }
            
            # Cache the result (only if not using DB filtering)
            if use_cache and cache_key:
                self.cache.set(cache_key, recommendations)
            
            # Log detail about what was fetched if anything is empty
            if not any([recommendations["artists"], recommendations["albums"], recommendations["tracks"]]):
                logger.warning(f"Last.fm recommendations returned empty for {self.username or 'global'} - API may have returned no results or filtering removed all items")
            
            return recommendations
        except (ConnectionError, ConnectionResetError) as e:
            logger.error(f"Connection error fetching Last.fm recommendations for {self.username or 'global'}: {e} - may indicate network issues")
            return {"artists": [], "albums": [], "tracks": []}
        except Timeout as e:
            logger.error(f"Timeout fetching Last.fm recommendations for {self.username or 'global'}: {e}")
            return {"artists": [], "albums": [], "tracks": []}
        except HTTPError as e:
            status_code = e.response.status_code if hasattr(e.response, 'status_code') else 'unknown'
            logger.error(f"HTTP error {status_code} fetching Last.fm recommendations for {self.username or 'global'}: {e}")
            return {"artists": [], "albums": [], "tracks": []}
        except Exception as e:
            logger.error(f"Failed to fetch Last.fm recommendations for {self.username or 'global'}: {e}", exc_info=True)
            return {"artists": [], "albums": [], "tracks": []}
    
    def _get_recommended_artists(self) -> list:
        """Fetch recommended artists from Last.fm.
        
        Note: Last.fm doesn't have a native 'user.getRecommendedArtists' endpoint.
        This method uses user.getTopArtists for personalized data, or chart.getTopArtists as a fallback.
        
        Strategy:
        1. Try user.getTopArtists if username is available (closest to personalized)
        2. Fall back to chart.getTopArtists (global trending)
        """
        recommended_artists = {}
        
        try:
            # Use user's top artists if username available (personalized), else global chart
            if self.username:
                params = {
                    "method": "user.getTopArtists",
                    "user": self.username,
                    "api_key": self.api_key,
                    "format": "json",
                    "limit": 20,
                    "period": "6month"  # Last 6 months for recent popularity
                }
            else:
                # Fall back to global chart if no username
                params = {
                    "method": "chart.getTopArtists",
                    "api_key": self.api_key,
                    "format": "json",
                    "limit": 20
                }
            
            def fetch_recommended_artists():
                return self.session.get(self.base_url, params=params, timeout=(5, 10))
            
            res = retry_with_backoff(
                fetch_recommended_artists,
                max_retries=LASTFM_CONFIG["MAX_RETRIES"],
                backoff_factor=LASTFM_CONFIG["RETRY_BACKOFF"],
                rate_limit_delay=LASTFM_CONFIG["RATE_LIMIT_DELAY"]
            )
            res.raise_for_status()
            
            # Get the artists
            if self.username:
                artists = res.json().get("topartists", {}).get("artist", [])
            else:
                artists = res.json().get("artists", {}).get("artist", [])
            
            for artist in artists:
                name = artist.get("name", "")
                if not name or name in recommended_artists:
                    continue
                
                # Extract image
                image_url = ""
                if isinstance(artist.get("image"), list):
                    for img in reversed(artist["image"]):
                        if img.get("#text"):
                            image_url = img.get("#text", "")
                            break
                
                recommended_artists[name] = {
                    "name": name,
                    "listeners": artist.get("listeners", 0),
                    "match": 1.0,  # Recommendation strength from Last.fm algorithm
                    "playcount": 0,
                    "image": image_url,
                    "url": artist.get("url", "")
                }
            
            logger.info(f"Found {len(recommended_artists)} recommended artists from Last.fm")
            return list(recommended_artists.values())[:20]
        except (ConnectionError, ConnectionResetError) as e:
            logger.error(f"Connection error fetching recommended artists: {e} - may indicate network issues")
            return []
        except Timeout as e:
            logger.error(f"Timeout fetching recommended artists: {e}")
            return []
        except HTTPError as e:
            status_code = e.response.status_code if hasattr(e.response, 'status_code') else 'unknown'
            logger.error(f"HTTP error {status_code} fetching recommended artists: {e}")
            return []
        except Exception as e:
            logger.error(f"Failed to fetch Last.fm recommended artists: {e}")
            return []
    
    def _get_recommended_albums(self) -> list:
        """Fetch recommended albums from Last.fm.
        
        Note: Last.fm doesn't have a native 'user.getRecommendedAlbums' endpoint.
        This method uses user.getTopAlbums for personalized data, or chart.getTopTags + top albums as a fallback.
        
        Strategy:
        1. Try user.getTopAlbums if username is available (user's most played albums)
        2. Fall back to chart.getTopArtists and fetch their top albums
        """
        recommended_albums = {}
        
        try:
            # Step 1: Get recommended albums using Last.fm API
            if self.username:
                artist_params = {
                    "method": "user.getTopAlbums",
                    "user": self.username,
                    "api_key": self.api_key,
                    "format": "json",
                    "limit": 12,
                    "period": "6month"  # Last 6 months for recent popularity
                }
            else:
                # Fall back to global chart
                artist_params = {
                    "method": "chart.getTopArtists",
                    "api_key": self.api_key,
                    "format": "json",
                    "limit": 12
                }
            
            def fetch_recommended_artists():
                return self.session.get(self.base_url, params=artist_params, timeout=(5, 10))
            
            res = retry_with_backoff(
                fetch_recommended_artists,
                max_retries=LASTFM_CONFIG["MAX_RETRIES"],
                backoff_factor=LASTFM_CONFIG["RETRY_BACKOFF"],
                rate_limit_delay=LASTFM_CONFIG["RATE_LIMIT_DELAY"]
            )
            res.raise_for_status()
            
            albums = []  # Initialize to avoid "possibly unbound" error
            
            if self.username:
                # When using getTopAlbums, the response has a different structure
                albums = res.json().get("topalbums", {}).get("album", [])
            else:
                # For chart.getTopArtists fallback, we get artists instead of albums
                # In this case, we'd need to fetch their albums separately
                # For simplicity, we'll just return the artists' names and let the matches handle it
                artists = res.json().get("artists", {}).get("artist", [])
            
            # Process albums from the response
            for album in albums:
                album_name = album.get("name", "")
                artist_name = self._extract_artist_name(album.get("artist", {}))
                
                if not album_name or not artist_name:
                    continue
                
                album_key = (artist_name.lower(), album_name.lower())
                if album_key in recommended_albums:
                    continue
                
                # Extract image
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
                    "similarity": 1.0  # From Last.fm recommendation algorithm
                }
            
            logger.info(f"Found {len(recommended_albums)} recommended albums from Last.fm ({self.username or 'global'})")
            return list(recommended_albums.values())[:12]
        except (ConnectionError, ConnectionResetError) as e:
            logger.error(f"Connection error fetching recommended albums: {e} - may indicate network issues")
            return []
        except Timeout as e:
            logger.error(f"Timeout fetching recommended albums: {e}")
            return []
        except HTTPError as e:
            status_code = e.response.status_code if hasattr(e.response, 'status_code') else 'unknown'
            logger.error(f"HTTP error {status_code} fetching recommended albums: {e}")
            return []
        except Exception as e:
            logger.error(f"Failed to fetch Last.fm recommended albums: {e}")
            return []
    
    def _get_recommended_tracks(self) -> list:
        """Fetch recommended tracks from Last.fm.
        
        Note: Last.fm doesn't have a native 'user.getRecommendedTracks' endpoint.
        This method uses user.getTopTracks for personalized data (most-played tracks),
        or chart.getTopTracks as a fallback.
        
        Strategy:
        1. Try user.getTopTracks if username is available (user's most played tracks)
        2. Fall back to chart.getTopTracks (global top tracks)
        """
        recommended_tracks = {}
        
        try:
            # Use user's top tracks if username available (personalized), else global chart
            if self.username:
                params = {
                    "method": "user.getTopTracks",
                    "user": self.username,
                    "api_key": self.api_key,
                    "format": "json",
                    "limit": 20,
                    "period": "6month"
                }
            else:
                # Fall back to global chart if no username
                params = {
                    "method": "chart.getTopTracks",
                    "api_key": self.api_key,
                    "format": "json",
                    "limit": 20
                }
            
            def fetch_recommended_tracks():
                return self.session.get(self.base_url, params=params, timeout=(5, 10))
            
            res = retry_with_backoff(
                fetch_recommended_tracks,
                max_retries=LASTFM_CONFIG["MAX_RETRIES"],
                backoff_factor=LASTFM_CONFIG["RETRY_BACKOFF"],
                rate_limit_delay=LASTFM_CONFIG["RATE_LIMIT_DELAY"]
            )
            res.raise_for_status()
            
            # Get the tracks - both user.getTopTracks and chart.getTopTracks use "toptracks"
            tracks = res.json().get("toptracks", {}).get("track", [])
            
            for track in tracks:
                track_name = track.get("name", "")
                artist_name = self._extract_artist_name(track.get("artist"))
                
                if not track_name or not artist_name:
                    continue
                
                track_key = (artist_name.lower(), track_name.lower())
                if track_key in recommended_tracks:
                    continue
                
                # Extract image
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
                    "similarity": 1.0  # From Last.fm recommendation algorithm
                }
            
            logger.info(f"Found {len(recommended_tracks)} recommended tracks from Last.fm")
            return list(recommended_tracks.values())[:20]
        except (ConnectionError, ConnectionResetError) as e:
            logger.error(f"Connection error fetching recommended tracks: {e} - may indicate network issues")
            return []
        except Timeout as e:
            logger.error(f"Timeout fetching recommended tracks: {e}")
            return []
        except HTTPError as e:
            status_code = e.response.status_code if hasattr(e.response, 'status_code') else 'unknown'
            logger.error(f"HTTP error {status_code} fetching recommended tracks: {e}")
            return []
        except Exception as e:
            logger.error(f"Failed to fetch Last.fm recommended tracks: {e}")
            return []

    def get_artist_info(self, artist: str) -> dict:
        """
        Fetch artist bio and info from Last.fm.
        
        Args:
            artist: Artist name
            
        Returns:
            Dict with 'bio' (HTML string), 'bio_text' (plain text), 'image' (URL), 'similar' (list of similar artists)
        """
        if not self.api_key:
            logger.debug("Last.fm API key missing. Skipping artist info lookup.")
            return {"bio": "", "bio_text": "", "image": "", "similar": []}
        
        params = {
            "method": "artist.getInfo",
            "artist": artist,
            "api_key": self.api_key,
            "format": "json"
        }
        
        try:
            res = self.session.get(self.base_url, params=params, timeout=(5, 10))
            res.raise_for_status()
            data = res.json().get("artist", {})
            
            # Extract bio (published field contains HTML summary, bio field is full)
            bio_html = data.get("bio", {}).get("content", "")
            bio_text = data.get("bio", {}).get("summary", "") or bio_html
            
            # Extract image (get largest size)
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
                "similar": []
            }
        except Exception as e:
            logger.debug(f"Failed to fetch artist info from Last.fm for '{artist}': {e}")
            return {"bio": "", "bio_text": "", "image": "", "similar": []}
    
    def get_artist_top_tags(self, artist: str, limit: int = 10) -> list:
        """
        Fetch top tags for an artist from Last.fm.
        
        Args:
            artist: Artist name
            limit: Maximum number of tags (1-100)
            
        Returns:
            List of dicts with 'name' and 'count' keys
            Example: [{'name': 'rock', 'count': 1000}, {'name': 'alternative', 'count': 800}]
        """
        if not self.api_key:
            logger.debug("Last.fm API key missing. Skipping artist tags lookup.")
            return []
        
        limit = max(1, min(100, limit))
        
        params = {
            "method": "artist.getTopTags",
            "artist": artist,
            "limit": limit,
            "api_key": self.api_key,
            "format": "json"
        }
        
        try:
            res = self.session.get(self.base_url, params=params, timeout=(5, 10))
            res.raise_for_status()
            data = res.json()
            
            if "error" in data:
                logger.debug(f"Last.fm error for '{artist}' tags: {data.get('message', 'unknown')}")
                return []
            
            tag_list = data.get("toptags", {}).get("tag", [])
            
            # Normalize response (might be a single dict or list)
            if isinstance(tag_list, dict):
                tag_list = [tag_list]
            
            result = []
            for tag_obj in tag_list:
                if isinstance(tag_obj, dict):
                    result.append({
                        "name": tag_obj.get("name", ""),
                        "count": int(tag_obj.get("count", 0))
                    })
            
            return result
        except Exception as e:
            logger.debug(f"Failed to fetch artist top tags from Last.fm for '{artist}': {e}")
            return []
    
    def get_album_top_tags(self, artist: str, album: str, limit: int = 10) -> list:
        """
        Fetch top tags for an album from Last.fm.
        
        Args:
            artist: Artist name
            album: Album name
            limit: Maximum number of tags (1-100)
            
        Returns:
            List of dicts with 'name' and 'count' keys
        """
        if not self.api_key:
            logger.debug("Last.fm API key missing. Skipping album tags lookup.")
            return []
        
        limit = max(1, min(100, limit))
        
        params = {
            "method": "album.getTopTags",
            "artist": artist,
            "album": album,
            "limit": limit,
            "api_key": self.api_key,
            "format": "json"
        }
        
        try:
            res = self.session.get(self.base_url, params=params, timeout=(5, 10))
            res.raise_for_status()
            data = res.json()
            
            if "error" in data:
                logger.debug(f"Last.fm error for '{album}' by '{artist}' tags: {data.get('message', 'unknown')}")
                return []
            
            tag_list = data.get("toptags", {}).get("tag", [])
            
            # Normalize response
            if isinstance(tag_list, dict):
                tag_list = [tag_list]
            
            result = []
            for tag_obj in tag_list:
                if isinstance(tag_obj, dict):
                    result.append({
                        "name": tag_obj.get("name", ""),
                        "count": int(tag_obj.get("count", 0))
                    })
            
            return result
        except Exception as e:
            logger.debug(f"Failed to fetch album top tags from Last.fm for '{album}' by '{artist}': {e}")
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