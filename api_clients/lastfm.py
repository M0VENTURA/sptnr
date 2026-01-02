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
            Dict with 'track_play' and other metadata
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
            return {"track_play": track_play}
        except Exception as e:
            logger.error(f"Last.fm fetch failed for '{title}' by '{artist}': {e}")
            return {"track_play": 0}


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
