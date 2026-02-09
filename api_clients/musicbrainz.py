"""MusicBrainz API client module."""
import logging
import difflib
import time
import json
import os
import re
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from . import session

# Import SSLAdapter from helpers to avoid duplication
import sys
# Add parent directory to path to import helpers
parent_dir = os.path.dirname(os.path.dirname(__file__))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)
from helpers import SSLAdapter

logger = logging.getLogger(__name__)

# Read version from VERSION file
def _get_version():
    """Read version from VERSION file."""
    try:
        # Try to locate VERSION file relative to this module
        version_file = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'VERSION')
        with open(version_file, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except (FileNotFoundError, IOError, PermissionError, UnicodeDecodeError) as e:
        logger.debug(f"Could not read VERSION file, using fallback version: {e}")
        return "2.0.0-alpha"  # Fallback version

_VERSION = _get_version()

# MusicBrainz API User-Agent (complies with https://musicbrainz.org/doc/MusicBrainz_API)
# Format: AppName/Version ( contact-info )
_USER_AGENT = f"sptnr/{_VERSION} ( https://github.com/M0VENTURA/sptnr )"

# Import rate limiter
try:
    from api_rate_limiter import get_rate_limiter
    _rate_limiter = get_rate_limiter()
except ImportError:
    logger.warning("Rate limiter not available for MusicBrainz")
    _rate_limiter = None

# Import Roman numeral and punctuation patterns from matching_utils
try:
    from matching_utils import ROMAN_NUMERAL_PATTERN, PUNCTUATION_SUFFIX_PATTERN
except ImportError:
    # Fallback if matching_utils not available
    ROMAN_NUMERAL_PATTERN = r'\s+(I{1,3}|IV|V|VI{0,3}|IX|X{1,3}|XI{0,3}|XIV|XV|XVI{0,3}|XIX|XX)\s*$'
    PUNCTUATION_SUFFIX_PATTERN = r'([!+?]+)\s*$'

# Version keywords to detect in track titles (immutable tuple for performance)
VERSION_KEYWORDS = ('live', 'acoustic', 'unplugged', 'remix', 'edit', 'mix', 
                    'remaster', 'remastered', 'demo', 'instrumental', 'orchestral')

def _extract_version_info(title: str) -> tuple[str, set[str]]:
    """
    Extract base title and version keywords from a track title.
    
    IMPORTANT: Preserves title suffixes like "!", "+", "?", and Roman numerals (I, II, III, IV, etc.)
    to ensure different songs are not matched as the same track.
    
    Args:
        title: Track title (e.g., "Song Title (Live)", "Song Title - Acoustic Version")
        
    Returns:
        Tuple of (base_title, version_keywords_set)
        - base_title: Title without version suffixes (but preserving important suffixes)
        - version_keywords_set: Set of version keywords found (e.g., {'live', 'acoustic'})
    
    Examples:
        "Untot im Drachenboot (Live in Wacken 2022)" -> ("Untot im Drachenboot", {'live'})
        "Song Title - Acoustic Version" -> ("Song Title", {'acoustic'})
        "Regular Song" -> ("Regular Song", set())
        "Lost!" -> ("Lost!", set()) - preserves punctuation suffix
        "Life in Technicolor II" -> ("Life in Technicolor II", set()) - preserves Roman numeral
    """
    title_lower = title.lower()
    found_versions = set()
    
    # Check for version keywords in the title
    for keyword in VERSION_KEYWORDS:
        if re.search(r'\b' + re.escape(keyword) + r'\b', title_lower):
            found_versions.add(keyword)
    
    # Save the original title before removing parenthetical content
    original_title = title.strip()
    
    # Extract base title (remove parenthesized/bracketed content and dash-based suffixes)
    # But FIRST check if the title has important suffixes we need to preserve
    
    # Preserve punctuation suffixes (!, +, ?, etc.) at the end of the title
    # These distinguish different songs like "Lost!" vs "Lost+"
    preserved_suffix = ""
    title_match = re.search(PUNCTUATION_SUFFIX_PATTERN, original_title)
    if title_match:
        preserved_suffix = title_match.group(1)
        # Temporarily remove it for processing
        title = original_title[:title_match.start()]
    
    # Remove parenthesized/bracketed content
    base_title = re.sub(r'\s*[\(\[].*?[\)\]]', '', title)
    
    # Dynamically build pattern from VERSION_KEYWORDS for consistency
    # Remove dash-based version keywords BEFORE extracting Roman numerals
    version_pattern = '|'.join(keyword.capitalize() for keyword in VERSION_KEYWORDS)
    base_title = re.sub(
        r'\s*-\s*(?:' + version_pattern + r').*$', 
        '', 
        base_title, 
        flags=re.IGNORECASE
    )
    base_title = base_title.strip()
    
    # Preserve Roman numerals at the end (I, II, III, IV, V, etc.)
    # These distinguish different songs like "Song" vs "Song II"
    # Match space + Roman numeral at the end of the title (AFTER version keyword removal)
    roman_suffix = ""
    roman_match = re.search(ROMAN_NUMERAL_PATTERN, base_title, re.IGNORECASE)
    if roman_match:
        roman_suffix = " " + roman_match.group(1).lower()  # Normalize to lowercase with space
        # Temporarily remove it for final processing
        base_title = base_title[:roman_match.start()].strip()
    
    # Re-attach preserved suffixes in the correct order
    if roman_suffix:
        base_title = base_title + roman_suffix  # Suffix includes proper spacing (space + roman numeral)
    if preserved_suffix:
        base_title = base_title + preserved_suffix  # Add back punctuation suffix
    
    return base_title, found_versions

# Simple MBID cache to avoid repeated lookups
_mbid_cache = {}
_CACHE_FILE = "/tmp/mbid_cache.json" if os.path.exists("/tmp") else "mbid_cache.json"


def _escape_lucene_special_chars(text: str) -> str:
    r"""
    Escape special characters in Lucene query syntax.
    
    Lucene special characters that need escaping:
    + - && || ! ( ) { } [ ] ^ " ~ * ? : \ /
    
    Args:
        text: Text to escape
        
    Returns:
        Escaped text safe for use in Lucene queries
    """
    # Characters that need to be escaped with backslash in Lucene
    # Note: We escape double quotes by replacing them with escaped quotes
    special_chars = ['+', '-', '&', '|', '!', '(', ')', '{', '}', '[', ']', '^', '"', '~', '*', '?', ':', '\\', '/']
    
    escaped = text
    # Backslash must be escaped first to avoid double-escaping
    escaped = escaped.replace('\\', '\\\\')
    
    # Escape other special characters
    for char in special_chars:
        if char != '\\':  # Already handled
            escaped = escaped.replace(char, '\\' + char)
    
    return escaped


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
        self.headers = {"User-Agent": _USER_AGENT}
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
        
        # Use custom SSL adapter (imported from helpers) for HTTPS to handle SSL/TLS protocol issues
        ssl_adapter = SSLAdapter(max_retries=retry_strategy)
        
        # Apply to both http and https
        if hasattr(self.session, 'mount'):
            self.session.mount("http://", HTTPAdapter(max_retries=retry_strategy))
            self.session.mount("https://", ssl_adapter)
    
    def is_single(self, title: str, artist: str) -> bool:
        """
        Query MusicBrainz release-group by title+artist and check primary-type=Single.
        
        Also verifies that the version matches (e.g., doesn't match a studio single
        when checking a live version).
        
        Args:
            title: Track title
            artist: Artist name
            
        Returns:
            True if release-group type is Single AND version matches
        """
        if not self.enabled:
            return False
        
        # Extract version information from the track title
        base_title, track_versions = _extract_version_info(title)
        
        max_retries = 3
        retry_delay = 1.0
        for attempt in range(max_retries):
            try:
                # Use rate limiter to enforce proper delays between requests
                if _rate_limiter:
                    _rate_limiter.wait_if_needed_musicbrainz(max_wait_seconds=2.0)
                    _rate_limiter.record_musicbrainz_request()
                else:
                    # Fallback to simple delay if rate limiter not available
                    time.sleep(1.0)
                
                # Search using base title to find all versions
                # Quote title and artist to handle multi-word values properly (Lucene syntax)
                # Escape special characters to prevent query syntax errors
                escaped_title = _escape_lucene_special_chars(base_title)
                escaped_artist = _escape_lucene_special_chars(artist)
                query = f'releasegroup:"{escaped_title}" AND artist:"{escaped_artist}" AND primarytype:Single'
                params = {
                    "query": query,
                    "fmt": "json",
                    "limit": 10  # Increased to check more results for version matching
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
                
                # Check if any result is a single with matching version AND title
                for rg in rgs:
                    if (rg.get("primary-type") or "").lower() != "single":
                        continue
                    
                    # Extract version and base title from release-group title
                    rg_title = rg.get("title", "")
                    rg_base_title, rg_versions = _extract_version_info(rg_title)
                    
                    # Match if:
                    # 1. Base titles match exactly (case-insensitive, preserving suffixes like !, +, ?, II, III, etc.)
                    # 2. AND version keywords match (e.g., both live, both acoustic, or both studio)
                    if base_title.lower() == rg_base_title.lower() and track_versions == rg_versions:
                        logger.debug(f"MusicBrainz single match: '{title}' matched '{rg_title}' (base: '{base_title}' == '{rg_base_title}', versions: {track_versions})")
                        return True
                    else:
                        logger.debug(f"MusicBrainz mismatch: track '{title}' (base: '{base_title}', versions: {track_versions}) vs release '{rg_title}' (base: '{rg_base_title}', versions: {rg_versions})")
                
                # No matching single found with same version
                return False
                
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
        
        max_retries = 3
        retry_delay = 1.0
        
        for attempt in range(max_retries):
            try:
                # Use rate limiter to enforce proper delays between requests
                if _rate_limiter:
                    _rate_limiter.wait_if_needed_musicbrainz(max_wait_seconds=2.0)
                    _rate_limiter.record_musicbrainz_request()
                else:
                    # Fallback to simple delay if rate limiter not available
                    time.sleep(1.0)
                
                # Step 1: search recording with richer includes
                # Quote title and artist to handle multi-word values properly (Lucene syntax)
                # Escape special characters to prevent query syntax errors
                escaped_title = _escape_lucene_special_chars(title)
                escaped_artist = _escape_lucene_special_chars(artist)
                query = f'recording:"{escaped_title}" AND artist:"{escaped_artist}"'
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
            # Use rate limiter to enforce proper delays between requests
            if _rate_limiter:
                _rate_limiter.wait_if_needed_musicbrainz(max_wait_seconds=2.0)
                _rate_limiter.record_musicbrainz_request()
            else:
                # Fallback to simple delay if rate limiter not available
                time.sleep(1.0)
            
            # 1) Find recordings (with releases included for second hop)
            # Quote title and artist to handle multi-word values properly (Lucene syntax)
            # Escape special characters to prevent query syntax errors
            escaped_title = _escape_lucene_special_chars(title)
            escaped_artist = _escape_lucene_special_chars(artist)
            query = f'recording:"{escaped_title}" AND artist:"{escaped_artist}"'
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
                            # Use rate limiter for second lookup
                            if _rate_limiter:
                                _rate_limiter.wait_if_needed_musicbrainz(max_wait_seconds=2.0)
                                _rate_limiter.record_musicbrainz_request()
                            else:
                                # Fallback to simple delay if rate limiter not available
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
    
    def get_artist_country(self, artist: str) -> str:
        """
        Fetch artist country/origin from MusicBrainz.
        
        Args:
            artist: Artist name
            
        Returns:
            Country name (e.g., "United States", "United Kingdom") or empty string if not found
        """
        if not self.enabled:
            return ""
        
        max_retries = 3
        retry_delay = 1.0
        
        for attempt in range(max_retries):
            try:
                # Use rate limiter to enforce proper delays between requests
                if _rate_limiter:
                    _rate_limiter.wait_if_needed_musicbrainz(max_wait_seconds=2.0)
                    _rate_limiter.record_musicbrainz_request()
                else:
                    # Fallback to simple delay if rate limiter not available
                    time.sleep(1.0)
                
                # Search for artist with area information
                # Quote artist name to handle multi-word values properly (Lucene syntax)
                # Escape special characters to prevent query syntax errors
                escaped_artist = _escape_lucene_special_chars(artist)
                params = {
                    "query": f'artist:"{escaped_artist}"',
                    "fmt": "json",
                    "limit": 1,
                    "inc": "area"
                }
                r = self.session.get(f"{self.base_url}artist/", params=params, headers=self.headers, timeout=(3, 5))
                r.raise_for_status()
                artists = r.json().get("artists", [])
                
                if not artists:
                    return ""
                
                # Get the top match
                artist_data = artists[0]
                
                # Check for area (country) information
                area = artist_data.get("area", {})
                if area and area.get("name"):
                    return area["name"]
                
                # Fallback to begin-area (birth country for individuals)
                begin_area = artist_data.get("begin-area", {})
                if begin_area and begin_area.get("name"):
                    return begin_area["name"]
                
                return ""
                
            except (requests.exceptions.Timeout, requests.exceptions.SSLError, requests.exceptions.ConnectionError) as e:
                if attempt < max_retries - 1:
                    logger.debug(f"MusicBrainz artist country lookup attempt {attempt + 1} failed for '{artist}': {e}, retrying...")
                    time.sleep(retry_delay)
                    retry_delay *= 2  # Exponential backoff
                else:
                    logger.warning(f"MusicBrainz artist country lookup failed for '{artist}' after {max_retries} retries: {e}")
                    return ""
            except requests.exceptions.RequestException as e:
                logger.warning(f"MusicBrainz artist country lookup request error for '{artist}': {e}")
                return ""
            except Exception as e:
                logger.warning(f"MusicBrainz artist country lookup failed for '{artist}': {e}")
                return ""
        
        return ""


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

def get_artist_country(artist: str, enabled: bool = True) -> str:
    """Get artist country from MusicBrainz."""
    client = _get_musicbrainz_client(enabled)
    return client.get_artist_country(artist)
