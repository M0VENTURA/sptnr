"""Discogs API client module."""
import logging
import difflib
import time
import json
from typing import Optional, Dict, List, Tuple
from . import session

# Import centralized logging for visible operational messages
# Use try-except to handle cases where logging_config is not available (e.g., in tests)
try:
    from logging_config import log_unified, log_info, log_debug
    _HAVE_CENTRALIZED_LOGGING = True
except (ImportError, PermissionError):
    # Fallback to standard logger if centralized logging not available
    _HAVE_CENTRALIZED_LOGGING = False
    def log_unified(msg, level=logging.INFO):
        logging.getLogger(__name__).log(level, msg)
    def log_info(msg, level=logging.INFO):
        logging.getLogger(__name__).log(level, msg)
    def log_debug(msg, level=logging.DEBUG):
        logging.getLogger(__name__).log(level, msg)

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


def _retry_on_500(func, max_retries: int = 3, retry_delay: float = 2.0):
    """
    Retry a function on 500 errors with exponential backoff.
    
    Args:
        func: Function to execute (should return requests.Response)
        max_retries: Maximum number of retry attempts
        retry_delay: Initial delay between retries (doubles each time)
        
    Returns:
        Function result on success
        
    Raises:
        Exception: If all retries fail
    """
    last_exception = None
    current_delay = retry_delay
    
    for attempt in range(max_retries + 1):
        try:
            result = func()
            # Check for 500-level errors
            if hasattr(result, 'status_code') and 500 <= result.status_code < 600:
                raise Exception(f"Server error: {result.status_code}")
            return result
        except Exception as e:
            last_exception = e
            if attempt < max_retries:
                logger.warning(f"Discogs API attempt {attempt + 1} failed with {e}, retrying in {current_delay}s...")
                time.sleep(current_delay)
                current_delay *= 2  # Exponential backoff
            else:
                logger.error(f"Discogs API: all {max_retries + 1} attempts failed")
    
    raise last_exception


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
            "User-Agent": "sptnr-cli/1.0 +https://github.com/M0VENTURA/sptnr"
        }
        self._single_cache = {}  # (artist, title, context) -> bool
        self._metadata_cache = {}  # (artist, title) -> metadata dict
    
    def get_comprehensive_metadata(
        self,
        title: str,
        artist: str,
        duration: Optional[float] = None,
        timeout: tuple = (5, 10)
    ) -> Optional[Dict]:
        """
        Get comprehensive Discogs metadata for database storage.
        
        This method is designed to fetch and return all required metadata fields
        for storage in the database according to the problem statement requirements.
        
        Returns dict with keys:
        - discogs_release_id: str or None
        - discogs_master_id: str or None
        - discogs_formats: List[str] (JSON-serializable)
        - discogs_format_descriptions: List[str] (JSON-serializable)
        - discogs_is_single: bool
        - discogs_track_titles: List[str] (JSON-serializable)
        - discogs_release_year: int or None
        - discogs_label: str or None
        - discogs_country: str or None
        
        Args:
            title: Track title
            artist: Artist name
            duration: Optional track duration in seconds
            timeout: Request timeout
            
        Returns:
            Metadata dict or None if lookup failed
        """
        # Check cache first
        cache_key = (artist.lower(), title.lower())
        if cache_key in self._metadata_cache:
            return self._metadata_cache[cache_key]
        
        if not self.enabled or not self.token:
            return None
        
        try:
            # Search for releases - try without format filter first
            _throttle_discogs()
            search_url = f"{self.base_url}/database/search"
            base_params = {
                "q": f"{artist} {title}",
                "type": "release",
                "per_page": 5
            }
            
            def make_search_request(search_params):
                response = self.session.get(search_url, headers=self.headers, params=search_params, timeout=timeout)
                if response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", 60))
                    time.sleep(retry_after)
                    _throttle_discogs()
                    response = self.session.get(search_url, headers=self.headers, params=search_params, timeout=timeout)
                response.raise_for_status()
                return response
            
            search_response = _retry_on_500(lambda: make_search_request(base_params), max_retries=2, retry_delay=1.0)
            results = search_response.json().get("results", [])
            
            # If no results, try with format filter as fallback
            if not results:
                _throttle_discogs()
                fallback_params = {**base_params, "format": "Single, EP"}
                search_response = _retry_on_500(lambda: make_search_request(fallback_params), max_retries=2, retry_delay=1.0)
                results = search_response.json().get("results", [])
            
            if not results:
                logger.debug(f"No Discogs results for '{title}' by '{artist}'")
                return None
            
            # Get first matching release
            for result in results[:3]:
                release_id = result.get('id')
                if not release_id:
                    continue
                
                # Fetch full release data
                _throttle_discogs()
                release_url = f"{self.base_url}/releases/{release_id}"
                
                def make_release_request():
                    response = self.session.get(release_url, headers=self.headers, timeout=timeout)
                    if response.status_code == 429:
                        retry_after = int(response.headers.get("Retry-After", 60))
                        time.sleep(retry_after)
                        _throttle_discogs()
                        response = self.session.get(release_url, headers=self.headers, timeout=timeout)
                    response.raise_for_status()
                    return response
                
                release_response = _retry_on_500(make_release_request, max_retries=2, retry_delay=1.0)
                release_data = release_response.json()
                
                # Extract metadata
                formats = release_data.get('formats', []) or []
                format_names = [f.get('name', '') for f in formats if f.get('name')]
                format_descriptions = []
                for fmt in formats:
                    descs = fmt.get('descriptions') or []
                    format_descriptions.extend([d for d in descs if d])
                
                tracklist = release_data.get('tracklist', []) or []
                track_titles = [t.get('title', '') for t in tracklist if t.get('title')]
                
                master_id = release_data.get('master_id')
                release_year = release_data.get('year')
                labels = release_data.get('labels', []) or []
                label = labels[0].get('name', '') if labels else None
                country = release_data.get('country')
                
                # Determine if single
                is_single = self._determine_if_single(
                    format_names,
                    format_descriptions,
                    len(tracklist),
                    master_id,
                    timeout
                )
                
                metadata = {
                    'discogs_release_id': str(release_id),
                    'discogs_master_id': str(master_id) if master_id else None,
                    'discogs_formats': format_names,
                    'discogs_format_descriptions': format_descriptions,
                    'discogs_is_single': is_single,
                    'discogs_track_titles': track_titles,
                    'discogs_release_year': release_year,
                    'discogs_label': label,
                    'discogs_country': country
                }
                
                # Cache the result
                self._metadata_cache[cache_key] = metadata
                
                return metadata
            
            # No matching release found
            return None
            
        except Exception as e:
            logger.error(f"Discogs metadata lookup failed for '{title}' by '{artist}': {e}")
            return None
    
    def _determine_if_single(
        self,
        format_names: List[str],
        format_descriptions: List[str],
        track_count: int,
        master_id: Optional[int],
        timeout: tuple
    ) -> bool:
        """
        Determine if release is a single based on Discogs data.
        
        Implements comprehensive single determination rules from problem statement.
        """
        # Rule 1: Format contains "Single"
        names_lower = [n.lower() for n in format_names]
        descs_lower = [d.lower() for d in format_descriptions]
        
        single_format_patterns = ['single', '7"', '12" single', 'cd single']
        for pattern in single_format_patterns:
            for name in names_lower:
                if pattern in name:
                    return True
        
        # Rule 2: Description contains "Single" or "Maxi-Single" (but not EP)
        for desc in descs_lower:
            if ('single' in desc or 'maxi-single' in desc) and 'ep' not in desc:
                return True
        
        # Rule 3: 1-2 tracks
        if 1 <= track_count <= 2:
            return True
        
        # Rule 4: Promo with 1-2 tracks
        is_promo = any('promo' in f for f in names_lower + descs_lower)
        if is_promo and 1 <= track_count <= 2:
            return True
        
        # Rule 5: Check master release
        if master_id:
            try:
                _throttle_discogs()
                master_url = f"{self.base_url}/masters/{master_id}"
                
                def make_master_request():
                    response = self.session.get(master_url, headers=self.headers, timeout=timeout)
                    if response.status_code == 429:
                        retry_after = int(response.headers.get("Retry-After", 60))
                        time.sleep(retry_after)
                        _throttle_discogs()
                        response = self.session.get(master_url, headers=self.headers, timeout=timeout)
                    response.raise_for_status()
                    return response
                
                master_response = _retry_on_500(make_master_request, max_retries=2, retry_delay=1.0)
                master_data = master_response.json()
                
                master_formats = master_data.get('formats', []) or []
                master_names = [f.get('name', '').lower() for f in master_formats if f.get('name')]
                master_descs = []
                for fmt in master_formats:
                    descs = fmt.get('descriptions') or []
                    master_descs.extend([d.lower() for d in descs if d])
                
                # Check master for single
                for pattern in single_format_patterns:
                    for name in master_names:
                        if pattern in name:
                            return True
                
                for desc in master_descs:
                    if ('single' in desc or 'maxi-single' in desc) and 'ep' not in desc:
                        return True
                        
            except Exception as e:
                logger.debug(f"Failed to check master release {master_id}: {e}")
        
        return False
    
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
            # Try search without format filter first (to catch singles with non-standard format tags)
            _throttle_discogs()
            search_url = f"{self.base_url}/database/search"
            params = {
                "q": f"{artist} {title}", 
                "type": "release", 
                "per_page": 15
            }
            
            # Helper function for making search requests with rate limit handling
            def make_discogs_search_request(search_params):
                """Make a Discogs search request with rate limit handling."""
                response = self.session.get(search_url, headers=self.headers, params=search_params, timeout=timeout)
                if response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", 60))
                    time.sleep(retry_after)
                    _throttle_discogs()
                    response = self.session.get(search_url, headers=self.headers, params=search_params, timeout=timeout)
                response.raise_for_status()
                return response.json().get("results", [])
            
            results = make_discogs_search_request(params)
            
            # If no results without filter, try with format filter as fallback
            if not results:
                _throttle_discogs()
                fallback_params = {**params, "format": "Single, EP"}
                results = make_discogs_search_request(fallback_params)
            
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
                # If a release has an official video for the matched track, it's likely a single
                videos = data.get("videos", []) or []
                if videos:
                    log_info(f"   Discogs: Checking {len(videos)} video(s) in release {rid} for '{title}'")
                for video in videos:
                    video_title = (video.get("title") or "").lower()
                    video_desc = (video.get("description") or "").lower()
                    # Check if it's an official video for this track
                    is_official = ("official" in video_title or "official" in video_desc)
                    matches_title = (nav_title in video_title or nav_title in video_desc)
                    
                    if is_official and matches_title:
                        # Official video for this track found - likely a single
                        log_unified(f"   ✓ Discogs confirms single via official music video in release {rid}: {title}")
                        log_info(f"   Discogs result: Official music video found in release for '{title}' (video: {video.get('title', 'N/A')})")
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
    
    def get_artist_biography(self, artist: str, timeout: tuple[int, int] | int = (5, 10)) -> dict:
        """
        Fetch artist biography/profile from Discogs API.
        
        Args:
            artist: Artist name
            timeout: Request timeout
            
        Returns:
            Dictionary with biography info including 'profile', 'real_name', 'urls', 'images'
        """
        if not self.enabled or not self.token:
            logger.debug("Discogs artist biography lookup skipped (disabled or token missing).")
            return {}
        
        try:
            # Search for artist
            _throttle_discogs()
            search_url = f"{self.base_url}/database/search"
            params = {"q": artist, "type": "artist", "per_page": 5}
            
            res = self.session.get(search_url, headers=self.headers, params=params, timeout=timeout)
            if res.status_code == 429:
                retry_after = int(res.headers.get("Retry-After", 60))
                time.sleep(retry_after)
                res = self.session.get(search_url, headers=self.headers, params=params, timeout=timeout)
            res.raise_for_status()
            
            results = res.json().get("results", [])
            if not results:
                logger.debug(f"No Discogs artist found for: {artist}")
                return {}
            
            # Get the best match (first result, Discogs search is pretty accurate)
            artist_url = results[0].get("resource_url")
            if not artist_url:
                return {}
            
            # Fetch full artist details
            _throttle_discogs()
            artist_res = self.session.get(artist_url, headers=self.headers, timeout=timeout)
            if artist_res.status_code == 429:
                retry_after = int(artist_res.headers.get("Retry-After", 60))
                time.sleep(retry_after)
                artist_res = self.session.get(artist_url, headers=self.headers, timeout=timeout)
            artist_res.raise_for_status()
            
            artist_data = artist_res.json()
            
            # Extract relevant biography info
            bio_info = {
                "profile": artist_data.get("profile", ""),
                "real_name": artist_data.get("realname", ""),
                "urls": artist_data.get("urls", []),
                "images": artist_data.get("images", []),
                "members": artist_data.get("members", []),
                "name_variations": artist_data.get("namevariations", []),
                "discogs_id": artist_data.get("id"),
                "discogs_url": artist_data.get("uri", "")
            }
            
            logger.debug(f"Found Discogs biography for '{artist}': {len(bio_info.get('profile', ''))} chars")
            return bio_info
            
        except Exception as e:
            logger.error(f"Discogs artist biography lookup failed for '{artist}': {e}")
            return {}


# Backward-compatible module functions
_discogs_client = None

def _get_discogs_client(token: str, enabled: bool = True):
    """Get or create singleton Discogs client."""
    global _discogs_client
    if _discogs_client is None or _discogs_client.token != token:
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

def get_discogs_artist_biography(artist: str, token: str = "", enabled: bool = True, timeout: tuple[int, int] | int = (5, 10)) -> dict:
    """Backward-compatible wrapper for artist biography lookup."""
    client = _get_discogs_client(token, enabled=enabled)
    return client.get_artist_biography(artist, timeout)
