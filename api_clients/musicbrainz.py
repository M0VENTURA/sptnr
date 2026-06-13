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

# Import SSLAdapter from helpers
import sys
# Add parent directory to path to import helpers
parent_dir = os.path.dirname(os.path.dirname(__file__))
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)
from helpers.helpers import SSLAdapter

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

# MusicBrainz UUID pattern (8-4-4-4-12 hex digits) – used for validating MBIDs
_MUSICBRAINZ_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.IGNORECASE
)

# Import rate limiter
try:
    from helpers.api_rate_limiter import get_rate_limiter
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
# Includes 'version' to catch custom versions like "Swing Tomorrow version by Rocksin"
# NOTE: 'remaster'/'remastered' are intentionally excluded from this list.
# Remastered versions are treated as the original release (same song, improved audio quality),
# not as alternate versions like remixes or live recordings.  A track titled
# "Higher (remastered 2024)" should still match the original "Higher" single release.
VERSION_KEYWORDS = ('live', 'acoustic', 'unplugged', 'remix', 'edit', 'mix',
                    'demo', 'instrumental', 'orchestral', 'version')

def _extract_version_info(title: str) -> tuple[str, set[str]]:
    """
    Extract base title and version keywords from a track title.
    
    IMPORTANT: Preserves title suffixes like "!", "+", "?", and Roman numerals (I, II, III, IV, etc.)
    to ensure different songs are not matched as the same track.
    
    SPECIAL HANDLING: Year-based versions (e.g., "2016 version") are NOT flagged as alternate
    versions because they represent different release years/remasters, not alternate versions
    like remixes or live performances. This prevents false negatives in single detection.

    REMASTER HANDLING: Remastered versions (e.g., "remastered 2024") are also NOT flagged as
    alternate versions — they are the same song with improved audio quality and should match
    the original studio single release in MusicBrainz lookups.
    
    Args:
        title: Track title (e.g., "Song Title (Live)", "Song Title - Acoustic Version")
        
    Returns:
        Tuple of (base_title, version_keywords_set)
        - base_title: Title without version suffixes (but preserving important suffixes)
        - version_keywords_set: Set of version keywords found (e.g., {'live', 'acoustic'})
    
    Examples:
        "Untot im Drachenboot (Live in Wacken 2022)" -> ("Untot im Drachenboot", {'live'})
        "Song Title - Acoustic Version" -> ("Song Title", {'acoustic'})
        "Song Title (2016 version)" -> ("Song Title", set()) - year-based version allowed ✓
        "Regular Song" -> ("Regular Song", set())
        "Lost!" -> ("Lost!", set()) - preserves punctuation suffix
        "Life in Technicolor II" -> ("Life in Technicolor II", set()) - preserves Roman numeral
        "Die Tomorrow (Swing Tomorrow version)" -> ("Die Tomorrow", {'version'}) - custom version blocked ✗
    """
    title_lower = title.lower()
    found_versions = set()
    
    # Check for version keywords in the title
    for keyword in VERSION_KEYWORDS:
        if re.search(r'\b' + re.escape(keyword) + r'\b', title_lower):
            # Special handling for 'version' keyword: only flag if NOT a year-based version
            # e.g., "2016 version" or "2022 version" are NOT alternate versions
            # but "Swing Tomorrow version" or "Hotel Lounge version" ARE alternate versions
            if keyword == 'version':
                # Check if "version" is preceded by a 4-digit year (2000-2999)
                if re.search(r'\d{4}\s+version', title_lower):
                    # This is a year-based version (e.g., "2016 version"), skip it
                    continue
                # Also check for just year before version without space
                if re.search(r'\(\d{4}\s*version', title_lower):
                    # This is a year-based version in parentheses, skip it
                    continue
            
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
    
    Note: single '&' and '|' are NOT Lucene operators (only '&&' and '||'
    are).  Escaping a bare '&' as '\&' inside a quoted phrase (e.g.
    artist:"Derek \& Brandon Fiechter") causes MusicBrainz to look for a
    literal backslash in the stored name and therefore returns zero results
    for artists whose names contain '&'.
    
    Args:
        text: Text to escape
        
    Returns:
        Escaped text safe for use in Lucene queries
    """
    # Characters that need to be escaped with backslash in Lucene.
    # '&' and '|' are intentionally omitted: the Lucene operators are the
    # two-character sequences '&&' and '||', not single characters.
    special_chars = ['+', '-', '!', '(', ')', '{', '}', '[', ']', '^', '"', '~', '*', '?', ':', '\\', '/']
    
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
        # Headers comply with MusicBrainz API requirements
        self.headers = {
            "User-Agent": _USER_AGENT,
            "Accept": "application/json"
        }
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
    
    def is_single(self, title: str, artist: str, artist_mbid: str = None, album_track_count: int = None) -> bool:
        """
        Query MusicBrainz to check if a track is a single.
        
        Two-stage approach:
        1. If artist_mbid is available, query by MBID (more accurate, avoids disambiguation)
        2. Fall back to title+artist name search (for cases without MBID)
        
        Also verifies that the version matches (e.g., doesn't match a studio single
        when checking a live version).
        
        IMPORTANT: Validates track count if provided. Releases with 4+ tracks cannot
        be classified as singles, even if MusicBrainz labels them as such. This prevents
        albums like "+44 - When Your Heart Stops Beating" (12 tracks) from being
        incorrectly classified as singles based solely on MusicBrainz data.
        
        Args:
            title: Track title
            artist: Artist name (used as fallback if MBID not available)
            artist_mbid: Optional MusicBrainz artist ID (preferred for accuracy)
            album_track_count: Optional album track count - if provided and >= 4, return False
            
        Returns:
            True if release-group type is Single AND version matches AND (no track_count OR track_count < 4)
        """
        # Sanity check: albums with 4+ tracks cannot be singles
        if album_track_count is not None and album_track_count >= 4:
            logger.debug(f"MusicBrainz single check rejected: album has {album_track_count} tracks (>= 4)")
            return False
        if not self.enabled:
            return False
        
        # Stage 1: Try MBID-based lookup if available (more accurate)
        if artist_mbid:
            try:
                result = self.is_single_by_artist_mbid(title, artist_mbid)
                if result:
                    logger.debug(f"MusicBrainz single detection: Found via artist MBID for '{title}'")
                    return True
                # If MBID lookup found no match, fall through to name-based search
                logger.debug(f"MusicBrainz single detection: MBID query returned no match for '{title}', trying name-based search")
            except Exception as e:
                logger.debug(f"MusicBrainz MBID lookup failed for '{title}': {e}, falling back to name-based search")
        
        # Stage 2: Fall back to name-based search (reliable fallback)
        
        # Extract version information from the track title
        base_title, track_versions = _extract_version_info(title)
        
        max_retries = 3
        retry_delay = 1.0
        for attempt in range(max_retries):
            try:
                # Use rate limiter to enforce proper delays between requests
                if _rate_limiter:
                    _rate_limiter.throttle_musicbrainz()
                else:
                    # Fallback to simple delay if rate limiter not available
                    time.sleep(1.0)
                
                # Search using base title WITHOUT Roman numerals/punctuation to find all versions
                # Lucene quoted queries are case-sensitive, so we strip Roman numerals and punctuation
                # to ensure "Life in Technicolor ii" finds "Life in Technicolor II"
                # We'll filter by exact base title match later (case-insensitive)
                search_title = base_title
                # Strip Roman numeral suffix for search (we'll match it later)
                search_title = re.sub(ROMAN_NUMERAL_PATTERN, '', search_title, flags=re.IGNORECASE).strip()
                # Strip punctuation suffix for search (we'll match it later)
                search_title = re.sub(PUNCTUATION_SUFFIX_PATTERN, '', search_title).strip()
                
                # Quote title and artist to handle multi-word values properly (Lucene syntax)
                # Escape special characters to prevent query syntax errors
                escaped_title = _escape_lucene_special_chars(search_title)
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
                
            except requests.exceptions.HTTPError as e:
                status_code = e.response.status_code if getattr(e, "response", None) is not None else None
                if status_code in (429, 503, 504):
                    if attempt < max_retries - 1:
                        logger.debug(
                            f"MusicBrainz is_single attempt {attempt+1}/{max_retries} got HTTP {status_code}, "
                            f"retrying in {retry_delay}s..."
                        )
                        time.sleep(retry_delay)
                        retry_delay *= 2
                        continue
                    logger.info(
                        f"MusicBrainz is_single temporarily unavailable for '{title}' by '{artist}' "
                        f"after {max_retries} attempts (HTTP {status_code})"
                    )
                    return False
                logger.warning(f"MusicBrainz is_single HTTP error for '{title}' by '{artist}': {e}")
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
    
    def is_single_by_artist_mbid(self, title: str, artist_mbid: str) -> bool:
        """
        Query MusicBrainz release-groups by artist MBID to find singles.
        
        This is more comprehensive than name-based search as it queries all releases
        by that specific artist, avoiding disambiguation and name variation issues.

        Uses an in-memory per-scan cache keyed by artist MBID so that multiple tracks
        for the same artist share a single API response (the full singles list is fetched
        once and reused for every subsequent lookup within the same process lifetime).
        
        Args:
            title: Track title
            artist_mbid: MusicBrainz artist ID
            
        Returns:
            True if a matching single is found for this title by the artist
        """
        if not self.enabled or not artist_mbid:
            return False

        # In-memory per-scan artist singles cache: maps artist_mbid → frozenset of
        # (base_title_lower, versions_frozenset) tuples so every track for the same
        # artist reuses the same API response.
        if not hasattr(self, '_artist_singles_cache'):
            self._artist_singles_cache = {}  # artist_mbid -> list[release-group dicts] or None

        # Extract version information from the track title
        base_title, track_versions = _extract_version_info(title)

        # Use cached release-group list if available; fetch on first access for this MBID.
        if artist_mbid not in self._artist_singles_cache:
            max_retries = 3
            retry_delay = 1.0
            fetched_rgs = None
            for attempt in range(max_retries):
                try:
                    if _rate_limiter:
                        _rate_limiter.throttle_musicbrainz()
                    else:
                        time.sleep(1.0)

                    params = {
                        "artist": artist_mbid,
                        "primarytype": "Single",
                        "fmt": "json",
                        "limit": 50,
                    }
                    if attempt == 0:
                        logger.debug(f"MusicBrainz is_single_by_artist_mbid: fetching singles list for artist={artist_mbid}")

                    res = self.session.get(
                        f"{self.base_url}release-group/",
                        params=params,
                        headers=self.headers,
                        timeout=(5, 10),
                    )
                    res.raise_for_status()
                    fetched_rgs = res.json().get("release-groups", [])
                    logger.debug(f"MusicBrainz: cached {len(fetched_rgs)} singles for artist {artist_mbid}")
                    break
                except requests.exceptions.HTTPError as e:
                    status_code = e.response.status_code if getattr(e, "response", None) is not None else None
                    if status_code in (429, 503, 504) and attempt < max_retries - 1:
                        time.sleep(retry_delay)
                        retry_delay *= 2
                        continue
                    logger.info(f"MusicBrainz is_single_by_artist_mbid cache-fetch failed (HTTP {status_code}) for artist {artist_mbid}")
                    break
                except (requests.exceptions.Timeout, requests.exceptions.SSLError, requests.exceptions.ConnectionError) as e:
                    if attempt < max_retries - 1:
                        time.sleep(retry_delay)
                        retry_delay *= 2
                    else:
                        logger.info(f"MusicBrainz is_single_by_artist_mbid cache-fetch unavailable for artist {artist_mbid}: {type(e).__name__}")
                    break
                except Exception as e:
                    logger.warning(f"MusicBrainz is_single_by_artist_mbid cache-fetch error for artist {artist_mbid}: {e}")
                    break
            # Use None sentinel for "fetch failed" (distinguishes from an empty result list
            # which means "artist has no singles") so transient failures allow a retry next scan.
            self._artist_singles_cache[artist_mbid] = fetched_rgs  # fetched_rgs is None on all-attempt failure

        rgs = self._artist_singles_cache[artist_mbid]
        if rgs is None:
            # Fetch failed (transient error); fall back to the name-based is_single() path
            logger.debug(f"MusicBrainz: Cache-fetch failed for artist MBID {artist_mbid}; returning False for '{title}' (will retry next scan run)")
            return False
        if not rgs:
            logger.debug(f"MusicBrainz: Artist MBID {artist_mbid} has no singles in MB; returning False for '{title}'")
            return False

        logger.debug(f"MusicBrainz: Checking {len(rgs)} cached singles for '{title}' (artist MBID: {artist_mbid})")
        for rg in rgs:
            rg_title = rg.get("title", "")
            rg_base_title, rg_versions = _extract_version_info(rg_title)
            if base_title.lower() == rg_base_title.lower() and track_versions == rg_versions:
                logger.debug(f"MusicBrainz single by MBID (cached): '{title}' matched '{rg_title}'")
                return True

        logger.debug(f"MusicBrainz: No matching single found for '{title}' in cached list (artist MBID: {artist_mbid})")
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
                    _rate_limiter.throttle_musicbrainz()
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
                _rate_limiter.throttle_musicbrainz()
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
                                _rate_limiter.throttle_musicbrainz()
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
                    _rate_limiter.throttle_musicbrainz()
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

    def get_artist_members(self, artist: str = None, artist_mbid: str = None) -> list[dict]:
        """
        Fetch band members for a MusicBrainz artist.

        Args:
            artist: Artist name to search when MBID is not available
            artist_mbid: Optional MusicBrainz artist MBID

        Returns:
            List of member dicts with name and relation metadata.
        """
        if not self.enabled:
            return []

        if not artist_mbid and not artist:
            return []

        max_retries = 3
        retry_delay = 1.0

        for attempt in range(max_retries):
            try:
                if _rate_limiter:
                    _rate_limiter.throttle_musicbrainz()
                else:
                    time.sleep(1.0)

                resolved_artist_mbid = artist_mbid
                if not resolved_artist_mbid:
                    escaped_artist = _escape_lucene_special_chars(artist)
                    search_params = {
                        "query": f'artist:"{escaped_artist}"',
                        "fmt": "json",
                        "limit": 10,
                    }
                    search_response = self.session.get(
                        f"{self.base_url}artist/",
                        params=search_params,
                        headers=self.headers,
                        timeout=(5, 10)
                    )
                    search_response.raise_for_status()
                    artists = search_response.json().get("artists", [])
                    if not artists:
                        return []

                    preferred = next(
                        (candidate for candidate in artists if (candidate.get("type") or "").lower() in {"group", "orchestra", "choir"}),
                        artists[0]
                    )
                    resolved_artist_mbid = preferred.get("id")
                    if not resolved_artist_mbid:
                        return []

                artist_response = self.session.get(
                    f"{self.base_url}artist/{resolved_artist_mbid}",
                    params={"fmt": "json", "inc": "artist-rels"},
                    headers=self.headers,
                    timeout=(5, 10)
                )
                artist_response.raise_for_status()
                artist_data = artist_response.json() or {}
                relations = artist_data.get("relations", []) or artist_data.get("artist-relation-list", []) or []

                members = []
                seen_names = set()
                allowed_relation_types = {
                    "member of band",
                    "member",
                    "founder",
                    "instrumental supporting musician",
                    "vocal supporting musician",
                }

                for relation in relations:
                    relation_type = (relation.get("type") or "").lower()
                    related_artist = relation.get("artist") or {}
                    member_name = (related_artist.get("name") or "").strip()
                    if not member_name:
                        continue
                    if relation_type and relation_type not in allowed_relation_types:
                        continue
                    if member_name.lower() in seen_names:
                        continue
                    seen_names.add(member_name.lower())
                    members.append({
                        "name": member_name,
                        "relation_type": relation.get("type") or "member",
                        "begin": relation.get("begin") or "",
                        "end": relation.get("end") or "",
                        "ended": bool(relation.get("ended")),
                        "attributes": relation.get("attributes") or relation.get("attribute-list") or [],
                    })

                members.sort(key=lambda member: (member.get("ended", False), member.get("name", "").lower()))
                return members

            except (requests.exceptions.Timeout, requests.exceptions.SSLError, requests.exceptions.ConnectionError) as e:
                if attempt < max_retries - 1:
                    logger.debug(f"MusicBrainz artist members attempt {attempt + 1} failed for '{artist or artist_mbid}': {e}, retrying...")
                    time.sleep(retry_delay)
                    retry_delay *= 2
                else:
                    logger.info(f"MusicBrainz artist members unavailable for '{artist or artist_mbid}' after {max_retries} attempts: {type(e).__name__}")
                    return []
            except requests.exceptions.HTTPError as e:
                status_code = getattr(getattr(e, 'response', None), 'status_code', None)
                if status_code == 503:
                    logger.info(f"MusicBrainz artist members temporarily unavailable for '{artist or artist_mbid}' (503)")
                    return []
                logger.warning(f"MusicBrainz artist members HTTP error for '{artist or artist_mbid}': {e}")
                return []
            except Exception as e:
                logger.warning(f"MusicBrainz artist members lookup failed for '{artist or artist_mbid}': {e}")
                return []

        return []

    def get_artist_member_names(self, artist: str = None, artist_mbid: str = None) -> list[str]:
        """Convenience wrapper returning only member names."""
        return [member.get("name", "") for member in self.get_artist_members(artist=artist, artist_mbid=artist_mbid) if member.get("name")]
    
    def has_video_relationship(self, title: str, artist: str) -> bool:
        """
        Check if a recording has a relationship to a music video.
        
        Music videos are typically made for singles, so this is a medium-confidence
        signal that the track was released as a single.
        
        Args:
            title: Track title
            artist: Artist name
            
        Returns:
            True if the recording has a video relationship, False otherwise
        """
        if not self.enabled:
            return False
        
        max_retries = 3
        retry_delay = 1.0
        
        for attempt in range(max_retries):
            try:
                # Use rate limiter
                if _rate_limiter:
                    _rate_limiter.throttle_musicbrainz()
                else:
                    time.sleep(1.0)
                
                # Search for recording with relationships
                escaped_title = _escape_lucene_special_chars(title)
                escaped_artist = _escape_lucene_special_chars(artist)
                query = f'recording:"{escaped_title}" AND artist:"{escaped_artist}"'
                params = {
                    "query": query,
                    "fmt": "json",
                    "limit": 1,
                    "inc": "url-rels"  # Include URL relationships (videos are typically URL rels)
                }
                
                r = self.session.get(f"{self.base_url}recording/", params=params, headers=self.headers, timeout=(5, 10))
                r.raise_for_status()
                recordings = r.json().get("recordings", [])
                
                if not recordings:
                    return False
                
                # Get the top match
                recording = recordings[0]
                relations = recording.get("relations", [])
                
                # Look for video relationships
                for rel in relations:
                    rel_type = rel.get("type", "").lower()
                    # Common video relationship types in MusicBrainz
                    if rel_type in ("video", "youtube", "vimeo", "streaming music", "free streaming"):
                        # Check if it's actually a video URL (not just audio streaming)
                        url = rel.get("url", {}).get("resource", "").lower()
                        if any(video_host in url for video_host in ["youtube.com", "youtu.be", "vimeo.com", "video"]):
                            logger.debug(f"MusicBrainz: Found video relationship for '{title}' by '{artist}': {rel_type}")
                            return True
                
                return False
                
            except (requests.exceptions.Timeout, requests.exceptions.SSLError, requests.exceptions.ConnectionError) as e:
                if attempt < max_retries - 1:
                    logger.debug(f"MusicBrainz video lookup attempt {attempt + 1} failed for '{title}' by '{artist}': {e}, retrying...")
                    time.sleep(retry_delay)
                    retry_delay *= 2
                else:
                    logger.debug(f"MusicBrainz video lookup failed for '{title}' by '{artist}' after {max_retries} retries: {e}")
                    return False
            except Exception as e:
                logger.debug(f"MusicBrainz video lookup error for '{title}' by '{artist}': {e}")
                return False
        
        return False
    
    def appears_on_various_artists(self, title: str, artist: str, min_appearances: int = 3) -> bool:
        """
        Check if a recording appears on multiple Various Artists compilation albums.
        
        Songs released as singles often appear on compilation albums, greatest hits,
        soundtracks, and other Various Artists releases. Multiple appearances on such
        albums is a medium-confidence signal that the track was a popular single.
        
        This method specifically checks for albums where the Release Artist is 
        "Various Artists" (or variants like "Various", "VA", "Soundtrack").
        
        Args:
            title: Track title
            artist: Artist name (the original artist)
            min_appearances: Minimum number of Various Artists album appearances (default: 3)
            
        Returns:
            True if the recording appears on at least min_appearances Various Artists albums
        """
        if not self.enabled:
            return False
        
        max_retries = 3
        retry_delay = 1.0
        
        for attempt in range(max_retries):
            try:
                # Use rate limiter
                if _rate_limiter:
                    _rate_limiter.throttle_musicbrainz()
                else:
                    time.sleep(1.0)
                
                # Search for recording with releases
                escaped_title = _escape_lucene_special_chars(title)
                escaped_artist = _escape_lucene_special_chars(artist)
                query = f'recording:"{escaped_title}" AND artist:"{escaped_artist}"'
                params = {
                    "query": query,
                    "fmt": "json",
                    "limit": 1,
                    "inc": "releases+artist-credits"
                }
                
                r = self.session.get(f"{self.base_url}recording/", params=params, headers=self.headers, timeout=(5, 10))
                r.raise_for_status()
                recordings = r.json().get("recordings", [])
                
                if not recordings:
                    return False
                
                # Get the top match
                recording = recordings[0]
                releases = recording.get("releases", [])
                
                # Count appearances on Various Artists albums only
                # Only count albums where the Release Artist is specifically "Various Artists" or variants
                various_artists_count = 0
                
                for release in releases:
                    # Check artist credits - only count if it's explicitly Various Artists
                    artist_credits = release.get("artist-credit", [])
                    if artist_credits:
                        # Get the first artist name from credits
                        release_artist = artist_credits[0].get("name", "").lower() if isinstance(artist_credits[0], dict) else ""
                        
                        # Only count if the release artist is explicitly "Various Artists" or known variants
                        if release_artist in ("various artists", "various", "va", "soundtrack"):
                            various_artists_count += 1
                            logger.debug(f"MusicBrainz: Found '{title}' on Various Artists album: {release.get('title', 'Unknown')} (artist: {release_artist})")
                
                if various_artists_count >= min_appearances:
                    logger.debug(f"MusicBrainz: '{title}' appears on {various_artists_count} Various Artists albums (threshold: {min_appearances})")
                    return True
                
                return False
                
            except (requests.exceptions.Timeout, requests.exceptions.SSLError, requests.exceptions.ConnectionError) as e:
                if attempt < max_retries - 1:
                    logger.debug(f"MusicBrainz various artists lookup attempt {attempt + 1} failed for '{title}' by '{artist}': {e}, retrying...")
                    time.sleep(retry_delay)
                    retry_delay *= 2
                else:
                    logger.debug(f"MusicBrainz various artists lookup failed for '{title}' by '{artist}' after {max_retries} retries: {e}")
                    return False
            except Exception as e:
                logger.debug(f"MusicBrainz various artists lookup error for '{title}' by '{artist}': {e}")
                return False
        
        return False
    
    def get_composers_for_track(self, title: str, artist: str) -> tuple[list[str], str, float]:
        """
        Fetch composer(s) for a track from MusicBrainz.
        
        Uses a two-step approach:
          1. Search for the recording to obtain its MBID.
          2. Look up the recording by MBID with inc=artist-rels+work-rels+work-level-rels
             so that composer/writer/lyricist credits on both the recording and any
             linked Work entities are returned inline.
        
        The MusicBrainz search endpoint does not support the ``inc`` parameter for
        relationship data, so passing ``inc`` to a search request always returns empty
        ``relations``.  The lookup endpoint (/recording/{mbid}) is the only way to
        retrieve relationship data.
        
        Args:
            title: Track title
            artist: Artist name
            
        Returns:
            Tuple of (composers, recording_mbid, confidence) where composers is a list of
            composer names, recording_mbid is the MusicBrainz recording UUID (empty string
            if not found or on error), and confidence is a 0.0–1.0 title-similarity score
            (1.0 on exact match, 0.0 when nothing was found).
        """
        if not self.enabled:
            return [], "", 0.0
        
        max_retries = 3
        retry_delay = 1.0
        
        for attempt in range(max_retries):
            try:
                # Step 1: Search for the recording to obtain its MBID.
                # The search endpoint does NOT support ``inc`` for relationship data;
                # only the lookup endpoint does.
                if _rate_limiter:
                    _rate_limiter.throttle_musicbrainz()
                else:
                    time.sleep(1.0)
                
                escaped_title = _escape_lucene_special_chars(title)
                escaped_artist = _escape_lucene_special_chars(artist)
                query = f'recording:"{escaped_title}" AND artist:"{escaped_artist}"'
                search_params = {
                    "query": query,
                    "fmt": "json",
                    "limit": 1,
                }
                
                r = self.session.get(f"{self.base_url}recording/", params=search_params, headers=self.headers, timeout=(5, 10))
                r.raise_for_status()
                recordings = r.json().get("recordings", [])
                
                if not recordings:
                    return [], "", 0.0
                
                recording_mbid = recordings[0].get("id")
                if not recording_mbid:
                    return [], "", 0.0

                # Compute title-similarity confidence so callers can apply thresholds.
                found_title = (recordings[0].get("title") or "").lower()
                confidence = difflib.SequenceMatcher(None, title.lower(), found_title).ratio()
                
                # Step 2: Look up the recording by MBID with relationship includes.
                # ``artist-rels``      – direct artist credits on the recording itself
                # ``work-rels``        – works (songs) the recording is a performance of
                # ``work-level-rels``  – inline artist-rels on each linked work so that
                #                        composer/lyricist/writer credits attached to
                #                        the Work entity are returned without a third hop
                if _rate_limiter:
                    _rate_limiter.throttle_musicbrainz()
                else:
                    time.sleep(1.0)
                
                lookup_params = {
                    "fmt": "json",
                    "inc": "artist-rels+work-rels+work-level-rels",
                }
                rl = self.session.get(
                    f"{self.base_url}recording/{recording_mbid}",
                    params=lookup_params,
                    headers=self.headers,
                    timeout=(5, 10),
                )
                rl.raise_for_status()
                recording = rl.json()
                
                # The lookup endpoint returns relationships under ``relations``
                # (not ``relationships`` which is used by some older endpoints).
                relations = recording.get("relations", [])
                
                composers = []
                for rel in relations:
                    # Look for composer, writer, or lyricist relationships directly
                    # on the recording (uncommon but possible).
                    rel_type = rel.get("type", "").lower()
                    if rel_type in ("composer", "writer", "lyricist"):
                        target = rel.get("artist", {})
                        if target and target.get("name"):
                            composers.append(target["name"])

                    # Work relationships: the recording is a "performance" of a Work.
                    # With work-level-rels the Work entity has its own ``relations``
                    # list containing the composer/lyricist/writer credits we need.
                    work = rel.get("work", {})
                    if work:
                        for work_rel in work.get("relations", []) or []:
                            work_rel_type = str(work_rel.get("type", "")).lower()
                            if work_rel_type in ("composer", "writer", "lyricist"):
                                work_target = work_rel.get("artist", {})
                                if work_target and work_target.get("name"):
                                    composers.append(work_target["name"])
                
                return list(dict.fromkeys(composers)), recording_mbid, round(confidence, 3)
                
            except (requests.exceptions.Timeout, requests.exceptions.SSLError, requests.exceptions.ConnectionError) as e:
                if attempt < max_retries - 1:
                    logger.debug(f"MusicBrainz composer lookup attempt {attempt + 1} failed for '{title}' by '{artist}': {e}, retrying...")
                    time.sleep(retry_delay)
                    retry_delay *= 2
                else:
                    logger.debug(f"MusicBrainz composer lookup failed for '{title}' by '{artist}' after {max_retries} retries: {e}")
                    return [], "", 0.0
            except Exception as e:
                logger.debug(f"MusicBrainz composer lookup error for '{title}' by '{artist}': {e}")
                return [], "", 0.0
        
        return [], "", 0.0


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


def lookup_recording_clean_names(title: str, artist: str, enabled: bool = True) -> dict:
    """
    Search MusicBrainz for a recording and return clean canonical artist/title names.

    Exportify CSVs often have semicolon-joined artists (e.g. "Atreyu;Soulfly;Max Cavalera").
    This function queries MusicBrainz recordings and returns the primary artist credit
    and recording title so downstream matching (library fuzzy-match and Soulseek search)
    use clean names.

    Args:
        title: Track title (from CSV / external source)
        artist: Artist name (may contain semicolons from Exportify)
        enabled: Whether MusicBrainz lookups are enabled

    Returns:
        Dict with keys:
            - artist: Clean primary artist name (empty string if lookup failed)
            - title: Clean recording title (empty string if lookup failed)
            - recording_mbid: MusicBrainz recording UUID (empty string if not found)
            - confidence: 0.0–1.0 title-similarity score
    """
    result = {"artist": "", "title": "", "recording_mbid": "", "confidence": 0.0}
    if not enabled:
        return result

    # Use the first artist if semicolon-separated
    primary_artist = artist.split(";")[0].strip() if ";" in artist else artist.strip()
    if not primary_artist or not title:
        return result

    try:
        client = _get_musicbrainz_client(enabled)

        if _rate_limiter:
            _rate_limiter.throttle_musicbrainz()
        else:
            time.sleep(1.0)

        escaped_title = _escape_lucene_special_chars(title)
        escaped_artist = _escape_lucene_special_chars(primary_artist)
        query = f'recording:"{escaped_title}" AND artist:"{escaped_artist}"'
        params = {
            "query": query,
            "fmt": "json",
            "limit": 3,
            "inc": "artist-credits",
        }
        r = client.session.get(
            f"{client.base_url}recording/",
            params=params,
            headers=client.headers,
            timeout=(5, 10),
        )
        r.raise_for_status()
        recordings = r.json().get("recordings", []) or []
        if not recordings:
            return result

        # Pick the best match by title similarity
        best_rec = None
        best_score = 0.0
        nav_title = (title or "").lower()
        for rec in recordings:
            rec_title = (rec.get("title") or "").lower()
            score = difflib.SequenceMatcher(None, nav_title, rec_title).ratio()
            if score > best_score:
                best_score = score
                best_rec = rec

        if not best_rec or best_score < 0.5:
            return result

        # Extract primary artist from artist-credit
        artist_credits = best_rec.get("artist-credit", [])
        clean_artist = ""
        if artist_credits and isinstance(artist_credits[0], dict):
            clean_artist = (artist_credits[0].get("name") or "").strip()

        clean_title = (best_rec.get("title") or "").strip()
        recording_mbid = (best_rec.get("id") or "").strip()

        result["artist"] = clean_artist or primary_artist
        result["title"] = clean_title or title
        result["recording_mbid"] = recording_mbid
        result["confidence"] = round(best_score, 3)
        return result
    except requests.exceptions.Timeout:
        logger.debug(f"MusicBrainz clean-name lookup timed out for '{title}' by '{artist}'")
        return result
    except Exception as e:
        logger.debug(f"MusicBrainz clean-name lookup failed for '{title}' by '{artist}': {e}")
        return result


def get_album_type_with_fallback(artist: str, album: str, spotify_album_type: str = None, enabled: bool = True, track_count: int = None, release_group_mbid: str = None) -> tuple[str, str, str | None]:
    """
    Get album type from MusicBrainz with Spotify fallback using intelligent candidate scoring.

    Strategy:
    1. If release_group_mbid is provided, do a direct lookup by MBID (most accurate)
    2. Otherwise query MusicBrainz for release group by artist + album title
    3. Score all candidates by relevance (title match, track count validation)
    4. Return primary_type and secondary_types (check for "compilation")
    5. Fall back to Spotify album_type if MusicBrainz doesn't find it
    6. Use track count to validate/correct misclassifications (e.g., EP with >6 tracks)

    Args:
        artist: Artist name
        album: Album title
        spotify_album_type: Spotify album type (as fallback)
        enabled: Whether MusicBrainz is enabled
        track_count: Number of tracks in the album (for validation/scoring)
        release_group_mbid: MusicBrainz release group MBID for direct lookup (optional)

    Returns:
        Tuple of (album_type, source, discovered_mbid) where:
        - album_type: "album", "single", "ep", "compilation" or "unknown"
        - source: "musicbrainz", "spotify", or "fallback"
        - discovered_mbid: the release-group MBID found (or None)
    """
    if not enabled:
        if spotify_album_type:
            return (spotify_album_type.lower() if spotify_album_type else "unknown", "spotify", None)
        return ("unknown", "fallback", None)
    
    try:
        client = _get_musicbrainz_client(enabled)
        
        # Use rate limiter before request
        if _rate_limiter:
            _rate_limiter.throttle_musicbrainz()
        else:
            time.sleep(1.0)
        
        # If we have a release group MBID, do a direct lookup (more accurate than text search)
        # Validate the MBID format (UUID: 8-4-4-4-12 hex digits) before using it in a URL.
        if release_group_mbid and _MUSICBRAINZ_UUID_RE.match(str(release_group_mbid)):
            try:
                rg_res = client.session.get(
                    f"{client.base_url}release-group/{release_group_mbid}",
                    params={"fmt": "json"},
                    headers=client.headers,
                    timeout=(5, 10)
                )
                rg_res.raise_for_status()
                rg = rg_res.json()
                primary_type = (rg.get("primary-type") or "").lower()
                secondary_types = [s.lower() for s in (rg.get("secondary-types") or [])]
                
                album_type = primary_type
                if primary_type in ("album", "single", "ep"):
                    displayable_secondary = None
                    for sec_type in ["compilation", "live", "remix", "soundtrack", "spokenword", "demo", "dj-mix", "mixtape/street"]:
                        if sec_type in secondary_types:
                            displayable_secondary = sec_type
                            break
                    
                    if displayable_secondary:
                        if displayable_secondary == "spokenword":
                            displayable_secondary = "spoken word"
                        elif displayable_secondary == "dj-mix":
                            displayable_secondary = "dj mix"
                        album_type = f"{primary_type} ({displayable_secondary})"
                
                logger.debug(f"MusicBrainz: Album '{album}' by '{artist}' type={album_type} (primary={primary_type}, secondary={secondary_types}, via MBID={release_group_mbid})")
                return (album_type, "musicbrainz", release_group_mbid)
            except Exception as mbid_err:
                logger.debug(f"MusicBrainz direct MBID lookup failed for '{release_group_mbid}': {mbid_err}, falling back to text search")
                # Rate limit before the text search fallback
                if _rate_limiter:
                    _rate_limiter.throttle_musicbrainz()
                else:
                    time.sleep(1.0)
        
        # Escape special characters for Lucene query
        escaped_artist = _escape_lucene_special_chars(artist)
        escaped_album = _escape_lucene_special_chars(album)
        
        params = {
            "query": f'artist:"{escaped_artist}" AND releasegroup:"{escaped_album}"',
            "fmt": "json",
            "limit": 5
        }
        
        res = client.session.get(
            f"{client.base_url}release-group/",
            params=params,
            headers=client.headers,
            timeout=(5, 10)
        )
        res.raise_for_status()
        
        release_groups = res.json().get("release-groups", []) or []
        
        if release_groups:
            # Score all candidates and pick the best match
            # This helps when there are multiple releases with similar names
            best_match = None
            best_score = -1
            
            for rg in release_groups:
                score = 0
                rg_title = (rg.get("title") or "").lower()
                album_lower = album.lower()
                
                # Exact title match gets highest score
                if rg_title == album_lower:
                    score += 100
                # Partial match
                elif album_lower in rg_title or rg_title in album_lower:
                    score += 50
                
                # Penalize EP classification if track count suggests otherwise  
                if track_count and track_count > 0:
                    primary = (rg.get("primary-type") or "").lower()
                    if primary == "ep" and track_count > 6:
                        score -= 20  # Penalize EP for >6 tracks
                    elif primary == "single" and track_count > 3:
                        score -= 30  # Penalize single for >3 tracks
                
                if score > best_score:
                    best_score = score
                    best_match = rg
            
            rg = best_match or release_groups[0]
            primary_type = (rg.get("primary-type") or "").lower()
            secondary_types = [s.lower() for s in (rg.get("secondary-types") or [])]
            
            # Note: Per MusicBrainz spec, "Compilation" is a SECONDARY type, not a primary type.
            # Valid primary types are: Album, Single, EP, Broadcast, Other.
            # Compilation detection is handled below in secondary types.
            
            # Combine primary type with secondary types for enhanced classification
            # Format: "primary (secondary)" for better readability
            # Examples: "album (live)", "album (soundtrack)", "album (compilation)", "ep (compilation)"
            album_type = primary_type
            if primary_type in ("album", "single", "ep"):
                # Check for secondary type modifiers in priority order
                displayable_secondary = None
                for sec_type in ["compilation", "live", "remix", "soundtrack", "spokenword", "demo", "dj-mix", "mixtape/street"]:
                    if sec_type in secondary_types:
                        displayable_secondary = sec_type
                        break
                
                if displayable_secondary:
                    # Normalize for display
                    if displayable_secondary == "spokenword":
                        displayable_secondary = "spoken word"
                    elif displayable_secondary == "dj-mix":
                        displayable_secondary = "dj mix"
                    
                    album_type = f"{primary_type} ({displayable_secondary})"
                
                logger.debug(f"MusicBrainz: Album '{album}' by '{artist}' type={album_type} (primary={primary_type}, secondary={secondary_types}, candidate_score={best_score})")
                return (album_type, "musicbrainz", rg.get("id"))
        
        # MusicBrainz didn't find this album, fall back to Spotify
        if spotify_album_type:
            album_type = spotify_album_type.lower()
            logger.debug(f"MusicBrainz: No match for '{album}' by '{artist}', using Spotify type: {album_type}")
            return (album_type, "spotify", None)
        
        logger.debug(f"Album type detection failed for '{album}' by '{artist}' - no MusicBrainz match and no Spotify type")
        return ("unknown", "fallback", None)
        
    except (requests.exceptions.Timeout, requests.exceptions.SSLError, requests.exceptions.ConnectionError) as e:
        logger.debug(f"MusicBrainz album type lookup failed for '{album}' by '{artist}': {e}, falling back to Spotify")
        if spotify_album_type:
            return (spotify_album_type.lower(), "spotify", None)
        return ("unknown", "fallback", None)
    except Exception as e:
        logger.debug(f"Unexpected error getting album type for '{album}' by '{artist}': {e}")
        if spotify_album_type:
            return (spotify_album_type.lower(), "spotify", None)
        return ("unknown", "fallback", None)


def lookup_and_save_artist_mbid(artist: str, db_connection) -> str:
    """
    Look up artist MBID from MusicBrainz and save it to database.
    
    Uses intelligent artist selection:
    - Prefers "Group" type for band/ensemble names
    - Prefers official artists over disambiguation variants
    - Uses partial name matching if exact match fails
    - Logs candidates for debugging
    
    Saves MBID to database for all tracks by this artist.
    
    Args:
        artist: Artist name
        db_connection: Database connection
        
    Returns:
        Artist MBID (or empty string if not found)
    """
    if not artist:
        return ""
    
    try:
        client = _get_musicbrainz_client(enabled=True)
        
        # Use rate limiter before request
        if _rate_limiter:
            _rate_limiter.throttle_musicbrainz()
        else:
            time.sleep(1.0)
        
        # Escape special characters for Lucene query
        escaped_artist = _escape_lucene_special_chars(artist)
        
        params = {
            "query": f'artist:"{escaped_artist}"',
            "fmt": "json",
            "limit": 10  # Get more candidates for better selection
        }
        
        res = client.session.get(
            f"{client.base_url}artist/",
            params=params,
            headers=client.headers,
            timeout=(5, 10)
        )
        res.raise_for_status()
        
        artists_data = res.json().get("artists", []) or []
        
        if not artists_data:
            logger.debug(f"No MusicBrainz matches found for artist: {artist}")
            return ""
        
        logger.debug(f"MusicBrainz found {len(artists_data)} candidates for artist: {artist}")
        
        # Score and rank candidates
        best_candidate = None
        best_score = -1
        
        for candidate in artists_data:
            score = 0
            candidate_name = candidate.get("name", "")
            artist_type = candidate.get("type", "").lower()
            disambiguation = candidate.get("disambiguation", "").lower()
            mbid = candidate.get("id", "")
            
            # Exact name match: +100 points
            if candidate_name.lower() == artist.lower():
                score += 100
                logger.debug(f"  Candidate: {candidate_name} (MBID: {mbid}) - EXACT MATCH")
            # Partial/close match: +50 points
            elif artist.lower() in candidate_name.lower():
                score += 50
                logger.debug(f"  Candidate: {candidate_name} (MBID: {mbid}) - PARTIAL MATCH")
            else:
                logger.debug(f"  Candidate: {candidate_name} (MBID: {mbid}) - NO MATCH")
                continue  # Skip if not even a partial match
            
            # Prefer "Group" type for bands: +25 points
            if artist_type == "group":
                score += 25
                logger.debug(f"    Type: Group (+25)")
            elif artist_type == "person":
                score += 0
                logger.debug(f"    Type: Person (no bonus)")
            else:
                score += 10
                logger.debug(f"    Type: {artist_type} (+10)")
            
            # Penalize disambiguation suffixes (usually indicates alternate artist): -10 points
            if disambiguation:
                score -= 10
                logger.debug(f"    Disambiguation: '{disambiguation}' (-10)")
            
            # Prefer active artists (with life-span): +5 points
            life_span = candidate.get("life-span", {})
            if life_span and not life_span.get("ended"):
                score += 5
                logger.debug(f"    Status: Active (+5)")
            
            logger.debug(f"    Total Score: {score}")
            
            if score > best_score:
                best_score = score
                best_candidate = candidate
        
        if not best_candidate or best_score < 0:
            logger.debug(f"No suitable MusicBrainz match for artist: {artist} (best score: {best_score})")
            return ""
        
        mbid = best_candidate.get("id", "")
        best_name = best_candidate.get("name", "")
        
        if not mbid:
            logger.debug(f"Best candidate has no MBID: {best_name}")
            return ""
        
        logger.info(f"Selected MusicBrainz match for '{artist}': '{best_name}' (MBID: {mbid}, score: {best_score})")
        
        # Save MBID to database for all tracks by this artist
        cursor = db_connection.cursor()
        
        placeholder = "%s"
        
        # Update tracks where musicbrainz_artist_id is NULL or contains
        # multiple MBIDs (separated by ;, ,, space, or /). We first fetch
        # all candidate rows and filter in Python so this works on both
        # PostgreSQL and SQLite.
        cursor.execute(f"""
            SELECT id, musicbrainz_artist_id FROM tracks
            WHERE artist = {placeholder}
        """, (artist,))
        _to_fix = []
        for _row in cursor.fetchall():
            _existing = (_row[1] if _row[1] is not None else '')
            if not _existing or not _MUSICBRAINZ_UUID_RE.match(str(_existing).strip()):
                _to_fix.append(_row[0])
        if _to_fix:
            # Batch update in chunks of 500 to avoid huge parameter lists
            _chunk_size = 500
            for _chunk_start in range(0, len(_to_fix), _chunk_size):
                _chunk = _to_fix[_chunk_start:_chunk_start + _chunk_size]
                _ph_list = ','.join([placeholder] * len(_chunk))
                cursor.execute(f"""
                    UPDATE tracks
                    SET musicbrainz_artist_id = {placeholder}
                    WHERE id IN ({_ph_list})
                """, (mbid, *_chunk))
            db_connection.commit()
            logger.info(f"Corrected {len(_to_fix)} track(s) for artist '{artist}' to single MBID: {mbid}")
        else:
            db_connection.commit()

        # Count how many tracks now have the corrected single MBID
        cursor.execute(f"SELECT COUNT(*) FROM tracks WHERE artist = {placeholder} AND musicbrainz_artist_id = {placeholder}", (artist, mbid))
        result = cursor.fetchone()
        updated_count = result[0] if result else 0
        logger.info(f"Updated {updated_count} tracks for artist '{artist}' with MBID: {mbid}")
        
        return mbid
        
    except (requests.exceptions.Timeout, requests.exceptions.SSLError, requests.exceptions.ConnectionError) as e:
        logger.debug(f"MusicBrainz artist lookup timeout for '{artist}': {e}")
        return ""
    except Exception as e:
        logger.debug(f"Error looking up artist MBID for '{artist}': {e}")
        return ""
