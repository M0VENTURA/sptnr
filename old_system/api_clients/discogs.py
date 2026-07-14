"""Discogs API client module."""
import logging
import difflib
import time
import json
import re
import os
import sys
from typing import Optional, Dict, List, Tuple
from . import session as shared_session, timeout_safe_session

# Add parent directory to path to import root-level modules
parent_dir = os.path.dirname(os.path.dirname(__file__))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

from helpers.helpers import clean_discogs_biography
from helpers.helpers import create_retry_session
from helpers.matching_utils import strip_search_parentheses
from discogs_singles_cache import normalize_track_title, get_discogs_cache

# Import centralized logging for visible operational messages
# Use try-except to handle cases where logging_config is not available (e.g., in tests)
try:
    from helpers.logging_config import log_unified, log_info, log_debug
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
_DISCOGS_CIRCUIT_BREAKER_OPEN = False  # Circuit breaker for when Discogs is down
_DISCOGS_CIRCUIT_BREAKER_RESET_TIME = 0  # When to reset the circuit breaker
_DISCOGS_CONSECUTIVE_ERRORS = 0  # Track consecutive errors to trigger circuit breaker
_DISCOGS_RATE_LIMIT_UNTIL = 0.0  # Shared cooldown window after a 429 response


def _build_discogs_session():
    """Create a Discogs-specific session that does not auto-retry HTTP 429."""
    return create_retry_session(
        user_agent="sptnr-cli/1.0 +https://github.com/M0VENTURA/sptnr",
        retries=3,
        backoff=1.0,
        status_forcelist=(500, 502, 503, 504),
    )


def _get_retry_after_seconds(response, default: float = 60.0) -> float:
    """Parse Discogs Retry-After header safely."""
    retry_after_raw = response.headers.get("Retry-After") if response is not None else None
    try:
        retry_after = float(retry_after_raw) if retry_after_raw is not None else float(default)
    except (TypeError, ValueError):
        retry_after = float(default)
    return max(1.0, retry_after)


def _strip_featured_artist(artist: str) -> str:
    """Return canonical primary artist by removing feat./ft./featuring suffixes."""
    if not artist:
        return artist
    primary = re.split(r"\s+(?:feat\.?|featuring|ft\.?)\s+", artist, maxsplit=1, flags=re.IGNORECASE)[0].strip()
    return primary or artist.strip()


def _set_discogs_rate_limit_window(wait_seconds: float) -> None:
    """Record a shared cooldown window so later requests do not hammer Discogs."""
    global _DISCOGS_RATE_LIMIT_UNTIL
    _DISCOGS_RATE_LIMIT_UNTIL = max(_DISCOGS_RATE_LIMIT_UNTIL, time.time() + max(0.0, wait_seconds))


def _sleep_for_discogs_rate_limit(wait_seconds: float, message: str | None = None) -> None:
    """Sleep and publish a shared Discogs rate-limit cooldown window."""
    _set_discogs_rate_limit_window(wait_seconds)
    if message:
        logger.warning(message)
    time.sleep(max(0.0, wait_seconds))


def _handle_discogs_rate_limit_response(response, message: str | None = None) -> float:
    """Handle a 429 response and return the cooldown duration in seconds."""
    wait_seconds = _get_retry_after_seconds(response)
    _sleep_for_discogs_rate_limit(wait_seconds, message or f"Discogs rate limited - waiting {int(wait_seconds)}s")
    return wait_seconds


def _throttle_discogs():
    """Respect Discogs rate limit (1 request per 0.35 seconds per token)."""
    global _DISCOGS_LAST_REQUEST_TIME
    if _DISCOGS_RATE_LIMIT_UNTIL > time.time():
        time.sleep(_DISCOGS_RATE_LIMIT_UNTIL - time.time())
    elapsed = time.time() - _DISCOGS_LAST_REQUEST_TIME
    if elapsed < _DISCOGS_MIN_INTERVAL:
        time.sleep(_DISCOGS_MIN_INTERVAL - elapsed)
    _DISCOGS_LAST_REQUEST_TIME = time.time()


def _check_circuit_breaker():
    """Check if Discogs circuit breaker is open (temporarily disabled due to errors)."""
    global _DISCOGS_CIRCUIT_BREAKER_OPEN, _DISCOGS_CIRCUIT_BREAKER_RESET_TIME
    if _DISCOGS_CIRCUIT_BREAKER_OPEN and time.time() < _DISCOGS_CIRCUIT_BREAKER_RESET_TIME:
        return False  # Circuit is still open
    elif _DISCOGS_CIRCUIT_BREAKER_OPEN and time.time() >= _DISCOGS_CIRCUIT_BREAKER_RESET_TIME:
        # Reset the circuit breaker
        _DISCOGS_CIRCUIT_BREAKER_OPEN = False
        logger.warning("Discogs circuit breaker reset - retrying")
    return True  # Circuit is closed, allow requests


