"""Last.fm API client module with enhanced discovery features."""
import logging
import json
import time
import os
from pathlib import Path
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
    """
    import random
    
    for attempt in range(max_retries):
        try:
            # Rate limiting
            time.sleep(rate_limit_delay)
            
            result = func()
            return result
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            
            # Exponential backoff with jitter
            wait_time = (backoff_factor ** attempt) + random.uniform(0, 1)
            logger.debug(f"Retry attempt {attempt + 1}/{max_retries} after {wait_time:.2f}s: {e}")
            time.sleep(wait_time)
    
    return None



class LastFmClient:
    """Last.fm API wrapper with enhanced discovery features from DiscoveryLastFM."""
    
    def __init__(self, api_key: str, username: str = None, http_session=None):
        """
        Initialize Last.fm client.
        
        Args:
            api_key: Last.fm API key
            username: Last.fm username for personalized recommendations (optional)
            http_session: Optional requests.Session (uses shared if not provided)
        """
        self.api_key = api_key
        self.username = username
        self.session = http_session or session
        self.base_url = "https://ws.audioscrobbler.com/2.0/"
        self.cache = RecommendationCache()
        
        # Try to import MusicBrainz client for album filtering
        try:
            from .musicbrainz import MusicBrainzClient
            self.mb_client = MusicBrainzClient()
        except Exception as e:
            logger.debug(f"MusicBrainz client not available for album filtering: {e}")
            self.mb_client = None
    
    def _is_studio_album(self, artist: str, album: str) -> bool:
        """
        Filter to only include studio albums (exclude compilations, live, EPs).
        
        Uses MusicBrainz if available, otherwise returns True for albums to be permissive.
        """
        if not self.mb_client:
            return True  # If MB not available, include album
        
        try:
            # Query MusicBrainz for release info
            releases = self.mb_client.search_releases(f'artist:"{artist}" AND release:"{album}"')
            
            if not releases:
                return True  # If not found, include it
            
            # Check the first result
            first_release = releases[0] if isinstance(releases, list) else releases
            album_type = (first_release.get("primaryType") or "").lower()
            secondary_types = [t.lower() for t in (first_release.get("secondaryTypes") or [])]
            
            # Only accept Studio albums
            if album_type != "album":
                logger.debug(f"Filtering out {album} by {artist}: type={album_type}")
                return False
            
            # Exclude compilations, live, remix albums
            excluded = {"compilation", "live", "remix", "ep"}
            if any(t in excluded for t in secondary_types):
                logger.debug(f"Filtering out {album} by {artist}: secondary_types={secondary_types}")
                return False
            
            return True
        except Exception as e:
            logger.debug(f"MusicBrainz filtering failed for {album}/{artist}: {e}")
            return True  # If error, include it (permissive)
    
    def get_track_info(self, artist: str, title: str) -> dict:
        """
        Fetch track playcount and metadata from Last.fm.
        
        Args:
            artist: Artist name
            title: Track title
            
        Returns:
            Dict with 'track_play' and other metadata including 'toptags'
        """
        if not self.api_key:
            logger.warning("Last.fm API key missing. Skipping lookup.")
            return {"track_play": 0}
        
        # Note: requests library automatically handles URL encoding of params dict
        # Special characters like '+' in artist names (e.g., "+44") are properly encoded
        params = {
            "method": "track.getInfo",
            "artist": artist,
            "track": title,
            "api_key": self.api_key,
            "format": "json"
        }
        
        try:
            res = self.session.get(self.base_url, params=params, timeout=(5, 10))  # (connect_timeout, read_timeout)
            res.raise_for_status()
            data = res.json().get("track", {})
            track_play = int(data.get("playcount", 0))
            toptags = data.get("toptags", {})
            return {
                "track_play": track_play,
                "toptags": toptags
            }
        except Exception as e:
            logger.error(f"Last.fm fetch failed for '{title}' by '{artist}': {e}")
            return {"track_play": 0, "toptags": {}}
    
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
        
        params = {
            "method": "album.getInfo",
            "artist": artist,
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
            
            return 0
        except Exception as e:
            logger.debug(f"Last.fm album lookup failed for '{album}' by '{artist}': {e}")
            return 0
    
    def get_recommendations(self) -> dict:
        """
        Fetch personalized recommendations from Last.fm for the current user.
        
        Features (inspired by DiscoveryLastFM):
        - Caching to avoid duplicate API calls
        - Retry logic with exponential backoff  
        - Rate limiting between requests
        - Minimum play count filtering
        - Studio album filtering (via MusicBrainz)
        
        Returns:
            Dict with 'artists', 'albums', and 'tracks' keys containing recommendations
        """
        if not self.api_key:
            logger.warning("Last.fm API key missing. Skipping recommendations.")
            return {"artists": [], "albums": [], "tracks": []}
        
        # Check cache first
        cache_key = f"recommendations_{self.username or 'global'}"
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
            
            # Cache the result
            self.cache.set(cache_key, recommendations)
            return recommendations
        except Exception as e:
            logger.error(f"Failed to fetch Last.fm recommendations: {e}")
            return {"artists": [], "albums": [], "tracks": []}
    
    def _get_recommended_artists(self) -> list:
        """Fetch recommended artists from Last.fm based on similar artists.
        
        Enhanced with DiscoveryLastFM features:
        - Minimum play count filtering (MIN_ARTIST_PLAYS)
        - Minimum similarity score (MIN_SIMILARITY_SCORE)
        - Rate limiting between requests
        - Retry logic with exponential backoff
        
        Strategy:
        1. Get user's top artists (to understand their taste)
        2. Filter by minimum play count
        3. For each top artist, fetch similar artists using artist.getSimilar
        4. Filter by minimum similarity score
        5. Dedup and return the similar artists (not the user's own top artists)
        """
        recommended_artists = {}
        
        try:
            # Step 1: Get user's top artists to understand their taste
            if self.username:
                params = {
                    "method": "user.getTopArtists",
                    "user": self.username,
                    "api_key": self.api_key,
                    "format": "json",
                    "limit": 15,  # Fetch more to account for filtering
                    "period": f"{LASTFM_CONFIG['RECENT_MONTHS']}month"
                }
            else:
                # Fall back to global chart
                params = {
                    "method": "chart.getTopArtists",
                    "api_key": self.api_key,
                    "format": "json",
                    "limit": 15
                }
            
            def fetch_top_artists():
                return self.session.get(self.base_url, params=params, timeout=(5, 10))
            
            res = retry_with_backoff(
                fetch_top_artists,
                max_retries=LASTFM_CONFIG["MAX_RETRIES"],
                backoff_factor=LASTFM_CONFIG["RETRY_BACKOFF"],
                rate_limit_delay=LASTFM_CONFIG["RATE_LIMIT_DELAY"]
            )
            res.raise_for_status()
            
            # Get the top artists
            if self.username:
                top_artists = res.json().get("topartists", {}).get("artist", [])
            else:
                top_artists = res.json().get("artists", {}).get("artist", [])
            
            # Step 2: For each top artist, get similar artists
            for top_artist in top_artists:
                artist_name = top_artist.get("name", "")
                if not artist_name:
                    continue
                
                # Filter by minimum play count (for user recommendations)
                if self.username:
                    playcount = int(top_artist.get("playcount", 0) or 0)
                    if playcount < LASTFM_CONFIG["MIN_ARTIST_PLAYS"]:
                        logger.debug(f"Skipping {artist_name}: {playcount} plays < {LASTFM_CONFIG['MIN_ARTIST_PLAYS']}")
                        continue
                
                try:
                    # Fetch similar artists using artist.getSimilar
                    similar_params = {
                        "method": "artist.getSimilar",
                        "artist": artist_name,
                        "api_key": self.api_key,
                        "format": "json",
                        "limit": LASTFM_CONFIG["MAX_SIMILAR_PER_ARTIST"]
                    }
                    
                    def fetch_similar():
                        return self.session.get(self.base_url, params=similar_params, timeout=(5, 10))
                    
                    similar_res = retry_with_backoff(
                        fetch_similar,
                        max_retries=2,  # Lower retry count for nested requests
                        backoff_factor=LASTFM_CONFIG["RETRY_BACKOFF"],
                        rate_limit_delay=LASTFM_CONFIG["RATE_LIMIT_DELAY"]
                    )
                    
                    if similar_res.status_code == 200:
                        similar_artists = similar_res.json().get("similarartists", {}).get("artist", [])
                        
                        for similar_artist in similar_artists:
                            name = similar_artist.get("name", "")
                            if not name or name in recommended_artists:
                                continue
                            
                            # Filter by minimum similarity score
                            match = float(similar_artist.get("match", 0) or 0)
                            if match < LASTFM_CONFIG["MIN_SIMILARITY_SCORE"]:
                                logger.debug(f"Skipping {name}: match {match} < {LASTFM_CONFIG['MIN_SIMILARITY_SCORE']}")
                                continue
                            
                            # Extract image
                            image_url = ""
                            if isinstance(similar_artist.get("image"), list):
                                for img in reversed(similar_artist["image"]):
                                    if img.get("#text"):
                                        image_url = img.get("#text", "")
                                        break
                            
                            recommended_artists[name] = {
                                "name": name,
                                "listeners": similar_artist.get("listeners", 0),
                                "match": match,
                                "playcount": 0,
                                "image": image_url,
                                "url": similar_artist.get("url", "")
                            }
                except Exception as e:
                    logger.debug(f"Failed to fetch similar artists for {artist_name}: {e}")
                    continue
            
            logger.info(f"Found {len(recommended_artists)} recommended artists (after filtering)")
            return list(recommended_artists.values())[:20]
        except Exception as e:
            logger.error(f"Failed to fetch recommended artists: {e}")
            return []
    
    def _get_recommended_albums(self) -> list:
        """Fetch recommended albums from Last.fm based on similar artists.
        
        Enhanced with DiscoveryLastFM features:
        - Studio album filtering via MusicBrainz
        - Minimum play count filtering
        - Minimum similarity score filtering
        - Rate limiting and retry logic
        - Caching and deduplication
        
        Strategy:
        1. Get user's top artists (to understand their taste)
        2. Filter by minimum play count
        3. For each top artist, fetch similar artists (with similarity score)
        4. For each similar artist, fetch their top albums
        5. Filter to only studio album releases using MusicBrainz
        6. Return albums from similar artists (NEW recommendations, not user's own top albums)
        """
        recommended_albums = {}
        
        try:
            # Step 1: Get user's top artists
            if self.username:
                params = {
                    "method": "user.getTopArtists",
                    "user": self.username,
                    "api_key": self.api_key,
                    "format": "json",
                    "limit": 12,  # Fetch more to account for filtering
                    "period": f"{LASTFM_CONFIG['RECENT_MONTHS']}month"
                }
            else:
                params = {
                    "method": "chart.getTopArtists",
                    "api_key": self.api_key,
                    "format": "json",
                    "limit": 12
                }
            
            def fetch_top_artists():
                return self.session.get(self.base_url, params=params, timeout=(5, 10))
            
            res = retry_with_backoff(
                fetch_top_artists,
                max_retries=LASTFM_CONFIG["MAX_RETRIES"],
                backoff_factor=LASTFM_CONFIG["RETRY_BACKOFF"],
                rate_limit_delay=LASTFM_CONFIG["RATE_LIMIT_DELAY"]
            )
            res.raise_for_status()
            
            if self.username:
                top_artists = res.json().get("topartists", {}).get("artist", [])
            else:
                top_artists = res.json().get("artists", {}).get("artist", [])
            
            # Step 2: For each top artist, get similar artists and their albums
            for top_artist in top_artists:
                artist_name = top_artist.get("name", "")
                if not artist_name:
                    continue
                
                # Filter by minimum play count (for user recommendations)
                if self.username:
                    playcount = int(top_artist.get("playcount", 0) or 0)
                    if playcount < LASTFM_CONFIG["MIN_ARTIST_PLAYS"]:
                        continue
                
                try:
                    # Get similar artists
                    similar_params = {
                        "method": "artist.getSimilar",
                        "artist": artist_name,
                        "api_key": self.api_key,
                        "format": "json",
                        "limit": 3
                    }
                    
                    def fetch_similar():
                        return self.session.get(self.base_url, params=similar_params, timeout=(5, 10))
                    
                    similar_res = retry_with_backoff(
                        fetch_similar,
                        max_retries=2,
                        backoff_factor=LASTFM_CONFIG["RETRY_BACKOFF"],
                        rate_limit_delay=LASTFM_CONFIG["RATE_LIMIT_DELAY"]
                    )
                    
                    if similar_res and similar_res.status_code == 200:
                        similar_artists = similar_res.json().get("similarartists", {}).get("artist", [])
                        
                        # Step 3: Get top albums from each similar artist
                        for similar_artist in similar_artists:
                            similar_artist_name = similar_artist.get("name", "")
                            if not similar_artist_name:
                                continue
                            
                            # Filter by similarity score
                            match = float(similar_artist.get("match", 0) or 0)
                            if match < LASTFM_CONFIG["MIN_SIMILARITY_SCORE"]:
                                continue
                            
                            try:
                                album_params = {
                                    "method": "artist.getTopAlbums",
                                    "artist": similar_artist_name,
                                    "api_key": self.api_key,
                                    "format": "json",
                                    "limit": LASTFM_CONFIG["MAX_ALBUMS_PER_ARTIST"]
                                }
                                
                                def fetch_albums():
                                    return self.session.get(self.base_url, params=album_params, timeout=(5, 10))
                                
                                album_res = retry_with_backoff(
                                    fetch_albums,
                                    max_retries=2,
                                    backoff_factor=LASTFM_CONFIG["RETRY_BACKOFF"],
                                    rate_limit_delay=LASTFM_CONFIG["RATE_LIMIT_DELAY"]
                                )
                                
                                if album_res and album_res.status_code == 200:
                                    top_albums = album_res.json().get("topalbums", {}).get("album", [])
                                    
                                    for album in top_albums:
                                        album_name = album.get("name", "")
                                        if not album_name:
                                            continue
                                        
                                        album_key = (similar_artist_name.lower(), album_name.lower())
                                        if album_key in recommended_albums:
                                            continue
                                        
                                        # ENHANCED: Filter to studio albums only using MusicBrainz
                                        if not self._is_studio_album(similar_artist_name, album_name):
                                            logger.debug(f"Filtering non-studio album: {album_name} by {similar_artist_name}")
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
                                            "artist": similar_artist_name,
                                            "playcount": 0,
                                            "image": image_url,
                                            "url": album.get("url", ""),
                                            "similarity": match
                                        }
                            except Exception as e:
                                logger.debug(f"Failed to fetch albums for {similar_artist_name}: {e}")
                                continue
                except Exception as e:
                    logger.debug(f"Failed to fetch similar artists for {artist_name}: {e}")
                    continue
            
            logger.info(f"Found {len(recommended_albums)} recommended studio albums (after filtering)")
            return list(recommended_albums.values())[:12]
        except Exception as e:
            logger.error(f"Failed to fetch recommended albums: {e}")
            return []
    
    def _get_recommended_tracks(self) -> list:
        """Fetch recommended tracks from Last.fm based on similar artists.
        
        Enhanced with DiscoveryLastFM features:
        - Minimum play count filtering
        - Minimum similarity score filtering
        - Rate limiting and retry logic
        - Caching and deduplication
        
        Strategy:
        1. Get user's top artists (to understand their taste)
        2. Filter by minimum play count
        3. For each top artist, fetch similar artists (with similarity score)
        4. For each similar artist, fetch their top tracks
        5. Return tracks from similar artists (NEW recommendations, not user's own recent tracks)
        """
        recommended_tracks = {}
        
        try:
            # Step 1: Get user's top artists
            if self.username:
                params = {
                    "method": "user.getTopArtists",
                    "user": self.username,
                    "api_key": self.api_key,
                    "format": "json",
                    "limit": 12,  # Fetch more to account for filtering
                    "period": f"{LASTFM_CONFIG['RECENT_MONTHS']}month"
                }
            else:
                params = {
                    "method": "chart.getTopArtists",
                    "api_key": self.api_key,
                    "format": "json",
                    "limit": 12
                }
            
            def fetch_top_artists():
                return self.session.get(self.base_url, params=params, timeout=(5, 10))
            
            res = retry_with_backoff(
                fetch_top_artists,
                max_retries=LASTFM_CONFIG["MAX_RETRIES"],
                backoff_factor=LASTFM_CONFIG["RETRY_BACKOFF"],
                rate_limit_delay=LASTFM_CONFIG["RATE_LIMIT_DELAY"]
            )
            res.raise_for_status()
            
            if self.username:
                top_artists = res.json().get("topartists", {}).get("artist", [])
            else:
                top_artists = res.json().get("artists", {}).get("artist", [])
            
            # Step 2: For each top artist, get similar artists and their tracks
            for top_artist in top_artists:
                artist_name = top_artist.get("name", "")
                if not artist_name:
                    continue
                
                # Filter by minimum play count (for user recommendations)
                if self.username:
                    playcount = int(top_artist.get("playcount", 0) or 0)
                    if playcount < LASTFM_CONFIG["MIN_ARTIST_PLAYS"]:
                        continue
                
                try:
                    # Get similar artists
                    similar_params = {
                        "method": "artist.getSimilar",
                        "artist": artist_name,
                        "api_key": self.api_key,
                        "format": "json",
                        "limit": 3
                    }
                    
                    def fetch_similar():
                        return self.session.get(self.base_url, params=similar_params, timeout=(5, 10))
                    
                    similar_res = retry_with_backoff(
                        fetch_similar,
                        max_retries=2,
                        backoff_factor=LASTFM_CONFIG["RETRY_BACKOFF"],
                        rate_limit_delay=LASTFM_CONFIG["RATE_LIMIT_DELAY"]
                    )
                    
                    if similar_res and similar_res.status_code == 200:
                        similar_artists = similar_res.json().get("similarartists", {}).get("artist", [])
                        
                        # Step 3: Get top tracks from each similar artist
                        for similar_artist in similar_artists:
                            similar_artist_name = similar_artist.get("name", "")
                            if not similar_artist_name:
                                continue
                            
                            # Filter by similarity score
                            match = float(similar_artist.get("match", 0) or 0)
                            if match < LASTFM_CONFIG["MIN_SIMILARITY_SCORE"]:
                                continue
                            
                            try:
                                track_params = {
                                    "method": "artist.getTopTracks",
                                    "artist": similar_artist_name,
                                    "api_key": self.api_key,
                                    "format": "json",
                                    "limit": 4
                                }
                                
                                def fetch_tracks():
                                    return self.session.get(self.base_url, params=track_params, timeout=(5, 10))
                                
                                track_res = retry_with_backoff(
                                    fetch_tracks,
                                    max_retries=2,
                                    backoff_factor=LASTFM_CONFIG["RETRY_BACKOFF"],
                                    rate_limit_delay=LASTFM_CONFIG["RATE_LIMIT_DELAY"]
                                )
                                
                                if track_res and track_res.status_code == 200:
                                    top_tracks = track_res.json().get("toptracks", {}).get("track", [])
                                    
                                    for track in top_tracks:
                                        track_name = track.get("name", "")
                                        if not track_name:
                                            continue
                                        
                                        track_key = (similar_artist_name.lower(), track_name.lower())
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
                                            "artist": similar_artist_name,
                                            "playcount": track.get("playcount", 0),
                                            "image": image_url,
                                            "url": track.get("url", ""),
                                            "similarity": match
                                        }
                            except Exception as e:
                                logger.debug(f"Failed to fetch tracks for {similar_artist_name}: {e}")
                                continue
                except Exception as e:
                    logger.debug(f"Failed to fetch similar artists for {artist_name}: {e}")
                    continue
            
            logger.info(f"Found {len(recommended_tracks)} recommended tracks (after filtering)")
            return list(recommended_tracks.values())[:20]
        except Exception as e:
            logger.error(f"Failed to fetch recommended tracks: {e}")
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
def get_lastfm_recommendations(api_key: str, username: str | None = None) -> dict:
    """Fetch Last.fm recommendations."""
    client = LastFmClient(api_key, username=username)
    return client.get_recommendations()