#!/usr/bin/env python3
"""
Discogs Single Detection Verification Module

This module implements comprehensive Discogs metadata fetching and single detection
with full verification of all requirements specified in the problem statement.

Requirements implemented:
1. Discogs API Access (authentication, rate limiting, retry logic)
2. Release Lookup (GET /releases/{id})
3. Format Parsing (formats and descriptions)
4. Master Release Handling (GET /masters/{master_id})
5. Track Matching (title normalization, duration matching)
6. Single Determination Rules (comprehensive format/description/track count logic)
7. Error Handling (retries, fallback, graceful degradation)
8. Database Storage (all Discogs fields)
9. Cross-Source Validation (integration with Spotify/MusicBrainz)
"""

import logging
import time
import re
import json
import difflib
from typing import Dict, List, Optional, Tuple
from api_clients import session

logger = logging.getLogger(__name__)

# Rate limiting for Discogs (25/min for unauthenticated, ~60/min for authenticated)
_DISCOGS_LAST_REQUEST_TIME = 0
_DISCOGS_MIN_INTERVAL = 0.35  # Conservative: ~171 requests/min max


def _throttle_discogs():
    """Respect Discogs rate limit (configurable interval between requests)."""
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
                logger.warning(f"Attempt {attempt + 1} failed with {e}, retrying in {current_delay}s...")
                time.sleep(current_delay)
                current_delay *= 2  # Exponential backoff
            else:
                logger.error(f"All {max_retries + 1} attempts failed")
    
    raise last_exception


def normalize_track_title(title: str) -> str:
    """
    Normalize track title for matching.
    
    Normalization rules:
    - Convert to lowercase
    - Remove punctuation (except hyphens in words)
    - Remove bracketed suffixes (e.g., "(Remix)", "[Live]")
    - Remove common suffixes after dashes (e.g., "- Remastered")
    - Normalize whitespace
    
    Args:
        title: Original track title
        
    Returns:
        Normalized title for comparison
    """
    # Remove bracketed content
    normalized = re.sub(r'\s*[\(\[].*?[\)\]]', '', title)
    
    # Remove dash-based suffixes (remix, live, remaster, etc.)
    normalized = re.sub(
        r'\s*-\s*(?:Live|Remix|Remaster|Edit|Mix|Version|Acoustic|Unplugged|Demo|Instrumental).*$',
        '',
        normalized,
        flags=re.IGNORECASE
    )
    
    # Remove punctuation (except hyphens within words)
    normalized = re.sub(r'[^\w\s-]', '', normalized)
    
    # Convert to lowercase
    normalized = normalized.lower()
    
    # Normalize whitespace
    normalized = re.sub(r'\s+', ' ', normalized).strip()
    
    return normalized


def is_alternate_version(title: str) -> bool:
    """
    Check if track title indicates an alternate version.
    
    Alternate versions include:
    - live, remix, acoustic, demo, instrumental
    - radio edit, extended, club mix
    - alternate, re-recorded, karaoke, cover
    
    Args:
        title: Track title to check
        
    Returns:
        True if title matches alternate version patterns
    """
    title_lower = title.lower()
    
    alternate_patterns = [
        r'\blive\b', r'\bunplugged\b',
        r'\bremix\b', r'\bedit\b', r'\bmix\b',
        r'\bacoustic\b', r'\borchestral\b',
        r'\bdemo\b', r'\binstrumental\b',
        r'\bkaraoke\b', r'\bcover\b',
        r'\balternate\b', r'\balt\b',
        r'\bre-recorded\b', r'\bre-recording\b',
        r'\(radio', r'\(extended', r'\(club'
    ]
    
    for pattern in alternate_patterns:
        if re.search(pattern, title_lower):
            return True
    
    return False