def _record_discogs_error(error_type: str):
    """Record a Discogs error and potentially open circuit breaker."""
    global _DISCOGS_CIRCUIT_BREAKER_OPEN, _DISCOGS_CIRCUIT_BREAKER_RESET_TIME, _DISCOGS_CONSECUTIVE_ERRORS
    
    # Count consecutive errors
    _DISCOGS_CONSECUTIVE_ERRORS += 1
    
    # Open circuit breaker after 5 consecutive server errors (502/503)
    if _DISCOGS_CONSECUTIVE_ERRORS >= 5 and error_type in ["502", "503"]:
        _DISCOGS_CIRCUIT_BREAKER_OPEN = True
        _DISCOGS_CIRCUIT_BREAKER_RESET_TIME = time.time() + 300  # Close in 5 minutes
        logger.error(f"Discogs circuit breaker OPEN (too many {error_type} errors). Will retry in 5 minutes.")


def _clear_discogs_errors():
    """Clear error counter on successful request."""
    global _DISCOGS_CONSECUTIVE_ERRORS
    _DISCOGS_CONSECUTIVE_ERRORS = 0


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
        if http_session is None or http_session is shared_session or http_session is timeout_safe_session:
            self.session = _build_discogs_session()
        else:
            self.session = http_session
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
                    _handle_discogs_rate_limit_response(response)
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
                        _handle_discogs_rate_limit_response(response)
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

    def search_releases(self, query: str, limit: int = 5, timeout: tuple[int, int] | int = (5, 10)) -> list[dict]:
        """Compatibility helper used by folder-grouping fallback code.

        Returns simplified release results with keys: id, title, artist, year, country.
        """
        if not self.enabled or not self.token:
            return []

        query = (query or "").strip()
        if not query:
            return []

        try:
            _throttle_discogs()
            search_url = f"{self.base_url}/database/search"
            params = {
                "q": query,
                "type": "release",
                "per_page": max(1, min(int(limit or 5), 25)),
            }

            def make_search_request():
                response = self.session.get(search_url, headers=self.headers, params=params, timeout=timeout)
                if response.status_code == 429:
                    _handle_discogs_rate_limit_response(response)
                    _throttle_discogs()
                    response = self.session.get(search_url, headers=self.headers, params=params, timeout=timeout)
                response.raise_for_status()
                return response

            response = _retry_on_500(make_search_request, max_retries=2, retry_delay=1.0)
            raw_results = response.json().get("results", []) or []

            normalized = []
            for item in raw_results[: params["per_page"]]:
                full_title = item.get("title", "") or ""
                artist_name = ""
                release_title = full_title
                if " - " in full_title:
                    artist_name, release_title = full_title.split(" - ", 1)

                normalized.append({
                    "id": item.get("id"),
                    "title": release_title or full_title,
                    "artist": artist_name,
                    "year": item.get("year"),
                    "country": item.get("country", "") or "",
                })

            return normalized
        except Exception as e:
            logger.debug(f"Discogs release search failed for query '{query}': {e}")
            return []
    
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
                        _handle_discogs_rate_limit_response(response)
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
        Check if a video is an official or promotional video for a given track.
        
        A video is considered valid if:
        1. The word "official" OR "promo" appears in the video title or description
           (avoiding false positives like "unofficial" by checking word boundaries)
           (includes promotional videos like "DVD, DVD-Video, Promo" format on Discogs)
        2. The track title matches the video title exactly (after cleaning)
           (requires exact match to avoid false positives like "Song" matching "Song II")
        
        Args:
            video: Video dict from Discogs API with 'title' and 'description' keys
            track_title_lower: Track title in lowercase for case-insensitive matching
            
        Returns:
            True if both conditions are met (official or promo video that matches the track)
        """
        video_title = (video.get("title") or "").lower()
        video_desc = (video.get("description") or "").lower()
        
        # Check for "official" or "promo" as whole words to avoid false positives
        # This includes promotional videos like DVD/DVD-Video promos on Discogs
        official_pattern = r'\b(official|promo)\b'
        is_official_or_promo = (
            re.search(official_pattern, video_title) is not None or
            re.search(official_pattern, video_desc) is not None
        )
        
        # Track title matching: Extract the title part from video title before comparing
        # Remove common video suffixes like "official video", "music video", "hd", "promo", etc.
        # Also remove common artist name prefixes like "Artist - Title"
        video_title_cleaned = re.sub(
            r'\s*[\(\[]?(official|music|promo)?\s*(video|music video|mv|hd|4k|lyric video)[\)\]]?\s*$',
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
                r'\s*[\(\[]?(official|music|promo)?\s*(video|music video|mv|hd|4k|lyric video)[\)\]]?\s*',
                '', video_desc, flags=re.IGNORECASE
            ).strip()
            if ' - ' in desc_cleaned:
                parts = desc_cleaned.split(' - ', 1)
                if len(parts) == 2:
                    desc_cleaned = parts[1].strip()
            matches_title = track_title_lower == desc_cleaned.lower()
        
        return is_official_or_promo and matches_title
    
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
                wait_seconds = _handle_discogs_rate_limit_response(
                    response,
                    f"Discogs API rate limited, retrying after {int(_get_retry_after_seconds(response))}s"
                )
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
    
    def _fetch_artist_singles_and_eps(self, artist_id: int, timeout: tuple[int, int] | int = (5, 10)) -> Dict[str, List[str]]:
        """
        Fetch all track titles from artist's Singles and EPs releases via artist releases endpoint.
        
        This directly accesses the authoritative Singles/EPs list for an artist,
        avoiding generic search limitations.
        
        Args:
            artist_id: Discogs artist ID
            timeout: Request timeout
            
        Returns:
            Dict with 'singles' and 'eps' keys, each containing list of track titles (normalized)
        """
        result = {"singles": [], "eps": []}
        
        try:
            cache = get_discogs_cache()
            
            # Fetch all artist releases (Discogs API doesn't support format parameter)
            try:
                _throttle_discogs()
                releases_url = f"{self.base_url}/artists/{artist_id}/releases"
                params = {"per_page": 100}
                
                logger.debug(f"Discogs: GET {releases_url} for artist {artist_id}")
                response = self.session.get(releases_url, headers=self.headers, params=params, timeout=timeout)
                logger.debug(f"Discogs: Artist releases response status={response.status_code}")
                
                if response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", 60))
                    logger.warning(f"Discogs API rate limited, retrying after {retry_after}s")
                    time.sleep(retry_after)
                    _throttle_discogs()
                    response = self.session.get(releases_url, headers=self.headers, params=params, timeout=timeout)
                
                if response.status_code == 401:
                    logger.error(f"Discogs API authentication failed (401): invalid or expired token")
                    logger.debug(f"Discogs token used: {self.token[:20] if self.token else 'None'}{'...' if self.token and len(self.token) > 20 else ''}")
                    return result
                elif response.status_code == 403:
                    logger.error(f"Discogs API access forbidden (403): check token permissions")
                    return result
                
                # Let response.raise_for_status() handle other errors (404, 500, etc.)
                response.raise_for_status()
                
                releases = response.json().get("releases", [])
                logger.debug(f"Discogs: Fetched {len(releases)} total releases for artist {artist_id}")
                log_debug(f"[DISCOGS_RELEASES] Fetched {len(releases)} total releases for artist ID {artist_id}")
                
                # Process each release and filter by format client-side
                singles_found = 0
                eps_found = 0
                skipped_releases = 0
                for release_info in releases:
                    release_id = release_info.get("id")
                    if not release_id:
                        continue
                    
                    release_type = release_info.get("type", "").lower()
                    release_role = release_info.get("role", "").lower()
                    
                    # Skip if not a primary release or wrong type
                    if release_role and "primary" not in release_role:
                        continue
                    
                    try:
                        # Fetch full release details with tracklist and format info
                        _throttle_discogs()
                        rel_url = f"{self.base_url}/releases/{release_id}"
                        rel_response = self.session.get(rel_url, headers=self.headers, timeout=timeout)
                        if rel_response.status_code == 429:
                            _handle_discogs_rate_limit_response(rel_response)
                            _throttle_discogs()
                            rel_response = self.session.get(rel_url, headers=self.headers, timeout=timeout)
                        
                        # Let response.raise_for_status() handle errors
                        rel_response.raise_for_status()
                        
                        release_data = rel_response.json()
                        release_title = release_data.get("title", "")
                        formats = release_data.get("formats", []) or []
                        tracks = release_data.get("tracklist", []) or []
                        
                        log_debug(f"[DISCOGS_RELEASES] Processing release {release_id}: '{release_title}' with {len(formats)} format(s), {len(tracks)} track(s)")
                        
                        # Check if this release is a Single or EP based on format data
                        is_single = False
                        is_ep = False
                        format_details = []
                        
                        for fmt in formats:
                            fmt_name = (fmt.get("name") or "").lower()
                            fmt_descs = fmt.get("descriptions") or []
                            fmt_descs = [d.lower() for d in fmt_descs if d]
                            format_details.append(f"name={fmt_name}, descs={fmt_descs}")
                            
                            if "single" in fmt_name or "single" in " ".join(fmt_descs):
                                is_single = True
                            if "ep" in fmt_name or any("ep" in d for d in fmt_descs if len(d) <= 5):  # Avoid matching words containing "ep"
                                is_ep = True
                        
                        log_debug(f"[DISCOGS_RELEASES] Format details: {format_details} -> is_single={is_single}, is_ep={is_ep}")
                        
                        # Extract and normalize track titles
                        if is_single or is_ep:
                            result_key = "singles" if is_single else "eps"
                            log_debug(f"[DISCOGS_RELEASES] Release is {result_key}: extracting {len(tracks)} tracks")
                            for track in tracks:
                                track_title = track.get("title", "").strip()
                                if track_title:
                                    normalized = normalize_track_title(track_title)
                                    log_debug(f"[DISCOGS_RELEASES]   Track: '{track_title}' -> normalized: '{normalized}'")
                                    if normalized and normalized not in result[result_key]:
                                        result[result_key].append(normalized)
                                        logger.debug(f"Discogs: Added {result_key}: '{normalized}' from '{release_title}'")
                                        log_debug(f"[DISCOGS_RELEASES]   Added to {result_key} list (total: {len(result[result_key])})")
                                        if result_key == "singles":
                                            singles_found += 1
                                        else:
                                            eps_found += 1
                                else:
                                    log_debug(f"[DISCOGS_RELEASES]   Track title empty after strip")
                        else:
                            log_debug(f"[DISCOGS_RELEASES] Skipping release - not single or EP")
                    
                    except Exception as e:
                        logger.debug(f"Failed to fetch release {release_id}: {e}")
                        continue
                
                log_debug(f"[DISCOGS_RELEASES] Summary: {singles_found} singles, {eps_found} EPs, {skipped_releases} skipped")
                logger.debug(f"Discogs: Processed releases - found {singles_found} singles and {eps_found} EPs")
            
            except Exception as e:
                logger.error(f"Failed to fetch releases for artist {artist_id}: {e}")
                import traceback
                logger.debug(f"   Traceback: {traceback.format_exc()}")
            
            log_debug(f"[DISCOGS_RELEASES] Final result: {len(result['singles'])} singles and {len(result['eps'])} EPs")
            log_debug(f"[DISCOGS_RELEASES] Singles: {result['singles'][:5]}{'...' if len(result['singles']) > 5 else ''}")
            log_debug(f"[DISCOGS_RELEASES] EPs: {result['eps'][:5]}{'...' if len(result['eps']) > 5 else ''}")
            logger.debug(f"Discogs: Fetched {len(result['singles'])} singles and {len(result['eps'])} EPs for artist {artist_id}")
            return result
        
        except Exception as e:
            logger.error(f"Failed to fetch artist Singles/EPs: {e}")
            import traceback
            logger.debug(f"   Traceback: {traceback.format_exc()}")
            return result
    
    def get_single_release_year(self, title: str, artist: str, timeout: tuple[int, int] | int = (5, 10)) -> Optional[int]:
        """
        Search Discogs for a single release and return its release year.

        This is used to compare the single's release date against the album's
        original release date to verify the single belongs to the album era.

        Args:
            title: Track title
            artist: Artist name
            timeout: Request timeout

        Returns:
            Release year as int, or None if not found
        """
        if not self.enabled or not self.token:
            return None

        try:
            log_debug(f"[DISCOGS_DATE] Searching single release year for: '{title}' by '{artist}'")
            normalized_title = normalize_track_title(title)
            base_title = strip_search_parentheses(title)
            normalized_base_title = normalize_track_title(base_title) if base_title else ""
            search_title = normalized_base_title if normalized_base_title and normalized_base_title != normalized_title else normalized_title

            search_artist = _strip_featured_artist(artist)

            # Search for single format
            _throttle_discogs()
            search_url = f"{self.base_url}/database/search"
            params = {
                "artist": search_artist,
                "track": search_title,
                "format": "Single",
                "type": "release",
                "per_page": 5,
            }

            max_retries = 2
            retry_delay = 1.0
            for attempt in range(max_retries + 1):
                try:
                    res = self.session.get(search_url, headers=self.headers, params=params, timeout=timeout)
                    if res.status_code == 429:
                        retry_after = _get_retry_after_seconds(res)
                        _handle_discogs_rate_limit_response(res, f"Discogs rate limited - waiting {int(retry_after)}s")
                        _throttle_discogs()
                        res = self.session.get(search_url, headers=self.headers, params=params, timeout=timeout)
                    if res.status_code in [502, 503]:
                        _record_discogs_error(str(res.status_code))
                        if attempt < max_retries:
                            time.sleep(retry_delay)
                            retry_delay *= 2
                            continue
                        return None
                    _clear_discogs_errors()
                    res.raise_for_status()
                    results = res.json().get("results", [])
                    for result in results:
                        result_title = result.get("title", "")
                        if " - " in result_title:
                            _, track_name = result_title.split(" - ", 1)
                        else:
                            track_name = result_title
                        normalized_result = normalize_track_title(track_name)
                        if normalized_result == search_title:
                            year = result.get("year")
                            if year:
                                log_debug(f"[DISCOGS_DATE] Found single year: {year} for '{title}'")
                                return int(year)
                    # Try EP fallback
                    params["format"] = "EP"
                    _throttle_discogs()
                    res = self.session.get(search_url, headers=self.headers, params=params, timeout=timeout)
                    if res.status_code == 429:
                        _handle_discogs_rate_limit_response(res)
                        _throttle_discogs()
                        res = self.session.get(search_url, headers=self.headers, params=params, timeout=timeout)
                    res.raise_for_status()
                    results = res.json().get("results", [])
                    for result in results:
                        result_title = result.get("title", "")
                        if " - " in result_title:
                            _, track_name = result_title.split(" - ", 1)
                        else:
                            track_name = result_title
                        normalized_result = normalize_track_title(track_name)
                        if normalized_result == search_title:
                            year = result.get("year")
                            if year:
                                log_debug(f"[DISCOGS_DATE] Found EP year: {year} for '{title}'")
                                return int(year)
                    return None
                except (TimeoutError, ConnectionError) as e:
                    if attempt < max_retries:
                        time.sleep(retry_delay)
                        retry_delay *= 2
                        continue
                    return None
        except Exception as e:
            log_debug(f"[DISCOGS_DATE] ERROR: {type(e).__name__}: {str(e)}")
            return None

    def is_single(self, title: str, artist: str, album_context: dict | None = None, timeout: tuple[int, int] | int = (5, 10)) -> bool:
        """
        Fast Discogs single detection using Search API with format filtering.
        
        Uses the Discogs /database/search endpoint with format=Single and format=EP filters.
        One API call instead of fetching all artist releases - much faster!
        
        Detection path:
          1. Check in-request cache (for repeated checks in same scan)
          2. Search Discogs: artist + title + format=(Single OR EP)
          3. If found, return True and cache result
          4. If not found, return False and cache result
          
        Args:
            title: Track title
            artist: Artist name
            album_context: Optional album context dict (is_live, is_unplugged, is_special_edition, album_name)
            timeout: Request timeout
            
        Returns:
            True if track found in Discogs' Singles & EPs database
        """
        if not self.enabled or not self.token:
            return False
        
        # Reject single detection for special edition albums via Discogs
        if album_context and album_context.get("is_special_edition"):
            logger.debug(f"Discogs: Skipping single check for '{title}' from special edition album")
            return False
        
        # In-request cache to avoid repeated API calls for same track within a scan session
        if not hasattr(self, "_single_check_cache"):
            self._single_check_cache = {}  # (artist_lower, normalized_title) -> bool
        
        try:
            log_debug(f"[DISCOGS_SINGLE] is_single check: title='{title}', artist='{artist}'")
            normalized_title = normalize_track_title(title)
            log_debug(f"[DISCOGS_SINGLE] Normalized title: '{normalized_title}'")
            
            # Strip version/edition suffixes (e.g. "remastered 2024", "single version") to
            # get the base title used for Discogs API search and in-request cache keying.
            # This ensures "Song (remastered 2024)" and "Song (single version)" both find
            # the original "Song" single on Discogs, and share the same cache entry.
            base_title = strip_search_parentheses(title)
            normalized_base_title = normalize_track_title(base_title) if base_title else ""
            # Fall back to normalized_title when stripping produces an empty or identical result
            base_normalized_title = normalized_base_title if normalized_base_title and normalized_base_title != normalized_title else normalized_title
            
            artist_lower = artist.lower()
            # Key on the base title so different versions of the same song share the cache
            cache_key = (artist_lower, base_normalized_title)
            
            # Check in-request cache
            if cache_key in self._single_check_cache:
                result = self._single_check_cache[cache_key]
                log_debug(f"[DISCOGS_SINGLE] Cache HIT (in-request): {result}")
                logger.debug(f"Discogs: Using cached result for '{title}' by '{artist}': {result}")
                return result
            
            log_debug(f"[DISCOGS_SINGLE] Cache MISS - Searching Discogs for single/EP...")

            # Check persistent DB cache before hitting the API.
            # This avoids repeated Discogs API calls across scan runs for the same track.
            try:
                _db_cache = get_discogs_cache()
                _db_result = _db_cache.get_cached_result(artist_lower, base_normalized_title)
                if _db_result is not None:
                    self._single_check_cache[cache_key] = _db_result
                    log_debug(f"[DISCOGS_SINGLE] Cache HIT (DB persistent): {_db_result}")
                    logger.debug(f"Discogs: Using persistent DB cache for '{title}' by '{artist}': {_db_result}")
                    return _db_result
            except Exception as _db_cache_err:
                log_debug(f"[DISCOGS_SINGLE] DB cache read error (will continue with API): {_db_cache_err}")

            # Search for artist + track as single/ep with optimized API calls
            result = self._search_discogs_for_single(artist, title, base_normalized_title, timeout)
            
            # Cache the result in-memory and in the persistent DB cache.
            self._single_check_cache[cache_key] = result
            try:
                _db_cache = get_discogs_cache()
                _db_cache.save_result(artist_lower, title, result)
            except Exception as _save_err:
                log_debug(f"[DISCOGS_SINGLE] DB cache save error (non-fatal): {_save_err}")

            log_debug(f"[DISCOGS_SINGLE] Search result: {result} - Cached for future checks")
            
            if result:
                logger.debug(f"Discogs: ✓ Found '{title}' by '{artist}' as Single/EP")
            else:
                logger.debug(f"Discogs: ✗ '{title}' by '{artist}' not found as Single/EP")
            
            return result
        
        except Exception as e:
            log_debug(f"[DISCOGS_SINGLE] ERROR: {type(e).__name__}: {str(e)}")
            logger.debug(f"Discogs single check failed for '{title}' by '{artist}': {e}")
            import traceback
            logger.debug(f"   Error traceback: {traceback.format_exc()}")
            log_debug(f"[DISCOGS_SINGLE] Traceback: {traceback.format_exc()}")
            return False
    
    def _search_discogs_for_single(self, artist: str, title: str, normalized_title: str, timeout: tuple[int, int] | int = (5, 10)) -> bool:
        """
        Search Discogs for a track as a Single or EP.
        
        Tries format-specific searches:
        1. format=Single search: artist + normalized_title (to handle punctuation)
        2. format=EP search: artist + normalized_title if Single not found
        
        Note: We use the base normalized_title (with version/edition info stripped) for the
        API search to avoid Discogs returning 0 results for titles like
        "janies got a gun remastered 2024" when only "janies got a gun" exists as a single.
        
        Args:
            artist: Artist name
            title: Track title (original, for logging)
            normalized_title: Base normalized track title (version info stripped) for search and comparison
            timeout: Request timeout
            
        Returns:
            True if track found in Discogs' Singles/EPs
        """
        try:
            # Try searching with format=Single first (most common)
            # Use base normalized_title (version info stripped) to find the original single,
            # e.g. "with arms wide open" matches even when album track is "(remastered 2024)"
            log_debug(f"[DISCOGS_SINGLE] Searching: artist='{artist}' track='{title}' (normalized: '{normalized_title}') format=Single")
            
            results_single = self._discogs_search_with_format(artist, normalized_title, "Single", timeout)
            if self._check_search_results(results_single, normalized_title):
                log_debug(f"[DISCOGS_SINGLE] ✓ Found in Singles results")
                return True
            
            # Try format=EP if no singles found
            log_debug(f"[DISCOGS_SINGLE] No Single found, searching format=EP")
            results_ep = self._discogs_search_with_format(artist, normalized_title, "EP", timeout)
            if self._check_search_results(results_ep, normalized_title):
                log_debug(f"[DISCOGS_SINGLE] ✓ Found in EP results")
                return True
            
            log_debug(f"[DISCOGS_SINGLE] ✗ Not found in Singles or EPs")
            return False
            
        except Exception as e:
            log_debug(f"[DISCOGS_SINGLE] Search error: {e}")
            return False
    
    def _discogs_search_with_format(self, artist: str, title: str, format_type: str, timeout: tuple[int, int] | int = (5, 10)) -> list:
        """
        Search Discogs database with specific format filter.
        
        Implements circuit breaker pattern: if Discogs is returning too many 502/503 errors,
        temporarily disable queries to avoid cascading failures and rate limiting.
        
        Args:
            artist: Artist name
            title: Track title
            format_type: Format to filter by ("Single", "EP", "Album", etc.)
            timeout: Request timeout
            
        Returns:
            List of matching search results
        """
        # Check circuit breaker first
        if not _check_circuit_breaker():
            log_debug(f"[DISCOGS_SINGLE] Circuit breaker OPEN - skipping {format_type} search")
            return []
        
        try:
            _throttle_discogs()
            search_url = f"{self.base_url}/database/search"
            
            # Use the search API with format filter
            # This searches Discogs' database for releases matching artist + title + format
            # Strip featured artists from the artist name so that queries like
            # "dArtagnan feat. Melissa Bonny" become "dArtagnan" - Discogs does
            # not index featuring credits in the artist field.
            search_artist = _strip_featured_artist(artist)
            params = {
                "artist": search_artist,
                "track": title,
                "format": format_type,
                "type": "release",
                "per_page": 10
            }
            
            log_debug(f"[DISCOGS_SINGLE] API request: {format_type} search with artist + track")
            
            # Retry on server errors (502/503) with exponential backoff
            max_retries = 2
            retry_delay = 1.0
            
            for attempt in range(max_retries + 1):
                try:
                    res = self.session.get(search_url, headers=self.headers, params=params, timeout=timeout)
                    
                    # Handle rate limiting (429) - this is temporary, not a server error
                    if res.status_code == 429:
                        retry_after = _get_retry_after_seconds(res)
                        log_debug(f"[DISCOGS_SINGLE] Rate limited, waiting {retry_after:.0f}s")
                        _handle_discogs_rate_limit_response(res, f"Discogs rate limited - waiting {int(retry_after)}s")
                        _throttle_discogs()
                        res = self.session.get(search_url, headers=self.headers, params=params, timeout=timeout)
                    
                    # Handle server errors (502/503) - temporary issue, retry with backoff
                    if res.status_code in [502, 503]:
                        _record_discogs_error(str(res.status_code))
                        if attempt < max_retries:
                            log_debug(f"[DISCOGS_SINGLE] Server error {res.status_code}, retrying in {retry_delay}s...")
                            logger.warning(f"Discogs returned {res.status_code}, retrying in {retry_delay}s...")
                            time.sleep(retry_delay)
                            retry_delay *= 2  # Exponential backoff: 1s, 2s, 4s
                            continue  # Retry
                        else:
                            log_debug(f"[DISCOGS_SINGLE] Server error {res.status_code}, max retries exceeded")
                            return []  # Give up after max retries
                    
                    # Success - clear error counter
                    _clear_discogs_errors()
                    
                    res.raise_for_status()
                    data = res.json()
                    results = data.get("results", [])
                    
                    log_debug(f"[DISCOGS_SINGLE] API returned {len(results)} {format_type} results")
                    return results
                    
                except (TimeoutError, ConnectionError) as e:
                    # Network error - could be transient
                    if attempt < max_retries:
                        log_debug(f"[DISCOGS_SINGLE] Network error: {e}, retrying in {retry_delay}s...")
                        time.sleep(retry_delay)
                        retry_delay *= 2
                        continue
                    else:
                        log_debug(f"[DISCOGS_SINGLE] Network error after {max_retries + 1} attempts: {e}")
                        return []
                        
            return []
            
        except Exception as e:
            log_debug(f"[DISCOGS_SINGLE] Search error for {format_type}: {e}")
            return []
    
    def _check_search_results(self, results: list, normalized_title: str) -> bool:
        """
        Check if any search results match the normalized track title.
        
        Args:
            results: List of search results from Discogs API
            normalized_title: Normalized track title to match
            
        Returns:
            True if a matching result is found
        """
        if not results:
            return False
        
        for result in results:
            result_type = result.get("type", "").lower()
            result_title = result.get("title", "")
            
            # Extract just the track name from "Artist - Title" format
            if " - " in result_title:
                _, track_name = result_title.split(" - ", 1)
            else:
                track_name = result_title
            
            # Normalize for comparison
            normalized_result = normalize_track_title(track_name)
            
            log_debug(f"[DISCOGS_SINGLE] Comparing '{normalized_title}' vs '{normalized_result}'")
            
            if normalized_result == normalized_title:
                log_debug(f"[DISCOGS_SINGLE] ✓ Match found! Result ID: {result.get('id')}")
                return True
        
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
        
        # Check circuit breaker first
        if not _check_circuit_breaker():
            logger.debug("Discogs video check skipped (circuit breaker open)")
            return False
        
        try:
            # Search for videos with retry on rate limit and server errors
            _throttle_discogs()
            search_url = f"{self.base_url}/database/search"
            params = {"q": f"{artist} {title}", "type": "master", "per_page": 10}
            
            max_retries = 1  # Single retry for video checks
            retry_delay = 1.0
            
            for attempt in range(max_retries + 1):
                try:
                    res = self.session.get(search_url, headers=self.headers, params=params, timeout=timeout)
                    
                    if res.status_code == 429:
                        retry_after = _get_retry_after_seconds(res)
                        _handle_discogs_rate_limit_response(res, f"Discogs rate limited in video check, waiting {int(retry_after)}s")
                        _throttle_discogs()
                        res = self.session.get(search_url, headers=self.headers, params=params, timeout=timeout)
                    
                    if res.status_code in [502, 503]:
                        _record_discogs_error(str(res.status_code))
                        if attempt < max_retries:
                            time.sleep(retry_delay)
                            continue
                        else:
                            return False
                    
                    _clear_discogs_errors()
                    res.raise_for_status()
                    results = res.json().get("results", [])
                    break
                except (TimeoutError, ConnectionError):
                    if attempt < max_retries:
                        time.sleep(retry_delay)
                        continue
                    else:
                        return False
            
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
                    _handle_discogs_rate_limit_response(master_res)
                    # Retry the request after sleeping
                    _throttle_discogs()
                    master_res = self.session.get(master_url, headers=self.headers, timeout=timeout)
                
                # Skip on server errors
                if master_res.status_code in [502, 503]:
                    _record_discogs_error(str(master_res.status_code))
                    continue
                    
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
                _handle_discogs_rate_limit_response(res)
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

        if not _check_circuit_breaker():
            logger.debug("Discogs genre lookup skipped: circuit breaker is open")
            return []
        
        try:
            _throttle_discogs()
            search_url = f"{self.base_url}/database/search"
            params = {"q": f"{artist} {title}", "type": "release", "per_page": 5}

            def make_search_request():
                response = self.session.get(search_url, headers=self.headers, params=params, timeout=timeout)
                if response.status_code == 429:
                    _handle_discogs_rate_limit_response(response)
                    _throttle_discogs()
                    response = self.session.get(search_url, headers=self.headers, params=params, timeout=timeout)
                response.raise_for_status()
                return response

            res = _retry_on_500(make_search_request, max_retries=2, retry_delay=1.0)
            _clear_discogs_errors()
            
            results = res.json().get("results", [])
            genres = []
            for r in results:
                genres.extend(r.get("genre", []))
                genres.extend(r.get("style", []))
            
            return genres
        except Exception as e:
            error_text = str(e)
            if "502" in error_text:
                _record_discogs_error("502")
                logger.warning(f"Discogs lookup temporary 502 for '{title}', skipping this track")
            elif "503" in error_text:
                _record_discogs_error("503")
                logger.warning(f"Discogs lookup temporary 503 for '{title}', skipping this track")
            else:
                logger.error(f"Discogs lookup failed for '{title}': {e}")
            return []
    
    def get_release(self, release_id: str, timeout: tuple[int, int] | int = (5, 10)) -> dict | None:
        """
        Fetch full release data for a specific Discogs release by ID.
        
        Args:
            release_id: Discogs release ID
            timeout: Request timeout
            
        Returns:
            Release data dict with tracklist, genres, formats, etc., or None if failed
        """
        if not self.enabled or not self.token or not release_id:
            logger.debug(f"Discogs release lookup skipped (disabled, token missing, or invalid release_id: {release_id})")
            return None
        
        try:
            _throttle_discogs()
            release_url = f"{self.base_url}/releases/{release_id}"
            
            res = self.session.get(release_url, headers=self.headers, timeout=timeout)
            
            # Handle rate limiting
            if res.status_code == 429:
                _handle_discogs_rate_limit_response(res)
                res = self.session.get(release_url, headers=self.headers, timeout=timeout)
            
            res.raise_for_status()
            release_data = res.json()
            
            logger.debug(f"Fetched Discogs release {release_id}")
            return release_data
            
        except Exception as e:
            logger.debug(f"Discogs release lookup failed for release_id {release_id}: {e}")
            return None

    def get_release_genres_by_id(self, release_id: str, timeout: tuple[int, int] | int = (5, 10)) -> list[dict]:
        """
        Fetch genres and styles for a specific Discogs release by ID.
        
        Args:
            release_id: Discogs release ID
            timeout: Request timeout
            
        Returns:
            List of dicts with 'name' (genre/style name) for easy JSON storage
            Example: [{'name': 'Electronic'}, {'name': 'House'}]
        """
        if not self.enabled or not self.token or not release_id:
            logger.debug(f"Discogs release lookup skipped (disabled, token missing, or invalid release_id: {release_id})")
            return []
        
        try:
            _throttle_discogs()
            release_url = f"{self.base_url}/releases/{release_id}"
            
            res = self.session.get(release_url, headers=self.headers, timeout=timeout)
            
            # Handle rate limiting
            if res.status_code == 429:
                _handle_discogs_rate_limit_response(res)
                res = self.session.get(release_url, headers=self.headers, timeout=timeout)
            
            res.raise_for_status()
            release_data = res.json()
            
            genres = []
            
            # Extract genres
            for genre in release_data.get("genres", []):
                genres.append({"name": genre})
            
            # Extract styles
            for style in release_data.get("styles", []):
                genres.append({"name": style})
            
            logger.debug(f"Fetched {len(genres)} genres/styles for Discogs release {release_id}")
            return genres
            
        except Exception as e:
            logger.debug(f"Discogs release lookup failed for release_id {release_id}: {e}")
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
                _handle_discogs_rate_limit_response(res)
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
                _handle_discogs_rate_limit_response(artist_res)
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

    def get_artist_biography_by_id(self, artist_id: str, timeout: tuple[int, int] | int = (5, 10)) -> dict:
        """
        Fetch artist biography/profile from Discogs API using a known artist ID.

        Args:
            artist_id: Discogs artist ID
            timeout: Request timeout

        Returns:
            Dictionary with biography info including 'profile', 'real_name', 'urls', 'images'
        """
        if not self.enabled or not self.token or not artist_id:
            logger.debug("Discogs artist biography by ID lookup skipped (disabled, token missing, or no artist_id).")
            return {}

        try:
            _throttle_discogs()
            artist_url = f"{self.base_url}/artists/{artist_id}"
            artist_res = self.session.get(artist_url, headers=self.headers, timeout=timeout)
            if artist_res.status_code == 429:
                _handle_discogs_rate_limit_response(artist_res)
                _throttle_discogs()
                artist_res = self.session.get(artist_url, headers=self.headers, timeout=timeout)
            artist_res.raise_for_status()

            artist_data = artist_res.json()
            raw_profile = artist_data.get("profile", "")
            cleaned_profile = clean_discogs_biography(raw_profile)

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

            logger.debug(f"Found Discogs biography for artist_id '{artist_id}': {len(bio_info.get('profile', ''))} chars")
            return bio_info

        except Exception as e:
            logger.error(f"Discogs artist biography by ID lookup failed for '{artist_id}': {e}")
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
