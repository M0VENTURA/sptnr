"""Discogs API client module."""
import logging
import difflib
import time
from . import session

logger = logging.getLogger(__name__)

# Rate limiting for Discogs
_DISCOGS_LAST_REQUEST_TIME = 0
_DISCOGS_MIN_INTERVAL = 0.35


def _throttle_discogs():
    """Respect Discogs rate limit (1 request per 0.35 seconds per token)."""
    global _DISCOGS_LAST_REQUEST_TIME
    elapsed = time.time() - _DISCOGS_LAST_REQUEST_TIME
    if elapsed < _DISCOGS_MIN_INTERVAL:
        time.sleep(_DISCOGS_MIN_INTERVAL - elapsed)
    _DISCOGS_LAST_REQUEST_TIME = time.time()


class DiscogsClient:
    """Discogs API wrapper for single detection and metadata."""
    
    def __init__(self, token: str, http_session=None, enabled: bool = True):
        """
        Initialize Discogs client.
        
        Args:
            token: Discogs API token
            http_session: Optional requests.Session (uses shared if not provided)
            enabled: Whether Discogs is enabled
        """
        self.token = token
        self.session = http_session or session
        self.enabled = enabled
        self.base_url = "https://api.discogs.com"
        self.headers = {
            "Authorization": f"Discogs token={token}" if token else "",
            "User-Agent": "sptnr-cli/1.0"
        }
        self._single_cache = {}  # (artist, title, context) -> bool
    
    def is_single(self, title: str, artist: str, album_context: dict | None = None, timeout: tuple[int, int] | int = (5, 10)) -> bool:
        """
        Discogs single detection (best-effort, rate-limit safe).
        
        Strong paths:
          - Explicit 'Single' in release formats
          - EP with first track == A-side AND an official video on the same release
          - Structural fallback: 1–2 track A/B sides where matched title is present
          
        Args:
            title: Track title
            artist: Artist name
            album_context: Optional album context dict (is_live, is_unplugged)
            timeout: Request timeout
            
        Returns:
            True if detected as single
        """
        if not self.enabled or not self.token:
            return False
        
        # Cache lookup
        allow_live_ctx = bool(album_context and (album_context.get("is_live") or album_context.get("is_unplugged")))
        context_key = "live" if allow_live_ctx else "studio"
        cache_key = (artist.lower(), title.lower(), context_key)
        
        if cache_key in self._single_cache:
            return self._single_cache[cache_key]
        
        try:
            # Search for releases
            _throttle_discogs()
            search_url = f"{self.base_url}/database/search"
            params = {"q": f"{artist} {title}", "type": "release", "per_page": 15}
            
            res = self.session.get(search_url, headers=self.headers, params=params, timeout=timeout)
            if res.status_code == 429:
                # Respect rate limit
                retry_after = int(res.headers.get("Retry-After", 60))
                time.sleep(retry_after)
            res.raise_for_status()
            
            results = res.json().get("results", [])
            if not results:
                self._single_cache[cache_key] = False
                return False
            
            # Inspect releases
            nav_title = title.lower()
            for r in results[:10]:
                rid = r.get("id")
                if not rid:
                    continue
                
                # Fetch full release
                _throttle_discogs()
                rel_url = f"{self.base_url}/releases/{rid}"
                rel = self.session.get(rel_url, headers=self.headers, timeout=timeout)
                rel.raise_for_status()
                data = rel.json()
                
                formats = data.get("formats", []) or []
                names = [f.get("name", "").lower() for f in formats]
                descs = [d.lower() for f in formats for d in (f.get("descriptions") or [])]
                
                # Albums out; EPs allowed
                if "album" in names or "album" in descs:
                    continue
                
                is_ep = ("ep" in names) or ("ep" in descs)
                tracks = data.get("tracklist", []) or []
                
                if not tracks or len(tracks) > 7:
                    continue
                
                # Find track match
                best_idx, best_ratio = -1, 0.0
                for i, t in enumerate(tracks):
                    r = difflib.SequenceMatcher(None, t.get("title", "").lower(), nav_title).ratio()
                    if r > best_ratio:
                        best_idx, best_ratio = i, r
                
                if best_ratio < 0.80:
                    continue
                
                mtitle = (tracks[best_idx].get("title", "") or "").lower()
                if ("live" in mtitle or "remix" in mtitle) and not allow_live_ctx:
                    continue
                
                # Strong path 1: explicit Single in formats
                if ("single" in names) or ("single" in descs):
                    self._single_cache[cache_key] = True
                    return True
                
                # Strong path 2: EP + first track match
                if is_ep and best_idx == 0:
                    self._single_cache[cache_key] = True
                    return True
                
                # Strong path 3: Check for music videos in the release
                # If a release has a video for the matched track, it's likely a single
                videos = data.get("videos", []) or []
                for video in videos:
                    video_title = (video.get("title") or "").lower()
                    video_desc = (video.get("description") or "").lower()
                    # Check if video title/desc contains the track title
                    if nav_title in video_title or nav_title in video_desc:
                        # Video for this track found - likely a single
                        logger.debug(f"Found video for '{title}' in Discogs release {rid}")
                        self._single_cache[cache_key] = True
                        return True
                
                # Structural fallback: 1-2 tracks
                if 1 <= len(tracks) <= 2:
                    if best_idx == 0:
                        self._single_cache[cache_key] = True
                        return True
            
            self._single_cache[cache_key] = False
            return False
        
        except Exception as e:
            logger.debug(f"Discogs single check failed for '{title}' by '{artist}': {e}")
            self._single_cache[cache_key] = False
            return False
    
    def has_official_video(self, title: str, artist: str, timeout: tuple[int, int] | int = (5, 10)) -> bool:
        """
        Check if track has an official video on Discogs.
        
        This provides a secondary confidence signal for single detection.
        Note: Video presence alone is not conclusive, as some artists release
        videos for non-singles.
        
        Args:
            title: Track title
            artist: Artist name
            timeout: Request timeout
            
        Returns:
            True if official video found
        """
        if not self.enabled or not self.token:
            return False
        
        try:
            # Search for videos with retry on rate limit
            _throttle_discogs()
            search_url = f"{self.base_url}/database/search"
            params = {"q": f"{artist} {title}", "type": "master", "per_page": 10}
            
            res = self.session.get(search_url, headers=self.headers, params=params, timeout=timeout)
            if res.status_code == 429:
                retry_after = int(res.headers.get("Retry-After", 60))
                time.sleep(retry_after)
                # Retry the request after sleeping
                _throttle_discogs()
                res = self.session.get(search_url, headers=self.headers, params=params, timeout=timeout)
            res.raise_for_status()
            
            results = res.json().get("results", [])
            if not results:
                return False
            
            # Check for video-related releases
            nav_title_lower = title.lower()
            for r in results[:5]:
                master_id = r.get("id")
                if not master_id:
                    continue
                
                # Fetch master release details with retry on rate limit
                _throttle_discogs()
                master_url = f"{self.base_url}/masters/{master_id}"
                master_res = self.session.get(master_url, headers=self.headers, timeout=timeout)
                if master_res.status_code == 429:
                    retry_after = int(master_res.headers.get("Retry-After", 60))
                    time.sleep(retry_after)
                    # Retry the request after sleeping
                    _throttle_discogs()
                    master_res = self.session.get(master_url, headers=self.headers, timeout=timeout)
                master_res.raise_for_status()
                master_data = master_res.json()
                
                # Check videos in the master release
                videos = master_data.get("videos", []) or []
                for video in videos:
                    video_title = (video.get("title") or "").lower()
                    video_desc = (video.get("description") or "").lower()
                    
                    # Check if it's an official video for this track
                    is_official = ("official" in video_title or "official" in video_desc)
                    matches_title = (nav_title_lower in video_title or nav_title_lower in video_desc)
                    
                    if is_official and matches_title:
                        logger.debug(f"Found official video for '{title}' by '{artist}' on Discogs")
                        return True
            
            return False
            
        except Exception as e:
            logger.debug(f"Discogs video check failed for '{title}' by '{artist}': {e}")
            return False
    
    def get_genres(self, title: str, artist: str, timeout: tuple[int, int] | int = (5, 10)) -> list[str]:
        """
        Fetch genres and styles from Discogs API.
        
        Args:
            title: Track title
            artist: Artist name
            timeout: Request timeout
            
        Returns:
            List of genre/style strings
        """
        if not self.enabled or not self.token:
            logger.debug("Discogs genre lookup skipped (disabled or token missing).")
            return []
        
        try:
            _throttle_discogs()
            search_url = f"{self.base_url}/database/search"
            params = {"q": f"{artist} {title}", "type": "release", "per_page": 5}
            
            res = self.session.get(search_url, headers=self.headers, params=params, timeout=timeout)
            res.raise_for_status()
            
            results = res.json().get("results", [])
            genres = []
            for r in results:
                genres.extend(r.get("genre", []))
                genres.extend(r.get("style", []))
            
            return genres
        except Exception as e:
            logger.error(f"Discogs lookup failed for '{title}': {e}")
            return []


# Backward-compatible module functions
_discogs_client = None

def _get_discogs_client(token: str, enabled: bool = True):
    """Get or create singleton Discogs client."""
    global _discogs_client
    if _discogs_client is None:
        _discogs_client = DiscogsClient(token, enabled=enabled)
    return _discogs_client

def is_discogs_single(title: str, artist: str, album_context: dict | None = None, timeout: tuple[int, int] | int = (5, 10), token: str = "", enabled: bool = True) -> bool:
    """Backward-compatible wrapper."""
    client = _get_discogs_client(token, enabled=enabled)
    return client.is_single(title, artist, album_context, timeout)

def get_discogs_genres(title: str, artist: str, token: str = "", enabled: bool = True, timeout: tuple[int, int] | int = (5, 10)) -> list[str]:
    """Backward-compatible wrapper."""
    client = _get_discogs_client(token, enabled=enabled)
    return client.get_genres(title, artist, timeout)

def has_discogs_video(title: str, artist: str, token: str = "", enabled: bool = True, timeout: tuple[int, int] | int = (5, 10)) -> bool:
    """Backward-compatible wrapper for video detection."""
    client = _get_discogs_client(token, enabled=enabled)
    return client.has_official_video(title, artist, timeout)
