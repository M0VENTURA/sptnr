"""
Enhanced playlist track matching logic inspired by navispot.
Implements a 3-tier matching strategy: ISRC → Fuzzy → Strict
"""

import difflib
import unicodedata
import re
import logging
from typing import Optional, Dict, List, Tuple

logger = logging.getLogger(__name__)


def normalize_string(text: str) -> str:
    """
    Normalize text for comparison: lowercase, remove accents, clean punctuation.
    Based on navispot's normalization strategy.
    """
    if not text:
        return ""
    
    # Remove accents
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    
    # Lowercase
    text = text.lower()
    
    # Remove special characters, keep only alphanumeric and spaces
    text = re.sub(r"[^\w\s]", " ", text)
    
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text)
    
    return text.strip()


def normalize_title(title: str) -> str:
    """
    Normalize track title with special handling for versions and live recordings.
    Based on navispot's title normalization.
    """
    if not title:
        return ""
    
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
    
    return normalize_string(title)


def normalize_artist(artist: str) -> str:
    """
    Normalize artist name, handling collaborations.
    Based on navispot's artist normalization.
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
    """Calculate Levenshtein distance between two strings."""
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
    """Calculate similarity score between two strings (0.0 to 1.0)."""
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


def calculate_duration_similarity(spotify_duration_ms: int, navidrome_duration_seconds: float) -> float:
    """
    Calculate duration similarity score.
    Based on navispot's duration matching with 3-second threshold.
    """
    if not spotify_duration_ms or not navidrome_duration_seconds:
        return 0.5  # Neutral if duration not available
    
    navidrome_duration_ms = navidrome_duration_seconds * 1000
    diff_ms = abs(spotify_duration_ms - navidrome_duration_ms)
    
    # Within 3 seconds = very high confidence
    if diff_ms < 3000:
        return 1.0 - (diff_ms / 3000) * 0.1  # 0.90 to 1.0
    
    # Beyond 3 seconds, penalize
    penalty = min(diff_ms / 60000, 1.0)  # Penalty up to 1 minute
    return max(1.0 - penalty, 0.0)


def calculate_track_similarity(
    spotify_track: Dict,
    navidrome_track: Dict
) -> Tuple[float, Dict[str, float]]:
    """
    Calculate overall track similarity using weighted components.
    
    Returns:
        (overall_score, component_scores)
    """
    # Calculate individual components
    title_sim = calculate_similarity(
        normalize_title(spotify_track.get("title", "")),
        normalize_title(navidrome_track.get("title", ""))
    )
    
    artist_sim = calculate_similarity(
        normalize_artist(spotify_track.get("artist", "")),
        normalize_artist(navidrome_track.get("artist", ""))
    )
    
    album_sim = calculate_similarity(
        normalize_string(spotify_track.get("album", "")),
        normalize_string(navidrome_track.get("album", ""))
    )
    
    duration_sim = calculate_duration_similarity(
        spotify_track.get("duration_ms", 0),
        navidrome_track.get("duration", 0)
    )
    
    # Weighted combination (based on navispot's algorithm)
    # Title and artist are most important
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
        base_score = min(base_score + 0.1, 0.95)
    
    # Boost for album context
    if album_sim >= 0.8 and (title_sim >= 0.6 or artist_sim >= 0.4):
        base_score = min(base_score + 0.05, 0.95)
    
    components = {
        "title": round(title_sim, 3),
        "artist": round(artist_sim, 3),
        "album": round(album_sim, 3),
        "duration": round(duration_sim, 3)
    }
    
    return round(base_score, 3), components


def match_by_isrc(
    spotify_track: Dict,
    cursor,
    logger=logger
) -> Optional[Tuple[Dict, float, str]]:
    """
    Match track by ISRC (International Standard Recording Code).
    This is the most reliable matching method.
    
    Returns:
        (matched_track_dict, confidence_score, match_strategy) or None
    """
    isrc = spotify_track.get("isrc", "").strip()
    
    if not isrc:
        return None
    
    # Try exact ISRC match
    cursor.execute("""
        SELECT id, title, artist, album, stars, duration, isrc
        FROM tracks
        WHERE isrc = ?
        LIMIT 1
    """, (isrc,))
    
    row = cursor.fetchone()
    
    if row:
        logger.debug(f"✅ ISRC match: {spotify_track.get('title')} -> {row['title']}")
        return (
            {
                "id": row["id"],
                "title": row["title"],
                "artist": row["artist"],
                "album": row["album"],
                "stars": row["stars"],
                "duration": row.get("duration", 0)
            },
            1.0,  # Perfect confidence for ISRC match
            "isrc"
        )
    
    return None


def match_by_fuzzy(
    spotify_track: Dict,
    cursor,
    threshold: float = 0.80,
    max_candidates: int = 250,
    logger=logger
) -> Optional[Tuple[Dict, float, str]]:
    """
    Match track using fuzzy matching with improved normalization.
    Based on navispot's fuzzy matching algorithm.
    
    Returns:
        (matched_track_dict, confidence_score, match_strategy) or None
    """
    raw_title = spotify_track.get("title", "").strip()
    raw_artist = spotify_track.get("artist", "").strip()
    
    if not raw_title or not raw_artist:
        return None
    
    # Get primary artist (before comma)
    primary_artist = raw_artist.split(",")[0].strip()
    
    # Search for candidates using LIKE
    title_like = f"%{raw_title.lower()}%"
    artist_like = f"%{primary_artist.lower()}%"
    
    cursor.execute("""
        SELECT id, title, artist, album, stars, duration, isrc
        FROM tracks
        WHERE LOWER(title) LIKE ? OR LOWER(artist) LIKE ?
        LIMIT ?
    """, (title_like, artist_like, max_candidates))
    
    candidates = cursor.fetchall()
    
    # If no candidates, try first word of title
    if not candidates and raw_title:
        first_word = normalize_title(raw_title).split()[0] if normalize_title(raw_title) else ""
        if first_word:
            cursor.execute("""
                SELECT id, title, artist, album, stars, duration, isrc
                FROM tracks
                WHERE LOWER(title) LIKE ?
                LIMIT ?
            """, (f"%{first_word}%", max_candidates))
            candidates = cursor.fetchall()
    
    if not candidates:
        return None
    
    # Score each candidate
    best_match = None
    best_score = 0.0
    
    for row in candidates:
        navidrome_track = {
            "title": row["title"],
            "artist": row["artist"],
            "album": row["album"],
            "duration": row.get("duration", 0)
        }
        
        score, components = calculate_track_similarity(spotify_track, navidrome_track)
        
        if score > best_score:
            best_score = score
            best_match = {
                "id": row["id"],
                "title": row["title"],
                "artist": row["artist"],
                "album": row["album"],
                "stars": row["stars"],
                "duration": row.get("duration", 0),
                "components": components
            }
    
    # Accept match if above threshold
    if best_match and best_score >= threshold:
        logger.debug(f"✅ Fuzzy match ({best_score:.3f}): {spotify_track.get('title')} -> {best_match['title']}")
        return (best_match, best_score, "fuzzy")
    
    return None


def match_by_strict(
    spotify_track: Dict,
    cursor,
    logger=logger
) -> Optional[Tuple[Dict, float, str]]:
    """
    Match track using strict normalized string matching.
    Fallback method when fuzzy matching doesn't find confident matches.
    
    Returns:
        (matched_track_dict, confidence_score, match_strategy) or None
    """
    norm_title = normalize_title(spotify_track.get("title", ""))
    norm_artist = normalize_artist(spotify_track.get("artist", ""))
    
    if not norm_title or not norm_artist:
        return None
    
    # Try to find exact normalized match
    cursor.execute("""
        SELECT id, title, artist, album, stars, duration, isrc
        FROM tracks
    """)
    
    for row in cursor.fetchall():
        candidate_title = normalize_title(row["title"])
        candidate_artist = normalize_artist(row["artist"])
        
        if candidate_title == norm_title and candidate_artist == norm_artist:
            logger.debug(f"✅ Strict match: {spotify_track.get('title')} -> {row['title']}")
            return (
                {
                    "id": row["id"],
                    "title": row["title"],
                    "artist": row["artist"],
                    "album": row["album"],
                    "stars": row["stars"],
                    "duration": row.get("duration", 0)
                },
                0.95,  # High confidence for strict match
                "strict"
            )
    
    return None


def match_track(
    spotify_track: Dict,
    cursor,
    enable_isrc: bool = True,
    enable_fuzzy: bool = True,
    enable_strict: bool = True,
    fuzzy_threshold: float = 0.80,
    logger=logger
) -> Tuple[Optional[Dict], float, str]:
    """
    Match a Spotify track to Navidrome database using 3-tier strategy.
    
    Strategy order (as per navispot):
    1. ISRC - Most reliable, exact match
    2. Fuzzy - Similarity-based with 0.80 threshold
    3. Strict - Exact normalized text match
    
    Returns:
        (matched_track_dict, confidence_score, match_strategy)
        If no match: (None, 0.0, "unmatched")
    """
    
    # Try ISRC match first
    if enable_isrc:
        result = match_by_isrc(spotify_track, cursor, logger)
        if result:
            return result
    
    # Try fuzzy match
    if enable_fuzzy:
        result = match_by_fuzzy(spotify_track, cursor, fuzzy_threshold, logger=logger)
        if result:
            return result
    
    # Try strict match
    if enable_strict:
        result = match_by_strict(spotify_track, cursor, logger)
        if result:
            return result
    
    # No match found
    return (None, 0.0, "unmatched")
