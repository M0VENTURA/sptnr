"""
Spotify API client module for SPTNR.
Handles Spotify authentication, track searching, and singles detection.

Usage:
    from api_clients.spotify import SpotifyClient
    client = SpotifyClient(client_id, client_secret, http_session)
    artist_id = client.get_artist_id("Artist Name")
    singles = client.get_artist_singles(artist_id)
"""

import base64
import logging
import time
import threading
import requests
from concurrent.futures import ThreadPoolExecutor
from api_clients import session, logger

# Global token cache
_spotify_token = None
_spotify_token_exp = 0
_token_lock = threading.Lock()


class SpotifyClient:
    """Client for interacting with Spotify Web API."""
    
    # Safety limits
    MAX_PAGINATION_ITERATIONS = 100  # Maximum iterations for paginated API calls to prevent infinite loops
    
    def __init__(self, client_id: str, client_secret: str, http_session=None, worker_threads: int = 4):
        """
        Initialize SpotifyClient.
        
        Args:
            client_id: Spotify Client ID
            client_secret: Spotify Client Secret
            http_session: Optional requests session (uses global by default)
            worker_threads: Number of threads for concurrent requests
        """
        self.client_id = client_id
        self.client_secret = client_secret
        self.session = http_session or session
        self.worker_threads = worker_threads
        
        # Per-instance caches
        self._artist_id_cache: dict[str, str] = {}
        self._artist_singles_cache: dict[str, set[str]] = {}
    
    def _get_token(self) -> str:
        """
        Get valid Spotify token via Client Credentials flow.
        Token is cached and automatically refreshed when near expiry.
        """
        global _spotify_token, _spotify_token_exp
        
        # Return cached token if still valid (refresh 60s before expiry)
        if _spotify_token and time.time() < (_spotify_token_exp - 60):
            return _spotify_token
        
        # Acquire lock briefly to check if we need to refresh
        with _token_lock:
            # Double-check after acquiring lock - another thread may have refreshed
            if _spotify_token and time.time() < (_spotify_token_exp - 60):
                return _spotify_token
        
        # Perform token request outside the lock to prevent blocking other threads
        auth_str = f"{self.client_id}:{self.client_secret}"
        headers = {
            "Authorization": "Basic " + base64.b64encode(auth_str.encode()).decode(),
            "Content-Type": "application/x-www-form-urlencoded"
        }
        data = {"grant_type": "client_credentials"}
        
        try:
            # Use self.session instead of requests directly to benefit from retry logic
            res = self.session.post(
                "https://accounts.spotify.com/api/token",
                headers=headers,
                data=data,
                timeout=(5, 10)  # (connect_timeout, read_timeout)
            )
            res.raise_for_status()
            payload = res.json()
            new_token = payload["access_token"]
            new_exp = time.time() + int(payload.get("expires_in", 3600))
            
            # Only acquire lock to update the cached token
            with _token_lock:
                _spotify_token = new_token
                _spotify_token_exp = new_exp
            
            logger.info("✅ Spotify token refreshed")
            return new_token
        except Exception as e:
            # Log error without holding any locks
            logger.error(f"❌ Spotify Token Error: {e}")
            # Return cached token if available, even if expired
            if _spotify_token:
                logger.warning("Using potentially expired token due to refresh failure")
                return _spotify_token
            raise
    
    def _headers(self) -> dict:
        """Build auth headers for Spotify API."""
        return {
            "Authorization": f"Bearer {self._get_token()}",
            "Content-Type": "application/json"
        }
    
    def get_artist_id(self, artist_name: str) -> str | None:
        """
        Search for an artist and cache their Spotify ID.
        
        Args:
            artist_name: Artist name to search for
            
        Returns:
            Spotify artist ID or None if not found
        """
        key = (artist_name or "").strip().lower()
        if key in self._artist_id_cache:
            return self._artist_id_cache[key]
        
        try:
            # Quote artist name to handle special characters like apostrophes
            # This improves search accuracy for artists with punctuation
            params = {"q": f'artist:"{artist_name}"', "type": "artist", "limit": 1}
            res = self.session.get(
                "https://api.spotify.com/v1/search",
                headers=self._headers(),
                params=params,
                timeout=(5, 10)  # (connect_timeout, read_timeout)
            )
            res.raise_for_status()
            items = res.json().get("artists", {}).get("items", [])
            if items:
                artist_id = items[0].get("id")
                self._artist_id_cache[key] = artist_id
                logger.debug(f"Found Spotify artist: {artist_name} → {artist_id}")
                return artist_id
        except Exception as e:
            logger.debug(f"Spotify artist search failed for '{artist_name}': {e}")
        
        return None
    
    def get_artist_singles(self, artist_id: str) -> set[str]:
        """
        Fetch all track IDs from single releases for an artist.
        Results are cached per artist_id.
        
        Args:
            artist_id: Spotify artist ID
            
        Returns:
            Set of Spotify track IDs from single releases
        """
        if not artist_id:
            return set()
        
        if artist_id in self._artist_singles_cache:
            return self._artist_singles_cache[artist_id]
        
        headers = self._headers()
        singles_album_ids: list[str] = []
        
        # Paginate artist albums filtered to singles
        url = f"https://api.spotify.com/v1/artists/{artist_id}/albums"
        params = {"include_groups": "single", "limit": 50}
        
        iteration = 0
        
        try:
            while iteration < self.MAX_PAGINATION_ITERATIONS:
                iteration += 1
                res = self.session.get(url, headers=headers, params=params, timeout=(5, 12))  # (connect_timeout, read_timeout)
                res.raise_for_status()
                payload = res.json()
                singles_album_ids.extend([
                    a.get("id") for a in payload.get("items", []) if a.get("id")
                ])
                
                next_url = payload.get("next")
                if next_url:
                    url, params = next_url, None  # 'next' already has full query
                else:
                    break
            
            if iteration >= self.MAX_PAGINATION_ITERATIONS:
                logger.warning(f"Reached max iterations ({self.MAX_PAGINATION_ITERATIONS}) fetching singles for artist {artist_id}")
        except Exception as e:
            logger.debug(f"Spotify singles album fetch failed for '{artist_id}': {e}")
        
        # Fetch tracks for each single album (with bounded concurrency)
        single_track_ids: set[str] = set()
        
        def _fetch_album_tracks(album_id: str) -> list[str]:
            """Fetch tracks from a single Spotify album."""
            try:
                res = self.session.get(
                    f"https://api.spotify.com/v1/albums/{album_id}/tracks",
                    headers=headers,
                    params={"limit": 50},
                    timeout=(5, 12)  # (connect_timeout, read_timeout)
                )
                res.raise_for_status()
                return [
                    t.get("id") for t in (res.json().get("items") or []) if t.get("id")
                ]
            except Exception as e:
                logger.debug(f"Failed to fetch tracks for album {album_id}: {e}")
                return []
        
        # Use thread pool for concurrent album track fetching
        with ThreadPoolExecutor(max_workers=self.worker_threads) as pool:
            futures = [
                pool.submit(_fetch_album_tracks, aid) 
                for aid in singles_album_ids[:250]  # safety cap
            ]
            for future in futures:
                for track_id in (future.result() or []):
                    single_track_ids.add(track_id)
        
        self._artist_singles_cache[artist_id] = single_track_ids
        logger.info(f"✅ Fetched {len(single_track_ids)} single track IDs for artist {artist_id}")
        return single_track_ids
    
    def get_playlist_tracks(self, playlist_id: str) -> list[dict]:
        """
        Fetch all tracks from a Spotify playlist.
        
        Args:
            playlist_id: Spotify playlist ID
            
        Returns:
            List of track dictionaries with title, artist, album, spotify_uri, spotify_id
        """
        headers = self._headers()
        tracks = []
        
        try:
            url = f"https://api.spotify.com/v1/playlists/{playlist_id}/tracks"
            params = {"limit": 100}
            
            while url:
                res = self.session.get(url, headers=headers, params=params, timeout=(5, 15))  # (connect_timeout, read_timeout)
                res.raise_for_status()
                payload = res.json()
                
                for item in payload.get("items", []):
                    track = item.get("track", {})
                    if track and track.get("id"):
                        artist = ", ".join([a.get("name", "") for a in track.get("artists", [])])
                        tracks.append({
                            "title": track.get("name", ""),
                            "artist": artist,
                            "album": track.get("album", {}).get("name", ""),
                            "spotify_uri": track.get("uri", ""),
                            "spotify_id": track.get("id", "")
                        })
                
                # Get next page
                url = payload.get("next")
                params = None  # next URL already has query params
            
            logger.info(f"✅ Fetched {len(tracks)} tracks from playlist {playlist_id}")
            return tracks
            
        except Exception as e:
            logger.error(f"❌ Failed to fetch playlist {playlist_id}: {e}")
            raise
    
    def search_track(self, title: str, artist: str, album: str = None) -> list:
        """
        Search for a track on Spotify with fallback queries.
        
        Args:
            title: Track title
            artist: Artist name
            album: Album name (optional)
            
        Returns:
            List of matching track objects
        """
        headers = self._headers()
        
        def _query(q: str) -> list:
            """Execute a single search query."""
            try:
                params = {"q": q, "type": "track", "limit": 10}
                res = self.session.get(
                    "https://api.spotify.com/v1/search",
                    headers=headers,
                    params=params,
                    timeout=(5, 10)  # (connect_timeout, read_timeout)
                )
                res.raise_for_status()
                return res.json().get("tracks", {}).get("items", []) or []
            except Exception as e:
                logger.debug(f"Spotify track search failed for '{q}': {e}")
                return []
        
        # Try multiple query strategies
        queries = [
            f"{title} artist:{artist} album:{album}" if album else None,
            f"{title} artist:{artist}",
        ]
        
        all_results = []
        for q in filter(None, queries):
            results = _query(q)
            if results:
                all_results.extend(results)
        
        return all_results
    
    def get_audio_features(self, track_id: str) -> dict | None:
        """
        Fetch audio features for a track from Spotify /audio-features endpoint.
        
        Args:
            track_id: Spotify track ID
            
        Returns:
            Dictionary with audio features or None if not found
        """
        if not track_id:
            return None
        
        headers = self._headers()
        
        try:
            res = self.session.get(
                f"https://api.spotify.com/v1/audio-features/{track_id}",
                headers=headers,
                timeout=(5, 10)
            )
            res.raise_for_status()
            return res.json()
        except Exception as e:
            logger.debug(f"Failed to fetch audio features for track {track_id}: {e}")
            return None
    
    def get_audio_features_batch(self, track_ids: list[str]) -> dict[str, dict]:
        """
        Fetch audio features for multiple tracks in a single request (up to 100).
        
        Args:
            track_ids: List of Spotify track IDs (max 100)
            
        Returns:
            Dictionary mapping track_id to audio features
        """
        if not track_ids:
            return {}
        
        # Spotify API allows up to 100 IDs per request
        track_ids = track_ids[:100]
        headers = self._headers()
        
        try:
            params = {"ids": ",".join(track_ids)}
            res = self.session.get(
                "https://api.spotify.com/v1/audio-features",
                headers=headers,
                params=params,
                timeout=(5, 15)
            )
            res.raise_for_status()
            features_list = res.json().get("audio_features", [])
            
            # Map track IDs to features
            result = {}
            for features in features_list:
                if features and features.get("id"):
                    result[features["id"]] = features
            
            logger.debug(f"Fetched audio features for {len(result)}/{len(track_ids)} tracks")
            return result
        except Exception as e:
            logger.debug(f"Failed to fetch batch audio features: {e}")
            return {}
    
    def get_artist_metadata(self, artist_id: str) -> dict | None:
        """
        Fetch artist metadata including genres and popularity from /artists endpoint.
        
        Args:
            artist_id: Spotify artist ID
            
        Returns:
            Dictionary with artist metadata or None if not found
        """
        if not artist_id:
            return None
        
        headers = self._headers()
        
        try:
            res = self.session.get(
                f"https://api.spotify.com/v1/artists/{artist_id}",
                headers=headers,
                timeout=(5, 10)
            )
            res.raise_for_status()
            return res.json()
        except Exception as e:
            logger.debug(f"Failed to fetch artist metadata for {artist_id}: {e}")
            return None
    
    def get_album_metadata(self, album_id: str) -> dict | None:
        """
        Fetch album metadata including label, total tracks, and type from /albums endpoint.
        
        Args:
            album_id: Spotify album ID
            
        Returns:
            Dictionary with album metadata or None if not found
        """
        if not album_id:
            return None
        
        headers = self._headers()
        
        try:
            res = self.session.get(
                f"https://api.spotify.com/v1/albums/{album_id}",
                headers=headers,
                timeout=(5, 10)
            )
            res.raise_for_status()
            return res.json()
        except Exception as e:
            logger.debug(f"Failed to fetch album metadata for {album_id}: {e}")
            return None
    
    def get_track_metadata(self, track_id: str) -> dict | None:
        """
        Fetch complete track metadata from Spotify /tracks endpoint.
        
        Args:
            track_id: Spotify track ID
            
        Returns:
            Dictionary with track metadata or None if not found
        """
        if not track_id:
            return None
        
        headers = self._headers()
        
        try:
            res = self.session.get(
                f"https://api.spotify.com/v1/tracks/{track_id}",
                headers=headers,
                timeout=(5, 10)
            )
            res.raise_for_status()
            return res.json()
        except Exception as e:
            logger.debug(f"Failed to fetch track metadata for {track_id}: {e}")
            return None


