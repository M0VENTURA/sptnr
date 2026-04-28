"""Apple Music API client module for artwork and metadata."""
import logging
import time
from . import session

logger = logging.getLogger(__name__)

# Rate limiting for Apple Music API
_APPLE_MUSIC_LAST_REQUEST_TIME = 0
_APPLE_MUSIC_MIN_INTERVAL = 0.1  # 10 requests per second max


def _throttle_apple_music():
    """Respect Apple Music API rate limit."""
    global _APPLE_MUSIC_LAST_REQUEST_TIME
    elapsed = time.time() - _APPLE_MUSIC_LAST_REQUEST_TIME
    if elapsed < _APPLE_MUSIC_MIN_INTERVAL:
        time.sleep(_APPLE_MUSIC_MIN_INTERVAL - elapsed)
    _APPLE_MUSIC_LAST_REQUEST_TIME = time.time()


class AppleMusicClient:
    """Apple Music API wrapper for artwork and metadata."""
    
    def __init__(self, http_session=None, enabled: bool = True):
        """
        Initialize Apple Music client.
        
        Note: Apple Music API requires a developer token. However, we can use
        the iTunes Search API which is public and doesn't require authentication.
        
        Args:
            http_session: Optional requests.Session (uses shared if not provided)
            enabled: Whether Apple Music is enabled
        """
        self.session = http_session or session
        self.enabled = enabled
        # Using iTunes Search API (public, no auth required)
        self.base_url = "https://itunes.apple.com/search"
        self.headers = {"User-Agent": "sptnr-cli/1.0"}
    
    def search_artist(self, artist: str, limit: int = 5, timeout: tuple[int, int] | int = (5, 10)) -> list[dict]:
        """
        Search for artist on Apple Music/iTunes.
        
        Args:
            artist: Artist name
            limit: Number of results to return
            timeout: Request timeout
            
        Returns:
            List of artist result dictionaries
        """
        if not self.enabled:
            return []
        
        try:
            _throttle_apple_music()
            params = {
                "term": artist,
                "entity": "allArtist",
                "limit": limit
            }
            
            res = self.session.get(self.base_url, params=params, headers=self.headers, timeout=timeout)
            res.raise_for_status()
            
            data = res.json()
            results = data.get("results", [])
            
            # Filter to only artist results
            artists = [r for r in results if r.get("wrapperType") == "artist"]
            
            return artists
            
        except Exception as e:
            logger.error(f"Apple Music artist search failed for '{artist}': {e}")
            return []
    
    def search_track(self, title: str, artist: str, limit: int = 10, timeout: tuple[int, int] | int = (5, 10)) -> list[dict]:
        """
        Search for track on Apple Music/iTunes.
        
        Args:
            title: Track title
            artist: Artist name
            limit: Number of results to return
            timeout: Request timeout
            
        Returns:
            List of track result dictionaries
        """
        if not self.enabled:
            return []
        
        try:
            _throttle_apple_music()
            params = {
                "term": f"{artist} {title}",
                "entity": "song",
                "limit": limit
            }
            
            res = self.session.get(self.base_url, params=params, headers=self.headers, timeout=timeout)
            res.raise_for_status()
            
            data = res.json()
            results = data.get("results", [])
            
            return results
            
        except Exception as e:
            logger.error(f"Apple Music track search failed for '{title}' by '{artist}': {e}")
            return []
    
    def search_album(self, album: str, artist: str, limit: int = 10, timeout: tuple[int, int] | int = (5, 10)) -> list[dict]:
        """
        Search for album on Apple Music/iTunes.
        
        Args:
            album: Album name
            artist: Artist name
            limit: Number of results to return
            timeout: Request timeout
            
        Returns:
            List of album result dictionaries
        """
        if not self.enabled:
            return []
        
        try:
            _throttle_apple_music()
            params = {
                "term": f"{artist} {album}",
                "entity": "album",
                "limit": limit
            }
            
            res = self.session.get(self.base_url, params=params, headers=self.headers, timeout=timeout)
            res.raise_for_status()
            
            data = res.json()
            results = data.get("results", [])
            
            return results
            
        except Exception as e:
            logger.error(f"Apple Music album search failed for '{album}' by '{artist}': {e}")
            return []
    
    def get_artist_artwork(self, artist: str, size: int = 500, timeout: tuple[int, int] | int = (5, 10)) -> str:
        """
        Get artist artwork URL from Apple Music.
        
        Args:
            artist: Artist name
            size: Image size (100, 600, etc.)
            timeout: Request timeout
            
        Returns:
            URL to artist artwork image, or empty string if not found
        """
        if not self.enabled:
            return ""
        
        try:
            results = self.search_artist(artist, limit=1, timeout=timeout)
            if not results:
                return ""
            
            # Get artwork URL and resize
            artwork_url = results[0].get("artworkUrl100", "")
            if artwork_url:
                # Replace size in URL (e.g., 100x100bb -> 500x500bb)
                artwork_url = artwork_url.replace("100x100bb", f"{size}x{size}bb")
            
            return artwork_url
            
        except Exception as e:
            logger.error(f"Failed to get Apple Music artist artwork for '{artist}': {e}")
            return ""
    
    def get_track_artwork(self, title: str, artist: str, size: int = 600, timeout: tuple[int, int] | int = (5, 10)) -> str:
        """
        Get track/album artwork URL from Apple Music.
        
        Args:
            title: Track title
            artist: Artist name
            size: Image size (100, 600, etc.)
            timeout: Request timeout
            
        Returns:
            URL to track artwork image, or empty string if not found
        """
        if not self.enabled:
            return ""
        
        try:
            results = self.search_track(title, artist, limit=1, timeout=timeout)
            if not results:
                return ""
            
            # Get artwork URL and resize
            artwork_url = results[0].get("artworkUrl100", "")
            if artwork_url:
                # Replace size in URL (e.g., 100x100bb -> 600x600bb)
                artwork_url = artwork_url.replace("100x100bb", f"{size}x{size}bb")
            
            return artwork_url
            
        except Exception as e:
            logger.error(f"Failed to get Apple Music track artwork for '{title}' by '{artist}': {e}")
            return ""
    
    def get_album_artwork(self, album: str, artist: str, size: int = 600, timeout: tuple[int, int] | int = (5, 10)) -> str:
        """
        Get album artwork URL from Apple Music.
        
        Args:
            album: Album name
            artist: Artist name
            size: Image size (100, 600, etc.)
            timeout: Request timeout
            
        Returns:
            URL to album artwork image, or empty string if not found
        """
        if not self.enabled:
            return ""
        
        try:
            results = self.search_album(album, artist, limit=1, timeout=timeout)
            if not results:
                return ""
            
            # Get artwork URL and resize
            artwork_url = results[0].get("artworkUrl100", "")
            if artwork_url:
                # Replace size in URL (e.g., 100x100bb -> 600x600bb)
                artwork_url = artwork_url.replace("100x100bb", f"{size}x{size}bb")
            
            return artwork_url
            
        except Exception as e:
            logger.error(f"Failed to get Apple Music album artwork for '{album}' by '{artist}': {e}")
            return ""


# Backward-compatible module functions
_apple_music_client = None

def _get_apple_music_client(enabled: bool = True):
    """Get or create singleton Apple Music client."""
    global _apple_music_client
    if _apple_music_client is None:
        _apple_music_client = AppleMusicClient(enabled=enabled)
    return _apple_music_client

def get_artist_artwork(artist: str, size: int = 500, enabled: bool = True, timeout: tuple[int, int] | int = (5, 10)) -> str:
    """Get artist artwork URL from Apple Music."""
    client = _get_apple_music_client(enabled=enabled)
    return client.get_artist_artwork(artist, size, timeout)

def get_track_artwork(title: str, artist: str, size: int = 600, enabled: bool = True, timeout: tuple[int, int] | int = (5, 10)) -> str:
    """Get track artwork URL from Apple Music."""
    client = _get_apple_music_client(enabled=enabled)
    return client.get_track_artwork(title, artist, size, timeout)

def get_album_artwork(album: str, artist: str, size: int = 600, enabled: bool = True, timeout: tuple[int, int] | int = (5, 10)) -> str:
    """Get album artwork URL from Apple Music."""
    client = _get_apple_music_client(enabled=enabled)
    return client.get_album_artwork(album, artist, size, timeout)