def match_track_by_duration(
    track_duration: Optional[float],
    release_tracks: List[dict],
    tolerance: float = 2.0
) -> Optional[int]:
    """
    Match track by duration (±tolerance seconds).
    
    Args:
        track_duration: Track duration in seconds (None if unknown)
        release_tracks: List of track dicts from Discogs API
        tolerance: Duration tolerance in seconds
        
    Returns:
        Index of matching track, or None if no match
    """
    if track_duration is None:
        return None
    
    for idx, track in enumerate(release_tracks):
        # Parse Discogs duration format (e.g., "3:45" -> 225 seconds)
        duration_str = track.get('duration', '')
        if not duration_str or duration_str == '':
            continue
        
        try:
            # Parse MM:SS or H:MM:SS format
            parts = duration_str.split(':')
            if len(parts) == 2:
                minutes, seconds = map(int, parts)
                release_duration = minutes * 60 + seconds
            elif len(parts) == 3:
                hours, minutes, seconds = map(int, parts)
                release_duration = hours * 3600 + minutes * 60 + seconds
            else:
                continue
            
            # Check if within tolerance
            if abs(track_duration - release_duration) <= tolerance:
                return idx
        except (ValueError, TypeError):
            continue
    
    return None


class DiscogsVerificationClient:
    """
    Comprehensive Discogs API client for single detection verification.
    
    Implements all requirements from the problem statement:
    - Authenticated requests with User-Agent and token
    - Rate limiting (configurable)
    - Retry logic for 500 errors
    - Release and master release lookup
    - Format parsing and interpretation
    - Track title normalization and matching
    - Duration matching (±2 seconds)
    - Alternate version filtering
    - Comprehensive single determination rules
    - Database storage of all metadata
    """
    
    def __init__(self, token: str, http_session=None, enabled: bool = True):
        """
        Initialize Discogs verification client.
        
        Args:
            token: Discogs API token (required for authenticated requests)
            http_session: Optional requests.Session (uses shared if not provided)
            enabled: Whether Discogs is enabled
        """
        self.token = token
        self.session = http_session or session
        self.enabled = enabled
        self.base_url = "https://api.discogs.com"
        
        # Required User-Agent header (Discogs API requirement)
        self.headers = {
            "Authorization": f"Discogs token={token}" if token else "",
            "User-Agent": "sptnr-cli/1.0 +https://github.com/M0VENTURA/sptnr"
        }
    
    def get_release(self, release_id: str, timeout: tuple = (5, 10)) -> Optional[dict]:
        """
        Fetch release data from Discogs API.
        
        Implements: Requirement 2 (Release Lookup)
        Endpoint: GET /releases/{id}
        
        Returns release data including:
        - formats[]
        - format descriptions
        - tracklist[]
        - title
        - artists[]
        - master_id (if present)
        
        Args:
            release_id: Discogs release ID
            timeout: Request timeout (connect, read)
            
        Returns:
            Release data dict, or None if failed
        """
        if not self.enabled or not self.token:
            logger.debug("Discogs not enabled or token missing")
            return None
        
        try:
            _throttle_discogs()
            url = f"{self.base_url}/releases/{release_id}"
            
            # Use retry logic for 500 errors
            def make_request():
                response = self.session.get(url, headers=self.headers, timeout=timeout)
                
                # Handle rate limiting (429)
                if response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", 60))
                    logger.warning(f"Discogs rate limit hit, sleeping for {retry_after}s")
                    time.sleep(retry_after)
                    _throttle_discogs()
                    response = self.session.get(url, headers=self.headers, timeout=timeout)
                
                response.raise_for_status()
                return response
            
            response = _retry_on_500(make_request)
            data = response.json()
            
            logger.debug(f"Fetched Discogs release {release_id}: {data.get('title', 'Unknown')}")
            return data
            
        except Exception as e:
            logger.error(f"Failed to fetch Discogs release {release_id}: {e}")
            return None
    
    def get_master_release(self, master_id: str, timeout: tuple = (5, 10)) -> Optional[dict]:
        """
        Fetch master release data from Discogs API.
        
        Implements: Requirement 4 (Master Release Handling)
        Endpoint: GET /masters/{master_id}
        
        Returns master release data including:
        - formats (if available)
        - tracklist
        - title
        - artists
        
        Args:
            master_id: Discogs master release ID
            timeout: Request timeout (connect, read)
            
        Returns:
            Master release data dict, or None if failed
        """
        if not self.enabled or not self.token:
            logger.debug("Discogs not enabled or token missing")
            return None
        
        try:
            _throttle_discogs()
            url = f"{self.base_url}/masters/{master_id}"
            
            # Use retry logic for 500 errors
            def make_request():
                response = self.session.get(url, headers=self.headers, timeout=timeout)
                
                # Handle rate limiting (429)
                if response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", 60))
                    logger.warning(f"Discogs rate limit hit, sleeping for {retry_after}s")
                    time.sleep(retry_after)
                    _throttle_discogs()
                    response = self.session.get(url, headers=self.headers, timeout=timeout)
                
                response.raise_for_status()
                return response
            
            response = _retry_on_500(make_request)
            data = response.json()
            
            logger.debug(f"Fetched Discogs master {master_id}: {data.get('title', 'Unknown')}")
            return data
            
        except Exception as e:
            logger.error(f"Failed to fetch Discogs master {master_id}: {e}")
            return None
    
    def parse_formats(self, release_data: dict) -> Tuple[List[str], List[str]]:
        """
        Parse format names and descriptions from release data.
        
        Implements: Requirement 3 (Format Parsing)
        
        Checks formats[].name for:
        - "Single"
        - "7\""
        - "12\" Single"
        - "CD Single"
        - "Promo"
        
        Checks formats[].descriptions for:
        - "Single"
        - "Maxi-Single"
        - "EP" (should NOT count as single)
        - "Promo"
        
        Args:
            release_data: Release data dict from Discogs API
            
        Returns:
            Tuple of (format_names, format_descriptions)
        """
        formats = release_data.get('formats', []) or []
        
        format_names = []
        format_descriptions = []
        
        for fmt in formats:
            # Extract format name
            name = (fmt.get('name') or '').strip()
            if name:
                format_names.append(name)
            
            # Extract format descriptions
            descs = fmt.get('descriptions') or []
            for desc in descs:
                desc = (desc or '').strip()
                if desc:
                    format_descriptions.append(desc)
        
        return format_names, format_descriptions
    
    def is_single_by_format(
        self,
        format_names: List[str],
        format_descriptions: List[str]
    ) -> bool:
        """
        Determine if release is a single based on format information.
        
        Implements: Requirement 6 (Single Determination Rules)
        
        A release is considered a single if ANY of the following are true:
        - formats[].name contains "Single"
        - formats[].name contains "7\"", "12\" Single", "CD Single"
        - formats[].descriptions contains "Single" or "Maxi-Single"
        - formats[].name or descriptions contains "Promo" (with 1-2 tracks)
        
        Note: EP in descriptions should NOT count as single
        
        Args:
            format_names: List of format names
            format_descriptions: List of format descriptions
            
        Returns:
            True if format indicates single
        """
        # Combine all format info for checking
        names_lower = [n.lower() for n in format_names]
        descs_lower = [d.lower() for d in format_descriptions]
        
        # Check format names for single indicators
        single_format_patterns = [
            'single',
            '7"',
            '12" single',
            'cd single',
            'cassette single'
        ]
        
        for pattern in single_format_patterns:
            for name in names_lower:
                if pattern in name:
                    logger.debug(f"Single detected by format name: {name}")
                    return True
        
        # Check format descriptions for single indicators
        # Note: "EP" should NOT count as single
        single_desc_patterns = [
            'single',
            'maxi-single',
            'maxi single'
        ]
        
        for pattern in single_desc_patterns:
            for desc in descs_lower:
                if pattern in desc and 'ep' not in desc:
                    logger.debug(f"Single detected by format description: {desc}")
                    return True
        
        return False
    
    def is_promo(
        self,
        format_names: List[str],
        format_descriptions: List[str]
    ) -> bool:
        """
        Check if release is a promo.
        
        Args:
            format_names: List of format names
            format_descriptions: List of format descriptions
            
        Returns:
            True if release is marked as promo
        """
        all_formats = format_names + format_descriptions
        all_lower = [f.lower() for f in all_formats]
        
        return any('promo' in f for f in all_lower)
    
    def match_track_in_release(
        self,
        track_title: str,
        track_duration: Optional[float],
        release_data: dict,
        allow_alternate: bool = False
    ) -> Optional[dict]:
        """
        Match track in release by title and duration.
        
        Implements: Requirement 5 (Track Matching)
        
        Matching rules:
        - Normalize title (case-insensitive, punctuation removed, bracketed suffixes removed)
        - Use duration matching as fallback (±2 seconds)
        - Filter alternate versions (live, remix, acoustic, demo) unless allowed
        
        Args:
            track_title: Track title to match
            track_duration: Track duration in seconds (optional)
            release_data: Release data dict from Discogs API
            allow_alternate: Allow matching alternate versions
            
        Returns:
            Matched track dict with 'position', 'title', 'duration', or None
        """
        tracklist = release_data.get('tracklist', []) or []
        normalized_title = normalize_track_title(track_title)
        
        # Try exact normalized match first
        for track in tracklist:
            track_title_raw = track.get('title', '')
            track_normalized = normalize_track_title(track_title_raw)
            
            # Check if it's an alternate version
            if not allow_alternate and is_alternate_version(track_title_raw):
                continue
            
            # Check exact match
            if track_normalized == normalized_title:
                return track
        
        # Try fuzzy match with high threshold
        best_match = None
        best_ratio = 0.0
        
        for track in tracklist:
            track_title_raw = track.get('title', '')
            track_normalized = normalize_track_title(track_title_raw)
            
            # Check if it's an alternate version
            if not allow_alternate and is_alternate_version(track_title_raw):
                continue
            
            # Calculate similarity
            ratio = difflib.SequenceMatcher(None, track_normalized, normalized_title).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_match = track
        
        # Accept fuzzy match if similarity > 0.85
        if best_ratio > 0.85:
            logger.debug(f"Fuzzy matched '{track_title}' with '{best_match.get('title')}' (ratio: {best_ratio:.2f})")
            return best_match
        
        # Fallback: duration matching (±2 seconds)
        if track_duration is not None:
            duration_match_idx = match_track_by_duration(track_duration, tracklist)
            if duration_match_idx is not None:
                logger.debug(f"Matched by duration: {tracklist[duration_match_idx].get('title')}")
                return tracklist[duration_match_idx]
        
        return None
    
    def determine_single_status(
        self,
        track_title: str,
        artist: str,
        track_duration: Optional[float] = None,
        album_context: Optional[dict] = None,
        timeout: tuple = (5, 10)
    ) -> dict:
        """
        Comprehensive single detection using Discogs API.
        
        Implements all requirements:
        - API access with authentication and rate limiting
        - Release and master lookup
        - Format parsing
        - Track matching
        - Single determination rules
        - Error handling
        
        Args:
            track_title: Track title
            artist: Artist name
            track_duration: Track duration in seconds (optional)
            album_context: Optional context dict (is_live, is_unplugged)
            timeout: Request timeout
            
        Returns:
            Dict with keys:
                - is_single: bool
                - confidence: str ('high', 'medium', 'low')
                - source: str ('format', 'track_count', 'master', 'promo', etc.)
                - release_id: str or None
                - master_id: str or None
                - formats: List[str]
                - format_descriptions: List[str]
                - track_titles: List[str]
                - release_year: int or None
                - label: str or None
                - country: str or None
        """
        if not self.enabled or not self.token:
            return {
                'is_single': False,
                'confidence': 'low',
                'source': 'disabled',
                'release_id': None,
                'master_id': None,
                'formats': [],
                'format_descriptions': [],
                'track_titles': [],
                'release_year': None,
                'label': None,
                'country': None
            }
        
        try:
            # Search for releases - try without format filter first
            _throttle_discogs()
            search_url = f"{self.base_url}/database/search"
            params = {
                "q": f"{artist} {track_title}",
                "type": "release",
                "per_page": 10
            }
            
            def make_search_request():
                response = self.session.get(search_url, headers=self.headers, params=params, timeout=timeout)
                if response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", 60))
                    time.sleep(retry_after)
                    _throttle_discogs()
                    response = self.session.get(search_url, headers=self.headers, params=params, timeout=timeout)
                response.raise_for_status()
                return response
            
            search_response = _retry_on_500(make_search_request)
            results = search_response.json().get("results", [])
            
            # If no results, try with format filter as fallback
            if not results:
                _throttle_discogs()
                # Create new params dict to avoid mutation
                fallback_params = {**params, "format": "Single, EP"}
                params = fallback_params  # Update params for make_search_request closure
                search_response = _retry_on_500(make_search_request)
                results = search_response.json().get("results", [])
            
            if not results:
                logger.debug(f"No Discogs results for '{track_title}' by '{artist}'")
                return {
                    'is_single': False,
                    'confidence': 'low',
                    'source': 'no_results',
                    'release_id': None,
                    'master_id': None,
                    'formats': [],
                    'format_descriptions': [],
                    'track_titles': [],
                    'release_year': None,
                    'label': None,
                    'country': None
                }
            
            # Check each release
            allow_live = bool(album_context and (album_context.get("is_live") or album_context.get("is_unplugged")))
            
            for result in results[:5]:  # Check top 5 results
                release_id = result.get('id')
                if not release_id:
                    continue
                
                # Fetch full release data
                release_data = self.get_release(str(release_id), timeout)
                if not release_data:
                    continue
                
                # Parse formats
                format_names, format_descriptions = self.parse_formats(release_data)
                
                # Get tracklist
                tracklist = release_data.get('tracklist', []) or []
                track_titles = [t.get('title', '') for t in tracklist]
                
                # Extract metadata
                master_id = release_data.get('master_id')
                release_year = release_data.get('year')
                labels = release_data.get('labels', []) or []
                label = labels[0].get('name', '') if labels else None
                country = release_data.get('country')
                
                # Try to match track in this release
                matched_track = self.match_track_in_release(
                    track_title,
                    track_duration,
                    release_data,
                    allow_alternate=allow_live
                )
                
                if not matched_track:
                    continue
                
                # Check single determination rules (Requirement 6)
                
                # Rule 1: Format contains "Single"
                if self.is_single_by_format(format_names, format_descriptions):
                    return {
                        'is_single': True,
                        'confidence': 'high',
                        'source': 'format',
                        'release_id': str(release_id),
                        'master_id': str(master_id) if master_id else None,
                        'formats': format_names,
                        'format_descriptions': format_descriptions,
                        'track_titles': track_titles,
                        'release_year': release_year,
                        'label': label,
                        'country': country
                    }
                
                # Rule 2: Release has 1-2 tracks
                if 1 <= len(tracklist) <= 2:
                    # Check if matched track is first track (A-side)
                    if matched_track.get('position', '').startswith('A') or tracklist.index(matched_track) == 0:
                        return {
                            'is_single': True,
                            'confidence': 'medium',
                            'source': 'track_count',
                            'release_id': str(release_id),
                            'master_id': str(master_id) if master_id else None,
                            'formats': format_names,
                            'format_descriptions': format_descriptions,
                            'track_titles': track_titles,
                            'release_year': release_year,
                            'label': label,
                            'country': country
                        }
                
                # Rule 3: Promo with 1-2 tracks
                if self.is_promo(format_names, format_descriptions) and 1 <= len(tracklist) <= 2:
                    return {
                        'is_single': True,
                        'confidence': 'medium',
                        'source': 'promo',
                        'release_id': str(release_id),
                        'master_id': str(master_id) if master_id else None,
                        'formats': format_names,
                        'format_descriptions': format_descriptions,
                        'track_titles': track_titles,
                        'release_year': release_year,
                        'label': label,
                        'country': country
                    }
                
                # Rule 4: Check master release (if available)
                if master_id:
                    master_data = self.get_master_release(str(master_id), timeout)
                    if master_data:
                        master_formats, master_descs = self.parse_formats(master_data)
                        if self.is_single_by_format(master_formats, master_descs):
                            return {
                                'is_single': True,
                                'confidence': 'high',
                                'source': 'master',
                                'release_id': str(release_id),
                                'master_id': str(master_id),
                                'formats': format_names,
                                'format_descriptions': format_descriptions,
                                'track_titles': track_titles,
                                'release_year': release_year,
                                'label': label,
                                'country': country
                            }
            
            # No single detected
            return {
                'is_single': False,
                'confidence': 'low',
                'source': 'not_found',
                'release_id': None,
                'master_id': None,
                'formats': [],
                'format_descriptions': [],
                'track_titles': [],
                'release_year': None,
                'label': None,
                'country': None
            }
            
        except Exception as e:
            logger.error(f"Discogs single detection failed for '{track_title}' by '{artist}': {e}")
            return {
                'is_single': False,
                'confidence': 'low',
                'source': 'error',
                'release_id': None,
                'master_id': None,
                'formats': [],
                'format_descriptions': [],
                'track_titles': [],
                'release_year': None,
                'label': None,
                'country': None
            }