def get_spotify_user_playlists(client_id: str, client_secret: str) -> list[dict]:
    """
    Fetch all playlists from Spotify using the current user context.
    
    Uses Authorization Code Flow with a cached token or fetches a new one.
    Falls back to browsing featured/category playlists if user auth unavailable.
    
    Args:
        client_id: Spotify Client ID
        client_secret: Spotify Client Secret
        
    Returns:
        List of playlists with id, name, image_url, and track_count
    """
    try:
        import os
        
        # Try to get user auth token from environment or cache
        user_token = os.environ.get("SPOTIFY_USER_TOKEN")
        
        if not user_token:
            # Fallback: Use Client Credentials to get featured playlists
            logger.debug("User token not available, fetching featured playlists instead")
            client = SpotifyClient(client_id, client_secret)
            
            headers = client._headers()
            playlists = []
            next_url = "https://api.spotify.com/v1/browse/featured-playlists?limit=50&country=AU"
            
            while next_url and len(playlists) < 100:  # Limit to 100 playlists
                try:
                    res = requests.get(next_url, headers=headers, timeout=(5, 10))  # (connect_timeout, read_timeout)
                    if res.status_code == 404:
                        logger.error(f"Spotify API endpoint not found: {next_url}")
                        break
                    res.raise_for_status()
                    data = res.json()
                    for item in data.get("playlists", {}).get("items", []):
                        playlists.append({
                            "id": item["id"],
                            "name": item["name"],
                            "description": item.get("description", ""),
                            "image_url": (item.get("images", [{}])[0] or {}).get("url"),
                            "track_count": item.get("tracks", {}).get("total", 0),
                            "owner": item.get("owner", {}).get("display_name", "Spotify"),
                            "external_url": item.get("external_urls", {}).get("spotify", "")
                        })
                    next_url = data.get("playlists", {}).get("next")
                    if not next_url:
                        break
                except Exception as e:
                    logger.error(f"Failed to fetch featured playlists: {e}")
                    break
            
            return playlists
        
        else:
            # User authenticated - fetch their playlists
            headers = {
                "Authorization": f"Bearer {user_token}",
                "Content-Type": "application/json"
            }
            
            playlists = []
            next_url = "https://api.spotify.com/v1/me/playlists?limit=50"
            
            while next_url:
                try:
                    res = requests.get(next_url, headers=headers, timeout=(5, 10))  # (connect_timeout, read_timeout)
                    res.raise_for_status()
                    data = res.json()
                    
                    for item in data.get("items", []):
                        playlists.append({
                            "id": item["id"],
                            "name": item["name"],
                            "description": item.get("description", ""),
                            "image_url": (item.get("images", [{}])[0] or {}).get("url"),
                            "track_count": item.get("tracks", {}).get("total", 0),
                            "owner": item.get("owner", {}).get("display_name", ""),
                            "external_url": item.get("external_urls", {}).get("spotify", "")
                        })
                    
                    next_url = data.get("next")
                    if not next_url:
                        break
                    
                except Exception as e:
                    logger.error(f"Failed to fetch user playlists: {e}")
                    break
            
            return playlists
    
    except Exception as e:
        logger.error(f"Error getting Spotify playlists: {e}")
        return []


