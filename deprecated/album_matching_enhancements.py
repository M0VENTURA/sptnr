#!/usr/bin/env python3
"""
Album Matching Enhancements
============================

Provides enhanced album matching logic for special cases:

1. Deluxe/Remastered/Rereleased album fallback matching
2. Time window validation for live/symphony/acoustic albums
"""

import re
import logging
from typing import Optional, Dict, List, Tuple
from datetime import datetime

# Import centralized logging
from logging_config import log_debug, log_info

logger = logging.getLogger(__name__)


def normalize_album_for_fallback(album_name: str) -> str:
    """
    Normalize album name by removing Deluxe/Remastered/Rereleased markers.
    
    This allows matching special editions with their original releases.
    
    Args:
        album_name: Album title
        
    Returns:
        Normalized album name without edition markers
    """
    if not album_name:
        return ""
    
    # Patterns to remove - these indicate special editions
    patterns_to_remove = [
        r'\(Deluxe\s*(?:Edition|Version)?\)',
        r'\(Remastered\s*(?:Edition|Version)?\)',
        r'\(Rerelease[d]?\s*(?:Edition|Version)?\)',
        r'\(Rereleased\s*(?:Edition|Version)?\)',
        r'\s+-\s+Deluxe\s*(?:Edition|Version)?',
        r'\s+-\s+Remastered\s*(?:Edition|Version)?',
        r'\s+-\s+Rerelease[d]?\s*(?:Edition|Version)?',
        # Also handle cases without parentheses at the end
        r'\s+Deluxe\s*(?:Edition|Version)?$',
        r'\s+Remastered\s*(?:Edition|Version)?$',
        r'\s+Rerelease[d]?\s*(?:Edition|Version)?$',
    ]
    
    normalized = album_name
    for pattern in patterns_to_remove:
        normalized = re.sub(pattern, '', normalized, flags=re.IGNORECASE)
    
    # Clean up any trailing/leading whitespace or empty parentheses
    normalized = re.sub(r'\(\s*\)', '', normalized)
    normalized = normalized.strip()
    
    return normalized


def is_special_album_type(album_name: str) -> bool:
    """
    Check if album is a live/symphony/symphonic/acoustic/unplugged album.
    
    These albums require time window validation for single matching.
    
    Args:
        album_name: Album title
        
    Returns:
        True if album is a special type requiring time window validation
    """
    if not album_name:
        return False
    
    album_lower = album_name.lower()
    
    special_keywords = [
        'live',
        'symphony',
        'symphonic',
        'acoustic',
        'unplugged'
    ]
    
    return any(keyword in album_lower for keyword in special_keywords)


def extract_year_from_date(date_str: Optional[str]) -> Optional[int]:
    """
    Extract year from a date string.
    
    Supports formats:
    - YYYY-MM-DD
    - YYYY-MM
    - YYYY
    
    Args:
        date_str: Date string
        
    Returns:
        Year as integer or None
    """
    if not date_str:
        return None
    
    # Try to extract year from various formats
    # Format: YYYY-MM-DD or YYYY-MM or YYYY
    match = re.match(r'(\d{4})', str(date_str))
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            pass
    
    return None


def is_within_time_window(
    track_release_year: Optional[int],
    album_release_year: Optional[int],
    window_years: int = 1
) -> bool:
    """
    Check if track release year is within window of album release year.
    
    Args:
        track_release_year: Year the track was released
        album_release_year: Year the album was released
        window_years: Number of years window (default: 1 for ±1 year)
        
    Returns:
        True if within window or if either year is unknown
    """
    # If either year is unknown, allow the match
    if track_release_year is None or album_release_year is None:
        log_debug("Time window check: One or both years unknown, allowing match")
        return True
    
    # Check if within the window
    year_diff = abs(track_release_year - album_release_year)
    is_within = year_diff <= window_years
    
    if is_within:
        log_debug(f"Time window check: Track year {track_release_year} within ±{window_years} of album year {album_release_year}")
    else:
        log_debug(f"Time window check: Track year {track_release_year} NOT within ±{window_years} of album year {album_release_year} (diff: {year_diff})")
    
    return is_within


def should_apply_time_window_restriction(
    album_name: str,
    track_release_date: Optional[str],
    album_release_date: Optional[str]
) -> Tuple[bool, bool]:
    """
    Determine if time window restriction should be applied to single matching.
    
    Returns:
        Tuple of (should_restrict: bool, is_within_window: bool)
        - should_restrict: True if this is a special album requiring time window
        - is_within_window: True if the track is within the allowed window
    """
    # Check if this is a special album type
    if not is_special_album_type(album_name):
        return False, True  # Not a special album, no restriction
    
    # Extract years
    track_year = extract_year_from_date(track_release_date)
    album_year = extract_year_from_date(album_release_date)
    
    # Apply time window check
    is_within = is_within_time_window(track_year, album_year, window_years=1)
    
    return True, is_within


def match_album_with_fallback(
    search_album: str,
    candidate_albums: List[str]
) -> Optional[str]:
    """
    Match album with fallback to original version.
    
    If exact match fails and search_album has Deluxe/Remastered/Rereleased,
    try matching with the normalized version.
    
    Args:
        search_album: Album to search for
        candidate_albums: List of candidate albums to match against
        
    Returns:
        Matched album name or None
    """
    # Try exact match first
    for candidate in candidate_albums:
        if search_album.lower().strip() == candidate.lower().strip():
            log_debug(f"Album match: Exact match found for '{search_album}'")
            return candidate
    
    # If no exact match and search_album has edition markers, try fallback
    normalized_search = normalize_album_for_fallback(search_album)
    
    # Only use fallback if the normalized version is different
    if normalized_search.lower() != search_album.lower():
        log_info(f"Album match: No exact match for '{search_album}', trying fallback to '{normalized_search}'")
        
        for candidate in candidate_albums:
            normalized_candidate = normalize_album_for_fallback(candidate)
            if normalized_search.lower().strip() == normalized_candidate.lower().strip():
                log_info(f"Album match: Fallback match found - '{search_album}' matched with '{candidate}'")
                return candidate
    
    log_debug(f"Album match: No match found for '{search_album}'")
    return None
