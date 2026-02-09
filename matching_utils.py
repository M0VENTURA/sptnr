"""
Shared Track Matching Utilities
=================================

This module provides robust track matching logic used across the codebase:
- Playlist matching (playlist_matcher.py)
- Single detection (single_detection_enhanced.py, helpers.py)
- Popularity scanning (popularity.py)

Implements a 3-tier matching strategy inspired by navispot:
1. ISRC matching (perfect confidence: 1.0)
2. Fuzzy matching with weighted components (threshold: 0.80)
3. Strict normalized matching (high confidence: 0.95)

Key Features:
- Full Unicode normalization with accent removal
- Collaboration handling (feat., ft., with, &, etc.)
- Weighted component scoring (title 35%, artist 25%, duration 25%, album 15%)
- Graduated duration matching with penalties
"""

import unicodedata
import re
import logging
from typing import Optional, Dict, Tuple

logger = logging.getLogger(__name__)

# Matching confidence thresholds
MAX_FUZZY_SCORE = 0.95  # Maximum score cap for fuzzy matching to avoid false confidence
STRICT_MATCH_SCORE = 0.95  # Confidence score for exact normalized matches
ISRC_MATCH_SCORE = 1.0  # Perfect confidence for ISRC matches
FUZZY_THRESHOLD = 0.80  # Minimum score for fuzzy matching to be accepted

# Roman numeral pattern for title suffix preservation
# Matches Roman numerals I-XX at the end of titles
# Used to distinguish tracks like "Song II" from "Song III"
ROMAN_NUMERAL_PATTERN = r'\s+(I{1,3}|IV|V|VI{0,3}|IX|X{1,3}|XI{0,3}|XIV|XV|XVI{0,3}|XIX|XX)\s*$'

# Punctuation suffix pattern for title preservation
# Matches trailing punctuation (!, +, ?) at the end of titles
# Used to distinguish tracks like "Lost!" from "Lost+"
PUNCTUATION_SUFFIX_PATTERN = r'([!+?]+)\s*$'


def normalize_string(text: str) -> str:
    """
    Normalize text for comparison: lowercase, remove accents, clean punctuation.
    
    IMPORTANT: Preserves trailing punctuation (!, +, ?) to distinguish different songs
    like "Lost!" vs "Lost+".
    
    This is the base normalization used for all text comparisons.
    
    Args:
        text: Input string to normalize
        
    Returns:
        Normalized string
    """
    if not text:
        return ""
    
    # Preserve trailing punctuation suffixes (!, +, ?) before normalization
    preserved_suffix = ""
    suffix_match = re.search(PUNCTUATION_SUFFIX_PATTERN, text)
    if suffix_match:
        preserved_suffix = suffix_match.group(1)
        text = text[:suffix_match.start()]
    
    # Remove accents using Unicode NFD decomposition
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    
    # Lowercase
    text = text.lower()
    
    # Remove special characters, keep only alphanumeric and spaces
    text = re.sub(r"[^\w\s]", " ", text)
    
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text)
    
    # Re-attach preserved suffix
    result = text.strip()
    if preserved_suffix:
        result = result + preserved_suffix
    
    return result


def normalize_title(title: str) -> str:
    """
    Normalize track title with special handling for versions and live recordings.
    
    IMPORTANT: Preserves title suffixes like "!", "+", "?", and Roman numerals (I, II, III, etc.)
    to ensure different songs are not matched as the same track.
    
    Removes:
    - Live indicators: (live), - live, [live], live$
    - Version/remix/remaster tags in parentheses/brackets
    - Keywords: remaster, remastered, deluxe, edit, mix, version, bonus
    
    Preserves:
    - Trailing punctuation: "Lost!" vs "Lost+"
    - Roman numerals: "Song II" vs "Song III"
    
    Args:
        title: Track title to normalize
        
    Returns:
        Normalized title string
    """
    if not title:
        return ""
    
    # Preserve Roman numerals at the end (I, II, III, IV, V, etc.) before normalization
    # Match space + Roman numeral at the end of the title
    roman_suffix = ""
    roman_match = re.search(ROMAN_NUMERAL_PATTERN, title, re.IGNORECASE)
    if roman_match:
        roman_suffix = " " + roman_match.group(1).lower()  # Preserve as lowercase
        title = title[:roman_match.start()]
    
    # Remove live indicators
    live_patterns = [
        r"\(live\)",
        r"\- live",
        r"\[live\]",
        r" live$",
    ]
    for pattern in live_patterns:
        title = re.sub(pattern, "", title, flags=re.IGNORECASE)
    
    # Remove version/remix/remaster tags
    title = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", title)
    title = re.sub(
        r"(?i)\b(remaster(?:ed)?\s*\d{0,4}|deluxe|edit|mix|version|bonus)\b",
        " ",
        title
    )
    
    # Normalize the base title (this will also preserve trailing punctuation)
    result = normalize_string(title)
    
    # Re-attach Roman numeral suffix if present
    if roman_suffix:
        result = result + roman_suffix
    
    return result