def get_spotify_user_public_playlists(user_id: str, client_id: str, client_secret: str) -> list[dict]:
    """
    Fetch public playlists for a specific Spotify user.
    
    Args:
        user_id: Spotify User ID (e.g., "spotify", "12345678")
        client_id: Spotify Client ID
        client_secret: Spotify Client Secret
        
    Returns:
        List of public playlists with id, name, image_url, and track_count
    """
    try:
        if not user_id:
            logger.warning("No user_id provided for fetching public playlists")
            return []
        
        # Use Client Credentials flow (doesn't require user auth)
        client = SpotifyClient(client_id, client_secret)
        headers = client._headers()
        
        playlists = []
        next_url = f"https://api.spotify.com/v1/users/{user_id}/playlists?limit=50"
        
        while next_url and len(playlists) < 100:  # Limit to 100 playlists
            try:
                res = requests.get(next_url, headers=headers, timeout=(5, 10))
                
                if res.status_code == 404:
                    logger.error(f"Spotify user not found: {user_id}")
                    break
                
                res.raise_for_status()
                data = res.json()
                
                for item in data.get("items", []):
                    # Only include public playlists
                    if item.get("public", False):
                        playlists.append({
                            "id": item["id"],
                            "name": item["name"],
                            "description": item.get("description", ""),
                            "image_url": (item.get("images", [{}])[0] or {}).get("url"),
                            "track_count": item.get("tracks", {}).get("total", 0),
                            "owner": item.get("owner", {}).get("display_name", user_id),
                            "external_url": item.get("external_urls", {}).get("spotify", "")
                        })
                
                next_url = data.get("next")
                if not next_url:
                    break
                
            except Exception as e:
                logger.error(f"Failed to fetch playlists for user {user_id}: {e}")
                break
        
        logger.info(f"Fetched {len(playlists)} public playlists for user {user_id}")
        return playlists
    
    except Exception as e:
        logger.error(f"Error getting public playlists for user {user_id}: {e}")
        return []


