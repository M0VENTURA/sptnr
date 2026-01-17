"""MusicBrainz API client module."""
import logging
import difflib
import time
import json
import os
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from . import session

logger = logging.getLogger(__name__)

# Simple MBID cache to avoid repeated lookups
_mbid_cache = {}
_CACHE_FILE = "/tmp/mbid_cache.json" if os.path.exists("/tmp") else "mbid_cache.json"


class MusicBrainzClient:
    """MusicBrainz API wrapper for single detection and metadata."""
    
    def __init__(self, http_session=None, enabled: bool = True):
        """
        Initialize MusicBrainz client.
        
        Args:
            http_session: Optional requests.Session (uses shared if not provided)
            enabled: Whether MusicBrainz is enabled
        """
        # Track if a custom session was provided (don't override its retry config)
        custom_session_provided = http_session is not None
        self.session = http_session or session
        self.enabled = enabled
        self.base_url = "https://musicbrainz.org/ws/2/"
        self.headers = {"User-Agent": "sptnr-cli/2.1 (support@example.com)"}
        # Only setup retry strategy if using default session (not a pre-configured one)
        if not custom_session_provided:
            self._setup_retry_strategy()
        self._load_cache()
    
    def _load_cache(self):
        """Load MBID cache from file if it exists."""
        global _mbid_cache
        if os.path.exists(_CACHE_FILE):
            try:
                with open(_CACHE_FILE, 'r') as f:
                    _mbid_cache = json.load(f)
                logger.debug(f"Loaded MBID cache with {len(_mbid_cache)} entries")
            except Exception as e:
                logger.debug(f"Failed to load MBID cache: {e}")
                _mbid_cache = {}
    
    def _save_cache(self):
        """Save MBID cache to file."""
        global _mbid_cache
        try:
            with open(_CACHE_FILE, 'w') as f:
                json.dump(_mbid_cache, f)
        except Exception as e:
            logger.debug(f"Failed to save MBID cache: {e}")
    
    def _get_cache_key(self, title: str, artist: str) -> str:
        """Generate cache key from title and artist."""
        return f"{artist.lower()} / {title.lower()}"
    
    def _setup_retry_strategy(self):
        """Configure retry strategy with exponential backoff for connection failures."""
        # Define what to retry on: connection errors, timeouts, and 429/503/504 errors
        retry_strategy = Retry(
            total=3,  # Total number of retries
            backoff_factor=0.5,  # Exponential backoff: 0.5s, 1s, 2s
            status_forcelist=[429, 503, 504],  # Retry on these HTTP status codes
            allowed_methods=["HEAD", "GET", "OPTIONS"]  # Only retry safe methods
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        
        # Apply to both http and https
        if hasattr(self.session, 'mount'):
            self.session.mount("http://", adapter)
            self.session.mount("https://", adapter)
    
    def is_single(self, title: str, artist: str) -> bool:
        """
        Query MusicBrainz release-group by title+artist and check primary-type=Single.
        
        Args:
            title: Track title
            artist: Artist name
            
        Returns:
            True if release-group type is Single
        """
        if not self.enabled:
            return False
        
        max_retries = 3
        retry_delay = 1.0
        for attempt in range(max_retries):
            try:
                # Add rate limiting delay before each request to avoid server issues
                time.sleep(1.0)
                
                query = f'{title} AND artist:{artist} AND primarytype:Single'
                params = {
                    "query": query,
                    "fmt": "json",
                    "limit": 5
                }
                # Only log first attempt at debug level to reduce noise
                if attempt == 0:
                    logger.debug(f"MusicBrainz is_single request: {self.base_url}release-group/ params={params}")
                    
                res = self.session.get(
                    f"{self.base_url}release-group/",
                    params=params,
                    headers=self.headers,
                    timeout=(5, 10)  # (connect_timeout, read_timeout)
                )
                # Only log response on debug level to reduce noise
                logger.debug(f"MusicBrainz is_single response: status={res.status_code}")
                res.raise_for_status()
                rgs = res.json().get("release-groups", [])
                return any((rg.get("primary-type") or "").lower() == "single" for rg in rgs)
            except (requests.exceptions.Timeout, requests.exceptions.SSLError, requests.exceptions.ConnectionError) as e:
                # Log SSL/connection/timeout errors at appropriate levels to reduce noise
                error_type = type(e).__name__
                if attempt < max_retries - 1:
                    # Log retries at debug level only
                    logger.debug(f"MusicBrainz is_single attempt {attempt+1}/{max_retries} failed for '{title}' by '{artist}': {error_type}, retrying in {retry_delay}s...")
                    time.sleep(retry_delay)
                    retry_delay *= 2
                else:
                    # Only log final failure at info level (not error) to reduce alarm
                    logger.info(f"MusicBrainz is_single unavailable for '{title}' by '{artist}' after {max_retries} attempts: {error_type}")
                    return False
            except Exception as e:
                logger.warning(f"MusicBrainz is_single unexpected error for '{title}' by '{artist}': {e}")
                return False
    
    def get_genres(self, title: str, artist: str) -> list[str]:
        """
        Fetch tags/genres from MusicBrainz with explicit includes on recordings.
        
        Strategy:
          1) Search recording with inc=tags+artist-credits+releases
          2) Use recording-level tags if present
          3) If no recording tags, try tags on first associated release
          
        Args:
            title: Track title
            artist: Artist name
            
        Returns:
            List of genre/tag names
        """
        if not self.enabled:
            return []
        
        import time
        max_retries = 3
        retry_delay = 1.0
        
        for attempt in range(max_retries):
            try:
                # Add rate limiting delay before each request to avoid server issues
                time.sleep(1.0)
                
                # Step 1: search recording with richer includes
                query = f'{title} AND artist:{artist}'
                rec_params = {
                    "query": query,
                    "fmt": "json",
                    "limit": 3,
                    "inc": "tags+artist-credits+releases",
                }
                r = self.session.get(f"{self.base_url}recording/", params=rec_params, headers=self.headers, timeout=(3, 5))  # (connect_timeout, read_timeout)
                r.raise_for_status()
                recs = r.json().get("recordings", []) or []
                if not recs:
                    return []
                
                # Prefer the top match
                rec = recs[0]
                
                # 2) use recording-level tags if present
                tags = rec.get("tags") or []
                tag_names = [t.get("name", "") for t in tags if t.get("name")]
                if tag_names:
                    return tag_names
                
                # 3) fallback: pull tags from the first release if any
                releases = rec.get("releases") or []
                if releases:
                    rel_id = releases[0].get("id")
                    if rel_id:
                        rel_params = {"fmt": "json", "inc": "tags"}
                        rr = self.session.get(f"{self.base_url}release/{rel_id}", params=rel_params, headers=self.headers, timeout=(3, 5))  # (connect_timeout, read_timeout)
                        rr.raise_for_status()
                        rel_tags = rr.json().get("tags", []) or []
                        return [t.get("name", "") for t in rel_tags if t.get("name")]
                return []
            except (requests.exceptions.Timeout, requests.exceptions.SSLError, requests.exceptions.ConnectionError) as e:
                if attempt < max_retries - 1:
                    logger.debug(f"MusicBrainz genres lookup attempt {attempt + 1} failed for '{title}' by '{artist}': {e}, retrying...")
                    time.sleep(retry_delay)
                    retry_delay *= 2  # Exponential backoff
                else:
                    logger.warning(f"MusicBrainz genres lookup failed for '{title}' by '{artist}' after {max_retries} retries: {e}")
                    return []
            except requests.exceptions.RequestException as e:
                logger.warning(f"MusicBrainz genres lookup request error for '{title}' by '{artist}': {e}")
                return []
            except Exception as e:
                logger.warning(f"MusicBrainz genres lookup failed for '{title}' by '{artist}': {e}")
                return []
        
        return []
    
    def get_suggested_mbid(self, title: str, artist: str, limit: int = 5) -> tuple[str, float]:
        """
        Search MusicBrainz recordings and compute (mbid, confidence).
        
        Confidence:
          - Title similarity (SequenceMatcher)
          - +0.15 bonus if associated release-group primary-type == 'Single'
          
        Uses caching to avoid repeated lookups.
          
        Args:
            title: Track title
            artist: Artist name
            limit: Number of results to check
            
        Returns:
            Tuple of (mbid, confidence_score)
        """
        if not self.enabled:
            return "", 0.0
        
        # Check cache first
        cache_key = self._get_cache_key(title, artist)
        global _mbid_cache
        if cache_key in _mbid_cache:
            cached = _mbid_cache[cache_key]
            logger.debug(f"MBID cache hit for '{title}' by '{artist}': {cached[0]} (confidence: {cached[1]})")
            return tuple(cached)
        
        try:
            # Add rate limiting delay before each request to avoid server issues
            time.sleep(1.0)
            
            # 1) Find recordings (with releases included for second hop)
            query = f'{title} AND artist:{artist}'
            rec_params = {
                "query": query,
                "fmt": "json",
                "limit": limit,
                "inc": "releases+artist-credits",
            }
            r = self.session.get(f"{self.base_url}recording/", params=rec_params, headers=self.headers, timeout=(5, 10))  # (connect_timeout, read_timeout)
            r.raise_for_status()
            recordings = r.json().get("recordings", []) or []
            if not recordings:
                _mbid_cache[cache_key] = ("", 0.0)
                self._save_cache()
                return "", 0.0
            
            best_mbid = ""
            best_score = 0.0
            nav_title = (title or "").lower()
            
            for rec in recordings:
                rec_mbid = rec.get("id", "")
                rec_title = (rec.get("title") or "").lower()
                title_sim = difflib.SequenceMatcher(None, nav_title, rec_title).ratio()
                
                # Default: no bonus
                single_bonus = 0.0
                
                # 2) If we have at least one release, second hop to get primary-type reliably
                releases = rec.get("releases") or []
                if releases:
                    rel_id = releases[0].get("id")
                    if rel_id:
                        rel_params = {"fmt": "json", "inc": "release-groups"}
                        try:
                            # Add rate limiting delay before each release lookup
                            time.sleep(1.0)
                            rr = self.session.get(f"{self.base_url}release/{rel_id}", params=rel_params, headers=self.headers, timeout=(5, 10))  # (connect_timeout, read_timeout)
                            if rr.ok:
                                rel_json = rr.json()
                                rg = rel_json.get("release-group") or {}
                                primary_type = (rg.get("primary-type") or "").lower()
                                if primary_type == "single":
                                    single_bonus = 0.15
                        except requests.exceptions.Timeout:
                            # Skip release lookup if timeout, still use recording match
                            logger.debug(f"MusicBrainz timeout fetching release {rel_id}")
                        except Exception as e:
                            # Log but continue with next recording
                            logger.debug(f"MusicBrainz release lookup failed for {rel_id}: {e}")
                
                confidence = min(1.0, title_sim + single_bonus)
                if confidence > best_score:
                    best_score = confidence
                    best_mbid = rec_mbid
            
            # Cache the result
            result = (best_mbid, round(best_score, 3))
            _mbid_cache[cache_key] = result
            self._save_cache()
            
            return result
        except requests.exceptions.Timeout:
            logger.debug(f"MusicBrainz timeout looking up MBID for '{title}' by '{artist}'")
            return "", 0.0
        except Exception as e:
            logger.debug(f"MusicBrainz suggested MBID lookup failed for '{title}' by '{artist}': {e}")
            return "", 0.0


# Backward-compatible module functions
_musicbrainz_client = None

def _get_musicbrainz_client(enabled: bool = True):
    """Get or create singleton MusicBrainz client."""
    global _musicbrainz_client
    if _musicbrainz_client is None:
        _musicbrainz_client = MusicBrainzClient(enabled=enabled)
    return _musicbrainz_client

def is_musicbrainz_single(title: str, artist: str, enabled: bool = True) -> bool:
    """Backward-compatible wrapper."""
    client = _get_musicbrainz_client(enabled)
    return client.is_single(title, artist)

def get_musicbrainz_genres(title: str, artist: str, enabled: bool = True) -> list[str]:
    """Backward-compatible wrapper."""
    client = _get_musicbrainz_client(enabled)
    return client.get_genres(title, artist)

def get_suggested_mbid(title: str, artist: str, limit: int = 5, enabled: bool = True) -> tuple[str, float]:
    """Backward-compatible wrapper."""
    client = _get_musicbrainz_client(enabled)
    return client.get_suggested_mbid(title, artist, limit)
