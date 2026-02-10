"""Last.fm API client module."""
import logging
from . import session

logger = logging.getLogger(__name__)


class LastFmClient:
    """Last.fm API wrapper for track info and listening stats."""
    
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
    
    def get_recommendations(self) -> dict:
        """
        Fetch personalized recommendations from Last.fm for the current user.
        
        Returns:
            Dict with 'artists', 'albums', and 'tracks' keys containing recommendations
        """
        if not self.api_key:
            logger.warning("Last.fm API key missing. Skipping recommendations.")
            return {"artists": [], "albums": [], "tracks": []}
        
        try:
            recommendations = {
                "artists": self._get_recommended_artists(),
                "albums": self._get_recommended_albums(),
                "tracks": self._get_recommended_tracks()
            }
            return recommendations
        except Exception as e:
            logger.error(f"Failed to fetch Last.fm recommendations: {e}")
            return {"artists": [], "albums": [], "tracks": []}
    
    def _get_recommended_artists(self) -> list:
        """Fetch recommended artists from Last.fm based on similar artists.
        
        Strategy:
        1. Get user's top artists (to understand their taste)
        2. For each top artist, fetch similar artists using artist.getSimilar
        3. Dedup and return the similar artists (not the user's own top artists)
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
                    "limit": 10,
                    "period": "3month"  # Use recent period for current taste
                }
            else:
                # Fall back to global chart
                params = {
                    "method": "chart.getTopArtists",
                    "api_key": self.api_key,
                    "format": "json",
                    "limit": 10
                }
            
            res = self.session.get(self.base_url, params=params, timeout=(5, 10))
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
                
                try:
                    # Fetch similar artists using artist.getSimilar
                    similar_params = {
                        "method": "artist.getSimilar",
                        "artist": artist_name,
                        "api_key": self.api_key,
                        "format": "json",
                        "limit": 5
                    }
                    
                    similar_res = self.session.get(self.base_url, params=similar_params, timeout=(5, 10))
                    if similar_res.status_code == 200:
                        similar_artists = similar_res.json().get("similarartists", {}).get("artist", [])
                        
                        for similar_artist in similar_artists:
                            name = similar_artist.get("name", "")
                            if not name or name in recommended_artists:
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
                                "playcount": 0,
                                "image": image_url,
                                "url": similar_artist.get("url", "")
                            }
                except Exception as e:
                    logger.debug(f"Failed to fetch similar artists for {artist_name}: {e}")
                    continue
            
            return list(recommended_artists.values())[:20]
        except Exception as e:
            logger.error(f"Failed to fetch recommended artists: {e}")
            return []
    
    def _get_recommended_albums(self) -> list:
        """Fetch recommended albums from Last.fm based on similar artists.
        
        Strategy:
        1. Get user's top artists (to understand their taste)
        2. For each top artist, fetch similar artists
        3. For each similar artist, fetch their top albums
        4. Return albums from similar artists (NEW recommendations, not user's own top albums)
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
                    "limit": 8,
                    "period": "3month"
                }
            else:
                params = {
                    "method": "chart.getTopArtists",
                    "api_key": self.api_key,
                    "format": "json",
                    "limit": 8
                }
            
            res = self.session.get(self.base_url, params=params, timeout=(5, 10))
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
                
                try:
                    # Get similar artists
                    similar_params = {
                        "method": "artist.getSimilar",
                        "artist": artist_name,
                        "api_key": self.api_key,
                        "format": "json",
                        "limit": 3
                    }
                    
                    similar_res = self.session.get(self.base_url, params=similar_params, timeout=(5, 10))
                    if similar_res.status_code == 200:
                        similar_artists = similar_res.json().get("similarartists", {}).get("artist", [])
                        
                        # Step 3: Get top albums from each similar artist
                        for similar_artist in similar_artists:
                            similar_artist_name = similar_artist.get("name", "")
                            if not similar_artist_name:
                                continue
                            
                            try:
                                album_params = {
                                    "method": "artist.getTopAlbums",
                                    "artist": similar_artist_name,
                                    "api_key": self.api_key,
                                    "format": "json",
                                    "limit": 3
                                }
                                
                                album_res = self.session.get(self.base_url, params=album_params, timeout=(5, 10))
                                if album_res.status_code == 200:
                                    top_albums = album_res.json().get("topalbums", {}).get("album", [])
                                    
                                    for album in top_albums:
                                        album_name = album.get("name", "")
                                        if not album_name:
                                            continue
                                        
                                        album_key = (similar_artist_name.lower(), album_name.lower())
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
                                            "artist": similar_artist_name,
                                            "playcount": 0,
                                            "image": image_url,
                                            "url": album.get("url", "")
                                        }
                            except Exception as e:
                                logger.debug(f"Failed to fetch albums for {similar_artist_name}: {e}")
                                continue
                except Exception as e:
                    logger.debug(f"Failed to fetch similar artists for {artist_name}: {e}")
                    continue
            
            return list(recommended_albums.values())[:12]
        except Exception as e:
            logger.error(f"Failed to fetch recommended albums: {e}")
            return []
    
    def _get_recommended_tracks(self) -> list:
        """Fetch recommended tracks from Last.fm based on similar artists.
        
        Strategy:
        1. Get user's top artists (to understand their taste)
        2. For each top artist, fetch similar artists
        3. For each similar artist, fetch their top tracks
        4. Return tracks from similar artists (NEW recommendations, not user's own recent tracks)
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
                    "limit": 8,
                    "period": "3month"
                }
            else:
                params = {
                    "method": "chart.getTopArtists",
                    "api_key": self.api_key,
                    "format": "json",
                    "limit": 8
                }
            
            res = self.session.get(self.base_url, params=params, timeout=(5, 10))
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
                
                try:
                    # Get similar artists
                    similar_params = {
                        "method": "artist.getSimilar",
                        "artist": artist_name,
                        "api_key": self.api_key,
                        "format": "json",
                        "limit": 3
                    }
                    
                    similar_res = self.session.get(self.base_url, params=similar_params, timeout=(5, 10))
                    if similar_res.status_code == 200:
                        similar_artists = similar_res.json().get("similarartists", {}).get("artist", [])
                        
                        # Step 3: Get top tracks from each similar artist
                        for similar_artist in similar_artists:
                            similar_artist_name = similar_artist.get("name", "")
                            if not similar_artist_name:
                                continue
                            
                            try:
                                track_params = {
                                    "method": "artist.getTopTracks",
                                    "artist": similar_artist_name,
                                    "api_key": self.api_key,
                                    "format": "json",
                                    "limit": 4
                                }
                                
                                track_res = self.session.get(self.base_url, params=track_params, timeout=(5, 10))
                                if track_res.status_code == 200:
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
                                            "url": track.get("url", "")
                                        }
                            except Exception as e:
                                logger.debug(f"Failed to fetch tracks for {similar_artist_name}: {e}")
                                continue
                except Exception as e:
                    logger.debug(f"Failed to fetch similar artists for {artist_name}: {e}")
                    continue
            
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