def normalize_artist(artist: str) -> str:
    """
    Normalize artist name, handling collaborations.
    
    Removes collaboration indicators:
    - feat., ft.
    - with
    - x, X (as in "Artist A x Artist B")
    - &
    - and
    
    Args:
        artist: Artist name to normalize
        
    Returns:
        Normalized artist string
    """
    if not artist:
        return ""
    
    # Remove collaboration indicators
    collab_patterns = [
        r"\bfeat\.?",
        r"\bft\.?",
        r"\bwith\b",
        r" x ",
        r" X ",
        r" & ",
        r" and ",
    ]
    for pattern in collab_patterns:
        artist = re.sub(pattern, " ", artist, flags=re.IGNORECASE)
    
    return normalize_string(artist)


def levenshtein_distance(s1: str, s2: str) -> int:
    """
    Calculate Levenshtein distance between two strings.
    
    The Levenshtein distance is the minimum number of single-character edits
    (insertions, deletions, or substitutions) required to change one string
    into the other.
    
    Args:
        s1: First string
        s2: Second string
        
    Returns:
        Integer distance between the strings
    """
    if len(s1) < len(s2):
        return levenshtein_distance(s2, s1)
    
    if len(s2) == 0:
        return len(s1)
    
    previous_row = range(len(s2) + 1)
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row
    
    return previous_row[-1]


def calculate_similarity(s1: str, s2: str) -> float:
    """
    Calculate similarity score between two strings (0.0 to 1.0).
    
    Uses Levenshtein distance normalized by maximum string length.
    
    Args:
        s1: First string
        s2: Second string
        
    Returns:
        Similarity score from 0.0 (completely different) to 1.0 (identical)
    """
    if not s1 or not s2:
        return 0.0
    
    normalized1 = normalize_string(s1)
    normalized2 = normalize_string(s2)
    
    if normalized1 == normalized2:
        return 1.0
    
    max_length = max(len(normalized1), len(normalized2))
    if max_length == 0:
        return 1.0
    
    distance = levenshtein_distance(normalized1, normalized2)
    return 1.0 - (distance / max_length)


def calculate_duration_similarity(duration1: float, duration2: float) -> float:
    """
    Calculate duration similarity score.
    
    Based on navispot's duration matching with 3-second threshold:
    - Within 3 seconds: 0.90 to 1.0
    - Beyond 3 seconds: linear penalty up to 1 minute
    
    Args:
        duration1: Duration in seconds (or milliseconds if > 1000)
        duration2: Duration in seconds (or milliseconds if > 1000)
        
    Returns:
        Similarity score from 0.0 to 1.0
    """
    if not duration1 or not duration2:
        return 0.5  # Neutral if duration not available
    
    # Auto-detect if values are in milliseconds
    if duration1 > 1000:
        duration1 = duration1 / 1000
    if duration2 > 1000:
        duration2 = duration2 / 1000
    
    diff_seconds = abs(duration1 - duration2)
    
    # Within 3 seconds = very high confidence
    if diff_seconds < 3:
        return 1.0 - (diff_seconds / 3) * 0.1  # 0.90 to 1.0
    
    # Beyond 3 seconds, penalize
    penalty = min(diff_seconds / 60, 1.0)  # Penalty up to 1 minute
    return max(1.0 - penalty, 0.0)


