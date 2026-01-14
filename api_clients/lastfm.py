"""Last.fm API client module."""
import logging
from . import session

logger = logging.getLogger(__name__)


class LastFmClient:
    """Last.fm API wrapper for track info and listening stats."""
    
    def __init__(self, api_key: str, http_session=None):
        """
        Initialize Last.fm client.
        
        Args:
            api_key: Last.fm API key
            http_session: Optional requests.Session (uses shared if not provided)
        """
        self.api_key = api_key
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
        
        params = {
            "method": "track.getInfo",
            "artist": artist,
            "track": title,
            "api_key": self.api_key,
            "format": "json"
        }
        
        try:
            res = self.session.get(self.base_url, params=params, timeout=10)
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
        """Fetch recommended artists from Last.fm."""
        params = {
            "method": "geo.getTopArtists",
            "country": "US",  # Or get from user preferences
            "api_key": self.api_key,
            "format": "json",
            "limit": 20
        }
        
        try:
            res = self.session.get(self.base_url, params=params, timeout=10)
            res.raise_for_status()
            artists = []
            for item in res.json().get("topartists", {}).get("artist", []):
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
            res = self.session.get(self.base_url, params=params, timeout=10)
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
                
                track_res = self.session.get(self.base_url, params=track_params, timeout=10)
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
        """Fetch recommended tracks from Last.fm."""
        params = {
            "method": "geo.getTopTracks",
            "country": "US",
            "api_key": self.api_key,
            "format": "json",
            "limit": 20
        }
        
        try:
            res = self.session.get(self.base_url, params=params, timeout=10)
            res.raise_for_status()
            tracks = []
            for item in res.json().get("tracks", {}).get("track", []):
                image_url = ""
                if isinstance(item.get("image"), list):
                    for img in reversed(item["image"]):
                        if img.get("#text"):
                            image_url = img.get("#text", "")
                            break
                
                tracks.append({
                    "name": item.get("name", ""),
                    "artist": item.get("artist", {}).get("name", "Unknown"),
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
def get_lastfm_recommendations(api_key: str) -> dict:
    """Fetch Last.fm recommendations."""
    client = _get_lastfm_client(api_key)
    return client.get_recommendations()