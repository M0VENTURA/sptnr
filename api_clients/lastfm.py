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
        """Fetch recommended artists from Last.fm.
        
        If username is set, uses user-specific recommendations.
        Otherwise falls back to chart top artists.
        """
        # Try user-specific recommendations first if username is provided
        if self.username:
            params = {
                "method": "user.getTopArtists",
                "user": self.username,
                "api_key": self.api_key,
                "format": "json",
                "limit": 20,
                "period": "overall"  # overall, 7day, 1month, 3month, 6month, 12month
            }
        else:
            # Fall back to global chart
            params = {
                "method": "chart.getTopArtists",
                "api_key": self.api_key,
                "format": "json",
                "limit": 20
            }
        
        try:
            res = self.session.get(self.base_url, params=params, timeout=(5, 10))  # (connect_timeout, read_timeout)
            res.raise_for_status()
            artists = []
            
            # Different response structure for user vs chart
            if self.username:
                artist_list = res.json().get("topartists", {}).get("artist", [])
            else:
                artist_list = res.json().get("artists", {}).get("artist", [])
            
            for item in artist_list:
                # Try to get artist image
                image_url = ""
                if isinstance(item.get("image"), list) and len(item["image"]) > 0:
                    for img in reversed(item["image"]):
                        if img.get("size") == "extralarge" or img.get("#text"):
                            image_url = img.get("#text", "")
                            break
                
                artists.append({
                    "name": item.get("name", ""),
                    "listeners": item.get("listeners", 0),
                    "playcount": item.get("playcount", 0),
                    "image": image_url,
                    "url": item.get("url", "")
                })
            return artists
        except Exception as e:
            logger.error(f"Failed to fetch recommended artists: {e}")
            return []
    
    def _get_recommended_albums(self) -> list:
        """Fetch recommended albums from Last.fm top tracks by artist."""
        params = {
            "method": "geo.getTopArtists",
            "country": "US",
            "api_key": self.api_key,
            "format": "json",
            "limit": 10
        }
        
        try:
            res = self.session.get(self.base_url, params=params, timeout=(5, 10))  # (connect_timeout, read_timeout)
            res.raise_for_status()
            albums = []
            
            # Get top artists, then get their top albums
            for artist_item in res.json().get("topartists", {}).get("artist", [])[:5]:
                artist_name = artist_item.get("name", "")
                artist_url = artist_item.get("url", "")
                
                # Get top tracks for this artist (which will show albums)
                track_params = {
                    "method": "artist.getTopTracks",
                    "artist": artist_name,
                    "api_key": self.api_key,
                    "format": "json",
                    "limit": 3
                }
                
                track_res = self.session.get(self.base_url, params=track_params, timeout=(5, 10))  # (connect_timeout, read_timeout)
                if track_res.status_code == 200:
                    for track in track_res.json().get("toptracks", {}).get("track", []):
                        album_info = track.get("album", {})
                        if album_info and album_info.get("title"):
                            image_url = ""
                            if isinstance(album_info.get("image"), list):
                                for img in reversed(album_info["image"]):
                                    if img.get("#text"):
                                        image_url = img.get("#text", "")
                                        break
                            
                            albums.append({
                                "name": album_info.get("title", ""),
                                "artist": artist_name,
                                "playcount": track.get("playcount", 0),
                                "image": image_url,
                                "url": album_info.get("url", "")
                            })
            
            return albums[:12]  # Return up to 12 albums
        except Exception as e:
            logger.error(f"Failed to fetch recommended albums: {e}")
            return []
    
    def _get_recommended_tracks(self) -> list:
        """Fetch recommended tracks from Last.fm.
        
        If username is set, uses user-specific top tracks.
        Otherwise falls back to chart top tracks.
        """
        # Try user-specific recommendations first if username is provided
        if self.username:
            params = {
                "method": "user.getRecentTracks",
                "user": self.username,
                "api_key": self.api_key,
                "format": "json",
                "limit": 20
            }
        else:
            # Fall back to global chart
            params = {
                "method": "chart.getTopTracks",
                "api_key": self.api_key,
                "format": "json",
                "limit": 20
            }
        
        try:
            res = self.session.get(self.base_url, params=params, timeout=(5, 10))  # (connect_timeout, read_timeout)
            res.raise_for_status()
            tracks = []
            
            # Different response structure for user vs chart
            if self.username:
                track_list = res.json().get("recenttracks", {}).get("track", [])
            else:
                track_list = res.json().get("tracks", {}).get("track", [])
            
            for item in track_list:
                image_url = ""
                if isinstance(item.get("image"), list):
                    for img in reversed(item["image"]):
                        if img.get("#text"):
                            image_url = img.get("#text", "")
                            break
                
                # Handle different artist formats (user.getRecentTracks vs chart)
                if isinstance(item.get("artist"), dict):
                    artist_name = item.get("artist", {}).get("name", "Unknown")
                else:
                    artist_name = item.get("artist", "Unknown")
                
                tracks.append({
                    "name": item.get("name", ""),
                    "artist": artist_name,
                    "playcount": item.get("playcount", 0),
                    "image": image_url,
                    "url": item.get("url", "")
                })
            return tracks
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
def get_lastfm_recommendations(api_key: str, username: str = None) -> dict:
    """Fetch Last.fm recommendations."""
    client = LastFmClient(api_key, username=username)
    return client.get_recommendations()