def calculate_track_similarity(
    track1: Dict,
    track2: Dict,
    title_key1: str = "title",
    title_key2: str = "title",
    artist_key1: str = "artist",
    artist_key2: str = "artist",
    album_key1: str = "album",
    album_key2: str = "album",
    duration_key1: str = "duration",
    duration_key2: str = "duration"
) -> Tuple[float, Dict[str, float]]:
    """
    Calculate overall track similarity using weighted components.
    
    Weighted combination:
    - Title: 35%
    - Artist: 25%
    - Duration: 25%
    - Album: 15%
    
    Includes boosters:
    - Perfect title match + artist >= 0.3: boost to 0.85+
    - Duration match >= 0.9: +0.1 boost
    - Album match >= 0.8 with decent title/artist: +0.05 boost
    
    Args:
        track1: First track dictionary
        track2: Second track dictionary
        title_key1: Key for title in track1 (default: "title")
        title_key2: Key for title in track2 (default: "title")
        artist_key1: Key for artist in track1 (default: "artist")
        artist_key2: Key for artist in track2 (default: "artist")
        album_key1: Key for album in track1 (default: "album")
        album_key2: Key for album in track2 (default: "album")
        duration_key1: Key for duration in track1 (default: "duration")
        duration_key2: Key for duration in track2 (default: "duration")
        
    Returns:
        Tuple of (overall_score, component_scores_dict)
    """
    # Calculate individual components
    title_sim = calculate_similarity(
        normalize_title(track1.get(title_key1, "")),
        normalize_title(track2.get(title_key2, ""))
    )
    
    artist_sim = calculate_similarity(
        normalize_artist(track1.get(artist_key1, "")),
        normalize_artist(track2.get(artist_key2, ""))
    )
    
    album_sim = calculate_similarity(
        normalize_string(track1.get(album_key1, "")),
        normalize_string(track2.get(album_key2, ""))
    )
    
    duration_sim = calculate_duration_similarity(
        track1.get(duration_key1, 0),
        track2.get(duration_key2, 0)
    )
    
    # Weighted combination (based on navispot's algorithm)
    base_score = (
        title_sim * 0.35 +
        artist_sim * 0.25 +
        duration_sim * 0.25 +
        album_sim * 0.15
    )
    
    # Boost for perfect title match
    if title_sim == 1.0:
        if artist_sim >= 0.3:
            base_score = max(base_score, 0.85)
        else:
            base_score = max(base_score, 0.75)
    
    # Boost for good duration match
    if duration_sim >= 0.9:
        base_score = min(base_score + 0.1, MAX_FUZZY_SCORE)
    
    # Boost for album context
    if album_sim >= 0.8 and (title_sim >= 0.6 or artist_sim >= 0.4):
        base_score = min(base_score + 0.05, MAX_FUZZY_SCORE)
    
    components = {
        "title": round(title_sim, 3),
        "artist": round(artist_sim, 3),
        "album": round(album_sim, 3),
        "duration": round(duration_sim, 3)
    }
    
    return round(base_score, 3), components


def matches_by_isrc(isrc1: Optional[str], isrc2: Optional[str]) -> bool:
    """
    Check if two ISRCs match.
    
    Args:
        isrc1: First ISRC code
        isrc2: Second ISRC code
        
    Returns:
        True if both ISRCs exist and match
    """
    if not isrc1 or not isrc2:
        return False
    
    return isrc1.strip().upper() == isrc2.strip().upper()


def is_fuzzy_match(
    track1: Dict,
    track2: Dict,
    threshold: float = FUZZY_THRESHOLD,
    **kwargs
) -> Tuple[bool, float, Dict[str, float]]:
    """
    Check if two tracks match using fuzzy matching.
    
    Args:
        track1: First track dictionary
        track2: Second track dictionary
        threshold: Minimum similarity score to consider a match (default: 0.80)
        **kwargs: Additional arguments to pass to calculate_track_similarity()
        
    Returns:
        Tuple of (is_match, score, component_scores)
    """
    score, components = calculate_track_similarity(track1, track2, **kwargs)
    return (score >= threshold, score, components)


def is_strict_match(
    track1: Dict,
    track2: Dict,
    title_key1: str = "title",
    title_key2: str = "title",
    artist_key1: str = "artist",
    artist_key2: str = "artist"
) -> bool:
    """
    Check if two tracks match using strict normalized comparison.
    
    Args:
        track1: First track dictionary
        track2: Second track dictionary
        title_key1: Key for title in track1 (default: "title")
        title_key2: Key for title in track2 (default: "title")
        artist_key1: Key for artist in track1 (default: "artist")
        artist_key2: Key for artist in track2 (default: "artist")
        
    Returns:
        True if normalized title and artist match exactly
    """
    norm_title1 = normalize_title(track1.get(title_key1, ""))
    norm_title2 = normalize_title(track2.get(title_key2, ""))
    
    norm_artist1 = normalize_artist(track1.get(artist_key1, ""))
    norm_artist2 = normalize_artist(track2.get(artist_key2, ""))
    
    return norm_title1 == norm_title2 and norm_artist1 == norm_artist2
