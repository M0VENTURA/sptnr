"""Discogs API client module."""
import logging
import difflib
import time
import json
import re
from typing import Optional, Dict, List, Tuple
from . import session
from helpers import clean_discogs_biography
from discogs_singles_cache import normalize_track_title, get_discogs_cache

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
    
    def __init__(self, token: str, http_session=None, enabled: bool = True, db_path: str = "/database/sptnr.db"):
        """
        Initialize Discogs client.
        
        Args:
            token: Discogs API token
            http_session: Optional requests.Session (uses shared if not provided)
            enabled: Whether Discogs is enabled
            db_path: Path to database for cache storage (default: /database/sptnr.db)
        """
        self.token = token
        self.session = http_session or session
        self.enabled = enabled
        self.db_path = db_path
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
        
        # Rule 3: Check master release
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
    
    def _is_official_video_for_track(self, video: dict, track_title_lower: str) -> bool:
        """
        Check if a video is an official video for a given track.
        
        A video is considered official if:
        1. The word "official" appears in the video title or description
           (avoiding false positives like "unofficial" by checking word boundaries)
        2. The track title matches the video title exactly (after cleaning)
           (requires exact match to avoid false positives like "Song" matching "Song II")
        
        Args:
            video: Video dict from Discogs API with 'title' and 'description' keys
            track_title_lower: Track title in lowercase for case-insensitive matching
            
        Returns:
            True if both conditions are met (official video that matches the track)
        """
        video_title = (video.get("title") or "").lower()
        video_desc = (video.get("description") or "").lower()
        
        # Check for "official" as a whole word to avoid matching "unofficial"
        # Use word boundary checking with common separators
        official_pattern = r'\bofficial\b'
        is_official = (
            re.search(official_pattern, video_title) is not None or
            re.search(official_pattern, video_desc) is not None
        )
        
        # Track title matching: Extract the title part from video title before comparing
        # Remove common video suffixes like "official video", "music video", "hd", etc.
        # Also remove common artist name prefixes like "Artist - Title"
        video_title_cleaned = re.sub(
            r'\s*[\(\[]?(official|music)?\s*(video|music video|mv|hd|4k|lyric video)[\)\]]?\s*$',
            '', video_title, flags=re.IGNORECASE
        ).strip()
        
        # Remove "artist - " prefix if present (common in video titles)
        # This handles cases like "Coldplay - Life in Technicolor"
        if ' - ' in video_title_cleaned:
            parts = video_title_cleaned.split(' - ', 1)
            if len(parts) == 2:
                # Use the part after the dash as the title
                video_title_cleaned = parts[1].strip()
        
        # Require exact match after cleaning to avoid false positives
        # This prevents "Life in Technicolor" from matching "Life in Technicolor II"
        # Ensure lowercase comparison (video_title is already lowercased, but explicit for clarity)
        matches_title = track_title_lower == video_title_cleaned.lower()
        
        # Also check description with exact matching
        if not matches_title and video_desc:
            desc_cleaned = re.sub(
                r'\s*[\(\[]?(official|music)?\s*(video|music video|mv|hd|4k|lyric video)[\)\]]?\s*',
                '', video_desc, flags=re.IGNORECASE
            ).strip()
            if ' - ' in desc_cleaned:
                parts = desc_cleaned.split(' - ', 1)
                if len(parts) == 2:
                    desc_cleaned = parts[1].strip()
            matches_title = track_title_lower == desc_cleaned.lower()
        
        return is_official and matches_title
    
    def _get_artist_id(self, artist: str, timeout: tuple[int, int] | int = (5, 10)) -> Optional[int]:
        """
        Get Discogs artist ID by searching for the artist.
        
        Args:
            artist: Artist name
            timeout: Request timeout
            
        Returns:
            Discogs artist ID or None if not found
        """
        try:
            log_debug(f"[DISCOGS_ARTIST_ID] Starting artist ID lookup for: {artist}")
            _throttle_discogs()
            search_url = f"{self.base_url}/database/search"
            params = {
                "q": artist,
                "type": "artist",
                "per_page": 5
            }
            
            logger.debug(f"Discogs: GET {search_url} with params={params}")
            log_debug(f"[DISCOGS_ARTIST_ID] Sending request: GET {search_url}")
            response = self.session.get(search_url, headers=self.headers, params=params, timeout=timeout)
            logger.debug(f"Discogs: Artist search response status={response.status_code}")
            log_debug(f"[DISCOGS_ARTIST_ID] Response status: {response.status_code}")
            
            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", 60))
                logger.warning(f"Discogs API rate limited, retrying after {retry_after}s")
                time.sleep(retry_after)
                _throttle_discogs()
                response = self.session.get(search_url, headers=self.headers, params=params, timeout=timeout)
            
            if response.status_code == 401:
                logger.error(f"Discogs API authentication failed (401): invalid or expired token")
                logger.debug(f"Discogs token used: {self.token[:20] if self.token else 'None'}{'...' if self.token and len(self.token) > 20 else ''}")
                return None
            elif response.status_code == 403:
                logger.error(f"Discogs API access forbidden (403): check token permissions")
                return None
            
            # Let response.raise_for_status() handle other errors (404, 500, etc.)
            response.raise_for_status()
            
            results = response.json().get("results", [])
            logger.debug(f"Discogs: Found {len(results)} artist results for '{artist}'")
            log_debug(f"[DISCOGS_ARTIST_ID] Found {len(results)} results for '{artist}'")
            if results:
                # Return the ID of the first artist match
                first_result = results[0]
                artist_id = first_result.get("id")
                artist_name = first_result.get("title", "Unknown")
                log_debug(f"[DISCOGS_ARTIST_ID] Using first match: ID={artist_id}, name={artist_name}")
                return artist_id
            log_debug(f"[DISCOGS_ARTIST_ID] No artist results found for '{artist}'")
            return None
        
        except Exception as e:
            logger.error(f"Failed to get artist ID for '{artist}': {e}")
            log_debug(f"[DISCOGS_ARTIST_ID] ERROR: {type(e).__name__}: {str(e)}")
            import traceback
            logger.debug(f"   Traceback: {traceback.format_exc()}")
            log_debug(f"[DISCOGS_ARTIST_ID] Traceback: {traceback.format_exc()}")
            return None
    
    def _fetch_artist_singles_and_eps(self, artist_name: str, timeout: tuple[int, int] | int = (5, 10)) -> Dict[str, List[str]]:
        """
        Fetch all track titles from artist's Singles and EPs releases via database search endpoint.
        
        Uses the Discogs database search endpoint with format filtering (Single, EP)
        which is more efficient than fetching all artist releases and filtering client-side.
        
        Args:
            artist_name: Artist name for search
            timeout: Request timeout
            
        Returns:
            Dict with 'singles' and 'eps' keys, each containing list of track titles (normalized)
        """
        result = {"singles": [], "eps": []}
        
        try:
            cache = get_discogs_cache(db_path=self.db_path)
            
            # Use database search endpoint with format filtering
            # This is more efficient than fetching all artist releases
            format_to_key = {"Single": "singles", "EP": "eps"}
            for format_type in ["Single", "EP"]:
                result_key = format_to_key[format_type]
                try:
                    _throttle_discogs()
                    search_url = f"{self.base_url}/database/search"
                    params = {
                        "artist": artist_name,
                        "format": format_type,
                        "type": "release",
                        "per_page": 100
                    }
                    
                    logger.debug(f"Discogs: GET {search_url} with params={params}")
                    response = self.session.get(search_url, headers=self.headers, params=params, timeout=timeout)
                    logger.debug(f"Discogs: Search response for {format_type}s status={response.status_code}")
                    
                    if response.status_code == 429:
                        retry_after = int(response.headers.get("Retry-After", 60))
                        logger.warning(f"Discogs API rate limited, retrying after {retry_after}s")
                        time.sleep(retry_after)
                        _throttle_discogs()
                        response = self.session.get(search_url, headers=self.headers, params=params, timeout=timeout)
                    
                    if response.status_code == 401:
                        logger.error(f"Discogs API authentication failed (401): invalid or expired token")
                        logger.debug(f"Discogs token used: {self.token[:20] if self.token else 'None'}{'...' if self.token and len(self.token) > 20 else ''}")
                        continue
                    elif response.status_code == 403:
                        logger.error(f"Discogs API access forbidden (403): check token permissions")
                        continue
                    
                    # Let response.raise_for_status() handle other errors (404, 500, etc.)
                    response.raise_for_status()
                    
                    releases = response.json().get("results", [])
                    logger.debug(f"Discogs: Found {len(releases)} {format_type} releases for artist '{artist_name}'")
                    log_debug(f"[DISCOGS_RELEASES] Found {len(releases)} {format_type} releases for artist '{artist_name}'")
                    
                    # Process each release to get track titles
                    tracks_found = 0
                    for release_info in releases:
                        release_id = release_info.get("id")
                        if not release_id:
                            continue
                        
                        try:
                            # Fetch full release details to get tracklist
                            _throttle_discogs()
                            rel_url = f"{self.base_url}/releases/{release_id}"
                            rel_response = self.session.get(rel_url, headers=self.headers, timeout=timeout)
                            if rel_response.status_code == 429:
                                retry_after = int(rel_response.headers.get("Retry-After", 60))
                                time.sleep(retry_after)
                                _throttle_discogs()
                                rel_response = self.session.get(rel_url, headers=self.headers, timeout=timeout)
                            
                            # Let response.raise_for_status() handle errors
                            rel_response.raise_for_status()
                            
                            release_data = rel_response.json()
                            release_title = release_data.get("title", "")
                            tracks = release_data.get("tracklist", []) or []
                            
                            log_debug(f"[DISCOGS_RELEASES] Processing {format_type} release {release_id}: '{release_title}' with {len(tracks)} track(s)")
                            
                            # Extract and normalize track titles
                            for track in tracks:
                                track_title = track.get("title", "").strip()
                                if track_title:
                                    normalized = normalize_track_title(track_title)
                                    log_debug(f"[DISCOGS_RELEASES]   Track: '{track_title}' -> normalized: '{normalized}'")
                                    if normalized and normalized not in result[result_key]:
                                        result[result_key].append(normalized)
                                        logger.debug(f"Discogs: Added {result_key}: '{normalized}' from '{release_title}'")
                                        log_debug(f"[DISCOGS_RELEASES]   Added to {result_key} list (total: {len(result[result_key])})")
                                        tracks_found += 1
                                else:
                                    log_debug(f"[DISCOGS_RELEASES]   Track title empty after strip")
                        
                        except Exception as e:
                            logger.debug(f"Failed to fetch release {release_id}: {e}")
                            continue
                    
                    logger.debug(f"Discogs: Processed {len(releases)} {format_type} releases - found {tracks_found} track titles")
                    log_debug(f"[DISCOGS_RELEASES] Summary for {format_type}: {tracks_found} track titles from {len(releases)} releases")
                
                except Exception as e:
                    logger.error(f"Failed to fetch {format_type} releases for artist '{artist_name}': {e}")
                    import traceback
                    logger.debug(f"   Traceback: {traceback.format_exc()}")
            
            log_debug(f"[DISCOGS_RELEASES] Final result: {len(result['singles'])} singles and {len(result['eps'])} EPs")
            log_debug(f"[DISCOGS_RELEASES] Singles: {result['singles'][:5]}{'...' if len(result['singles']) > 5 else ''}")
            log_debug(f"[DISCOGS_RELEASES] EPs: {result['eps'][:5]}{'...' if len(result['eps']) > 5 else ''}")
            logger.debug(f"Discogs: Fetched {len(result['singles'])} singles and {len(result['eps'])} EPs for artist '{artist_name}'")
            return result
        
        except Exception as e:
            logger.error(f"Failed to fetch artist Singles/EPs: {e}")
            import traceback
            logger.debug(f"   Traceback: {traceback.format_exc()}")
            return result
    
    def is_single(self, title: str, artist: str, album_context: dict | None = None, timeout: tuple[int, int] | int = (5, 10)) -> bool:
        """
        Discogs single detection using database search with format filtering (Singles & EPs).
        
        Uses Discogs database search endpoint with format=Single and format=EP filters,
        with persistent cache to avoid repeated API calls for same artist.
        
        Detection path:
          1. Check persistent cache for artist's single/EP tracks
          2. If cache hit, use normalized track title comparison
          3. If cache miss, fetch artist releases from Discogs using format filters and populate cache
          4. Compare normalized track titles against cached singles/EPs
          5. Return True if track found in artist's Singles & EPs releases
          
        Args:
            title: Track title
            artist: Artist name
            album_context: Optional album context dict (is_live, is_unplugged, is_special_edition, album_name)
            timeout: Request timeout
            
        Returns:
            True if track found in artist's Singles & EPs releases
        """
        if not self.enabled or not self.token:
            return False
        
        # Reject single detection for special edition albums via Discogs
        if album_context and album_context.get("is_special_edition"):
            logger.debug(f"Discogs: Skipping single check for '{title}' from special edition album")
            return False
        
        # Local in-request cache to prevent repeated API calls for same artist within single scan
        if not hasattr(self, "_artist_singles_cache"):
            self._artist_singles_cache = {}  # artist_name -> {"singles": [...], "eps": [...]}
        
        try:
            log_debug(f"[DISCOGS_SINGLE] is_single check: title='{title}', artist='{artist}'")
            cache = get_discogs_cache(db_path=self.db_path)
            normalized_title = normalize_track_title(title)
            log_debug(f"[DISCOGS_SINGLE] Normalized title: '{normalized_title}'")
            
            # Check local request cache first
            artist_lower = artist.lower()
            log_debug(f"[DISCOGS_SINGLE] Checking caches for artist '{artist}' (lower: '{artist_lower}')")
            if artist_lower not in self._artist_singles_cache:
                # Check persistent cache for this artist's tracks
                log_debug(f"[DISCOGS_SINGLE] Checking persistent cache for '{artist}'")
                cached_singles = cache.get_cached_titles(artist)
                
                if cached_singles:
                    # Cache hit: use cached track list (convert set to list)
                    logger.debug(f"Discogs: Using cached singles list for artist '{artist}' ({len(cached_singles)} tracks)")
                    log_debug(f"[DISCOGS_SINGLE] CACHE HIT: {len(cached_singles)} cached tracks for '{artist}'")
                    log_debug(f"[DISCOGS_SINGLE] Cached tracks: {list(cached_singles)[:5]}{'...' if len(cached_singles) > 5 else ''}")
                    self._artist_singles_cache[artist_lower] = {"singles": list(cached_singles), "eps": []}
                else:
                    # Cache miss: fetch from Discogs and populate cache
                    log_debug(f"[DISCOGS_SINGLE] CACHE MISS: No cached data for '{artist}', fetching from Discogs API")
                    logger.debug(f"Discogs: Cache miss for '{artist}', fetching artist releases from Discogs API")
                    
                    # Fetch singles and EPs using database search with format filtering
                    log_debug(f"[DISCOGS_SINGLE] Fetching singles/EPs from API for artist '{artist}'")
                    artist_releases = self._fetch_artist_singles_and_eps(artist, timeout)
                    
                    # Store in local cache for this request
                    log_debug(f"[DISCOGS_SINGLE] Caching fetched releases locally: {len(artist_releases['singles'])} singles, {len(artist_releases['eps'])} EPs")
                    self._artist_singles_cache[artist_lower] = artist_releases
                    
                    # Populate persistent cache with track titles
                    if artist_releases["singles"] or artist_releases["eps"]:
                        all_tracks = artist_releases["singles"] + artist_releases["eps"]
                        log_debug(f"[DISCOGS_SINGLE] Populating persistent cache with {len(all_tracks)} total tracks")
                        cache.add_to_cache(artist, all_tracks)
                        logger.debug(f"Discogs: Populated cache for '{artist}' with {len(all_tracks)} track titles ({len(artist_releases['singles'])} singles, {len(artist_releases['eps'])} EPs)")
                    else:
                        log_debug(f"[DISCOGS_SINGLE] No singles or EPs found for '{artist}' from API")
                        logger.debug(f"Discogs: No singles or EPs found for artist '{artist}'")
            
            # Check if normalized title matches any cached single or EP track
            artist_cache = self._artist_singles_cache.get(artist_lower, {"singles": [], "eps": []})
            all_cached_titles = artist_cache["singles"] + artist_cache["eps"]
            
            log_debug(f"[DISCOGS_SINGLE] Checking against {len(all_cached_titles)} cached tracks")
            logger.debug(f"Discogs: Checking if '{normalized_title}' matches any of {len(all_cached_titles)} cached tracks for '{artist}'")
            
            if normalized_title in all_cached_titles:
                log_debug(f"[DISCOGS_SINGLE] ✓ MATCH FOUND: '{normalized_title}' matches cached track")
                logger.debug(f"Discogs: ✓ Found '{title}' in artist '{artist}' Singles/EPs")
                return True
            
            log_debug(f"[DISCOGS_SINGLE] ✗ NO MATCH: checking normalized '{normalized_title}' against:")
            log_debug(f"[DISCOGS_SINGLE]   Singles ({len(artist_cache['singles'])}): {artist_cache['singles'][:10]}")
            log_debug(f"[DISCOGS_SINGLE]   EPs ({len(artist_cache['eps'])}): {artist_cache['eps'][:10]}")
            logger.debug(f"Discogs: ✗ '{title}' not found in artist '{artist}' Singles/EPs (searched {len(all_cached_titles)} tracks)")
            return False
        
        except Exception as e:
            log_debug(f"[DISCOGS_SINGLE] ERROR: {type(e).__name__}: {str(e)}")
            logger.debug(f"Discogs single check failed for '{title}' by '{artist}': {e}")
            import traceback
            logger.debug(f"   Error traceback: {traceback.format_exc()}")
            log_debug(f"[DISCOGS_SINGLE] Traceback: {traceback.format_exc()}")
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
                    if self._is_official_video_for_track(video, nav_title_lower):
                        logger.debug(f"Found official video for '{title}' by '{artist}' on Discogs")
                        return True
            
            return False
            
        except Exception as e:
            logger.debug(f"Discogs video check failed for '{title}' by '{artist}': {e}")
            return False
    
    def get_artist_id(self, artist: str, timeout: tuple[int, int] | int = (5, 10)) -> Optional[str]:
        """
        Search for an artist on Discogs and get their artist ID.
        
        Args:
            artist: Artist name
            timeout: Request timeout
            
        Returns:
            Discogs artist ID (as string) or None if not found
        """
        if not self.enabled or not self.token:
            return None
        
        try:
            _throttle_discogs()
            search_url = f"{self.base_url}/database/search"
            params = {"q": artist, "type": "artist", "per_page": 5}
            
            res = self.session.get(search_url, headers=self.headers, params=params, timeout=timeout)
            if res.status_code == 429:
                retry_after = int(res.headers.get("Retry-After", 60))
                time.sleep(retry_after)
                _throttle_discogs()
                res = self.session.get(search_url, headers=self.headers, params=params, timeout=timeout)
            res.raise_for_status()
            
            results = res.json().get("results", [])
            if not results:
                logger.debug(f"No Discogs artist found for '{artist}'")
                return None
            
            # Return the first result's ID
            artist_id = results[0].get("id")
            if artist_id:
                logger.debug(f"Found Discogs artist ID for '{artist}': {artist_id}")
                return str(artist_id)
            
            return None
            
        except Exception as e:
            logger.debug(f"Discogs artist ID lookup failed for '{artist}': {e}")
            return None
    
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
            
            # Extract and clean biography profile
            raw_profile = artist_data.get("profile", "")
            cleaned_profile = clean_discogs_biography(raw_profile)
            
            # Extract relevant biography info
            bio_info = {
                "profile": cleaned_profile,
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
