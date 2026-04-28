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
- RapidFuzz-backed string similarity (C++ speed)
- Multi-candidate title generation from filenames (inspired by Soulmate)
- Track-number guard to prevent cross-track false positives (inspired by SeekDownloader)
- Per-component minimum thresholds for title and artist
- Version-stripped title fallback scoring
"""

import unicodedata
import re
import logging
from typing import List, Optional, Dict, Tuple

try:
    from rapidfuzz import fuzz as _fuzz
    _HAVE_RAPIDFUZZ = True
except ImportError:  # pragma: no cover – fallback for environments without rapidfuzz
    _HAVE_RAPIDFUZZ = False

try:
    from helpers.config_loader import load_config
except ImportError:
    def load_config():  # Fallback if config_loader not available
        return {}

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


def strip_search_parentheses(title: str) -> str:
    """
    Strip parenthetical/bracketed suffixes for search queries.
    
    This function removes qualifiers from track titles that are search-interfering:
    - Remastered versions: "(Remastered)", "(2024 Remaster)", "(Remastered 2020)"
    - Radio edits: "(Radio Edit)", "(Radio Mix)"
    - Single versions: "(Single Version)", "(Album Version)"
    - Other edition markers: "(Deluxe)", "(Extended)", "(Live)"
    
    Purpose:
    When searching for a track like "Song (Remastered)" on MusicBrainz/Spotify,
    we want to find the original "Song" track, not duplicate/edit variants.
    This ensures accurate metadata fetching for popularity  and writer credit lookups.
    
    IMPORTANT: This is specifically for SEARCH normalization, not for matching/comparison.
    It returns the normalized search term that should be used when querying APIs.
    
    Examples:
        "Love Song (Remastered)" -> "Love Song"
        "Track (Radio Edit)" -> "Track"
        "Song (2024 Remastered Edition)" -> "Song"
        "Artist Name (Single Version)" -> "Artist Name"
        "Album (Deluxe Edition)" -> "Album"
        "Song - Remastered 2020" -> "Song"
        "Song [Live]" -> "Song"
        "Song" -> "Song"  # No change if no parentheses
    
    Args:
        title: Track or album title that may contain parenthetical edition markers
        
    Returns:
        Simplified title without parenthetical edition markers, ready for API searches
    """
    if not title:
        return ""
    
    # Get custom keywords from config if available, otherwise use defaults
    try:
        config = load_config()
        SEARCH_STRIP_KEYWORDS = tuple(config.get('strip_parentheses_filters', []))
        if not SEARCH_STRIP_KEYWORDS:
            # Fall back to defaults if config is empty
            # Only includes terms for "same song, different cut" - not alternate versions
            SEARCH_STRIP_KEYWORDS = (
                'remaster', 'remastered',           # Remaster versions like (Remastered), (2024 Remaster)
                'radio edit', 'radio mix',          # Radio edits like (Radio Edit), (Radio Mix)
                'single version', 'album version',  # Version markers like (Single Version), (Album Version)
                'deluxe', 'deluxe edition',         # Deluxe editions
                'extended', 'extended edition',     # Extended editions
                'expanded', 'expanded edition',     # Expanded editions
                'edition',                          # Generic edition marker
            )
    except Exception:
        # Fall back to defaults if config loading fails
        # Only includes terms for "same song, different cut" - not alternate versions
        SEARCH_STRIP_KEYWORDS = (
            'remaster', 'remastered',           # Remaster versions like (Remastered), (2024 Remaster)
            'radio edit', 'radio mix',          # Radio edits like (Radio Edit), (Radio Mix)
            'single version', 'album version',  # Version markers like (Single Version), (Album Version)
            'deluxe', 'deluxe edition',         # Deluxe editions
            'extended', 'extended edition',     # Extended editions
            'expanded', 'expanded edition',     # Expanded editions
            'edition',                          # Generic edition marker
        )
    
    # Remove content in parentheses/brackets if it contains any search-strip keyword
    # Pattern: (anything with keyword) or [anything with keyword]
    def should_remove_parens(match):
        content = match.group(0)  # The entire matched parenthetical
        content_lower = content.lower()
        # Check if any keyword is in this parenthetical
        for keyword in SEARCH_STRIP_KEYWORDS:
            if keyword in content_lower:
                return ""  # Remove this parenthetical
        return content  # Keep it if no keywords match
    
    # Match parentheses/brackets and their contents, but keep some if no keywords detected
    result = re.sub(r'\([^)]*\)|\[[^\]]*\]', should_remove_parens, title, flags=re.IGNORECASE)
    
    # Also handle dash-separated suffixes like "- Remastered 2020", "- Radio Edit"
    # Pattern: dash + optional whitespace + keyword + anything until end
    for keyword in SEARCH_STRIP_KEYWORDS:
        result = re.sub(r'\s*-\s*' + re.escape(keyword) + r'.*$', '', result, flags=re.IGNORECASE)
    
    # Clean up any trailing/multiple spaces
    result = re.sub(r'\s+', ' ', result).strip()
    
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


def normalize_album(album: str) -> str:
    """
    Normalize album name for search and matching purposes.
    
    Removes version/edition suffixes to help match albums like:
    - "Helix (2021 version)" -> "Helix"
    - "Album Name (Deluxe Edition)" -> "Album Name"
    - "Greatest Hits - Remastered" -> "Greatest Hits"
    
    This is useful for searching Spotify/MusicBrainz where the exact album
    version may differ from what's in the local library.
    
    Removes:
    - Year suffixes in parentheses: (2021 version), (2020 remaster)
    - Edition suffixes: deluxe, expanded, reissue, anniversary, special edition
    - Remaster indicators: remaster, remastered
    - Content in parentheses/brackets after removing specific patterns
    
    Args:
        album: Album name to normalize
        
    Returns:
        Normalized album string
    """
    if not album:
        return ""
    
    # Remove year-based version suffixes like "(2021 version)", "(2020 remaster)"
    # Pattern: ( + optional text + 4 digits + optional text + )
    # This catches patterns like "(2021 version)", "(10th Anniversary 2020)", etc.
    album = re.sub(r'\([^)]*\d{4}[^)]*\)', '', album)
    
    # Remove standalone anniversary editions (catches patterns like "10th Anniversary Edition")
    album = re.sub(r'\([^)]*\d+(?:st|nd|rd|th)\s+anniversary[^)]*\)', '', album, flags=re.IGNORECASE)
    
    # Remove edition/version keywords and their surrounding context
    # These patterns match both standalone and in parentheses/brackets
    # Only remove if they're in parentheses/brackets or after dash to avoid false positives
    edition_patterns = [
        # Parentheses-based patterns
        r'\(\s*deluxe\s*(?:edition|version)?\s*\)',
        r'\(\s*expanded\s*(?:edition|version)?\s*\)',
        r'\(\s*reissue\s*\)',
        r'\(\s*anniversary\s*(?:edition)?\s*\)',
        r'\(\s*special\s*edition\s*\)',
        r'\(\s*extended\s*edition\s*\)',
        r'\(\s*tour\s*edition\s*\)',
        r'\(\s*limited\s*edition\s*\)',
        r'\(\s*collector\'?s?\s*edition\s*\)',
        r'\(\s*remaster(?:ed)?\s*\)',
        r'\(\s*bonus\s*(?:tracks?|edition)?\s*\)',
        # Dash-based patterns (only after dash to avoid removing album names like "The Deluxe")
        r'\s+-\s+deluxe(?:\s+edition)?',
        r'\s+-\s+remaster(?:ed)?',
        r'\s+-\s+expanded(?:\s+edition)?',
        r'\s+-\s+special\s+edition',
    ]
    
    for pattern in edition_patterns:
        album = re.sub(pattern, '', album, flags=re.IGNORECASE)
    
    # Remove any remaining empty parentheses or brackets
    album = re.sub(r'\s*\(\s*\)\s*', ' ', album)
    album = re.sub(r'\s*\[\s*\]\s*', ' ', album)
    
    # Use base normalization to clean up
    return normalize_string(album)


def levenshtein_distance(s1: str, s2: str) -> int:
    """
    Calculate Levenshtein distance between two strings.

    Pure-Python fallback used only when rapidfuzz is unavailable.

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
    Calculate similarity score between two pre-normalised strings (0.0 to 1.0).

    Uses RapidFuzz ``fuzz.ratio`` when available (C++ speed); falls back to a
    pure-Python Levenshtein implementation otherwise.

    Unlike the old implementation this function accepts *already-normalised*
    strings so callers that need normalization should normalise before calling.

    Args:
        s1: First string
        s2: Second string

    Returns:
        Similarity score from 0.0 (completely different) to 1.0 (identical)
    """
    if not s1 or not s2:
        return 0.0

    if s1 == s2:
        return 1.0

    if _HAVE_RAPIDFUZZ:
        return _fuzz.ratio(s1, s2) / 100.0

    # Pure-Python fallback
    max_length = max(len(s1), len(s2))
    if max_length == 0:
        return 1.0
    distance = levenshtein_distance(s1, s2)
    return 1.0 - (distance / max_length)


# ---------------------------------------------------------------------------
# Rec 2 – Multi-candidate title generation from filenames (Soulmate-inspired)
# ---------------------------------------------------------------------------

_TRACK_NUMBER_PREFIX_RE = re.compile(
    # Matches: one or more digits, optionally followed by dot/dash + more digits,
    # then trailing separators (dot, dash, or spaces).
    # Examples matched: "01 ", "02-", "1.", "01-02 ", "1.2-"
    r"^\d+([-.]\d+)*[-.\s]*"
)
_FILE_EXTENSION_RE = re.compile(r"\.\w{2,5}$")

# Delimiters that may separate track number / artist / album from the actual
# title, together with which "side" of the split contains the title.
# NOTE: The hyphen delimiter uses " - " (space-hyphen-space) to avoid splitting
# titles that contain hyphens as part of the song name (e.g. "Spider-Man Theme").
_TITLE_SPLIT_DELIMITERS = [
    ("(", 0),       # "(feat. X)" → keep everything before
    ("feat.", 0),   # "Song feat. Artist" → keep left side
    ("featuring", 0),
    ("[", 0),       # "[Radio Edit]" → keep left side
    (" - ", -1),    # "Artist - Song" → keep right side (space-hyphen-space only)
]


def get_possible_titles(filename: str) -> List[str]:
    """
    Generate a list of candidate title strings from a filename.

    Inspired by Soulmate's ``_get_possible_titles``.  Each variant is
    already lowercased so it is ready for direct comparison or RapidFuzz.

    Variants produced for each form of the name (original / underscore→space):
    1. Full name with track-number prefix removed.
    2. Full name *including* the prefix (useful when the title itself starts
       with a number).
    3. Sub-strings produced by splitting on common delimiters like ``(``,
       ``feat.``, ``[``, ``-``.

    Args:
        filename: Raw filename, with or without directory path.

    Returns:
        Ordered list of unique candidate title strings (lowercased, no ext).
    """
    # Strip any leading directory components
    filename = filename.replace("\\", "/").rsplit("/", 1)[-1]
    # Replace en-dash / em-dash with ASCII hyphen for uniformity
    filename = filename.replace("–", "-").replace("—", "-")

    possible: List[str] = []

    def _add(candidate: str) -> None:
        c = candidate.strip()
        if c and c not in possible:
            possible.append(c)

    for raw in (filename, filename.replace("_", " ")):
        # Remove file extension
        name = _FILE_EXTENSION_RE.sub("", raw)
        name_with_num = name.lower()

        # Version without track-number prefix
        name_no_num = _TRACK_NUMBER_PREFIX_RE.sub("", name).lower()

        _add(name_no_num)
        _add(name_with_num)

        # Delimiter-based variants (applied to the no-number version)
        for delimiter, position in _TITLE_SPLIT_DELIMITERS:
            needle = delimiter.lower()
            if needle not in name_no_num:
                continue
            if position == 0:
                extracted_title = name_no_num.split(needle, 1)[0]
            else:
                extracted_title = name_no_num.rsplit(needle, 1)[-1]
            _add(extracted_title.strip())

    return possible


def best_title_match_score(filename: str, track_title: str) -> float:
    """
    Return the best similarity score between any candidate title derived from
    *filename* and the given *track_title*.

    Uses :func:`get_possible_titles` to enumerate candidates.  When RapidFuzz
    is available, ``fuzz.WRatio`` is used because it handles partial overlaps
    well (e.g. "artist - my song" vs "my song").  Without RapidFuzz the
    standard ``calculate_similarity`` (Levenshtein) is used instead.

    Args:
        filename: Raw filename (with or without path).
        track_title: The known track title to match against.

    Returns:
        Highest similarity score (0.0–1.0) across all candidates.
    """
    norm_target = normalize_string(track_title)
    if not norm_target:
        return 0.0

    candidates = get_possible_titles(filename)
    if not candidates:
        return 0.0

    if _HAVE_RAPIDFUZZ:
        return max((_fuzz.WRatio(c, norm_target) / 100.0 for c in candidates), default=0.0)

    return max((calculate_similarity(c, norm_target) for c in candidates), default=0.0)


# ---------------------------------------------------------------------------
# Rec 3 – Track-number guard (SeekDownloader ExactNumberMatch-inspired)
# ---------------------------------------------------------------------------

_DIGIT_SEQUENCE_RE = re.compile(r"\d+")


def _extract_numbers(text: str) -> List[int]:
    """Extract all contiguous digit sequences from *text* as integers."""
    return [int(m) for m in _DIGIT_SEQUENCE_RE.findall(text)]


def track_numbers_conflict(title1: str, title2: str) -> bool:
    """
    Return True when both titles contain digit sequences that differ.

    If either title contains *no* digits the guard is not applied (returns
    False), so the fuzzy scorer can still decide.  When both titles have
    digits and those digit sequences do not match exactly (same values, same
    order), the titles are considered conflicting track numbers and the
    function returns True to signal that no match should be declared.

    Inspired by SeekDownloader's ``FuzzyHelper.ExactNumberMatch``.

    Args:
        title1: First (normalised) title string.
        title2: Second (normalised) title string.

    Returns:
        True if a track-number conflict was detected, False otherwise.
    """
    nums1 = _extract_numbers(title1)
    nums2 = _extract_numbers(title2)

    # Guard only applies when both titles contain numbers
    if not nums1 or not nums2:
        return False

    return nums1 != nums2


# ---------------------------------------------------------------------------
# End of new helpers
# ---------------------------------------------------------------------------


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

    Rec 3 – Track-number guard: if both titles contain digit sequences that
    differ, returns a score of 0.0 immediately to prevent "01 Song" from
    matching "02 Song".

    Rec 5 – Version-stripped title fallback: the title similarity is the
    *maximum* of the full-title score and the score computed after stripping
    edition markers (e.g. "(Remastered)", "(Radio Edit)") from both titles.
    This ensures "Song (Radio Edit)" matches "Song" at a high score.

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
    raw_title1 = track1.get(title_key1, "")
    raw_title2 = track2.get(title_key2, "")

    norm_title1 = normalize_title(raw_title1)
    norm_title2 = normalize_title(raw_title2)

    # Rec 3 – Track-number guard: conflicting digit sequences → no match
    if track_numbers_conflict(norm_title1, norm_title2):
        zero_components: Dict[str, float] = {
            "title": 0.0, "artist": 0.0, "album": 0.0, "duration": 0.0,
        }
        return 0.0, zero_components

    # Rec 5 – Version-stripped title fallback: compute stripped variant lazily
    # (only when the full-title score is not already perfect) to avoid
    # unnecessary string operations on the hot path.
    title_sim_full = calculate_similarity(norm_title1, norm_title2)
    if title_sim_full < 1.0:
        stripped_title1 = normalize_string(strip_search_parentheses(raw_title1))
        stripped_title2 = normalize_string(strip_search_parentheses(raw_title2))
        title_sim_stripped = calculate_similarity(stripped_title1, stripped_title2)
        title_sim = max(title_sim_full, title_sim_stripped)
    else:
        title_sim = title_sim_full

    artist_sim = calculate_similarity(
        normalize_artist(track1.get(artist_key1, "")),
        normalize_artist(track2.get(artist_key2, ""))
    )

    album_sim = calculate_similarity(
        normalize_album(track1.get(album_key1, "")),
        normalize_album(track2.get(album_key2, ""))
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
        "duration": round(duration_sim, 3),
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
    min_title_score: float = 0.0,
    min_artist_score: float = 0.0,
    **kwargs
) -> Tuple[bool, float, Dict[str, float]]:
    """
    Check if two tracks match using fuzzy matching.

    Rec 4 – Per-component thresholds: in addition to the overall *threshold*,
    callers may supply independent minimum scores for title and artist.  A
    pair is only considered a match when *all* of the following hold:

    - ``overall_score >= threshold``
    - ``title_score  >= min_title_score``  (default: 0.0 → disabled)
    - ``artist_score >= min_artist_score`` (default: 0.0 → disabled)

    This prevents a high duration or album score from compensating for a
    completely wrong title or artist.

    Args:
        track1: First track dictionary
        track2: Second track dictionary
        threshold: Minimum overall similarity score (default: 0.80)
        min_title_score: Minimum per-component title score (default: 0.0)
        min_artist_score: Minimum per-component artist score (default: 0.0)
        **kwargs: Additional arguments forwarded to calculate_track_similarity()

    Returns:
        Tuple of (is_match, score, component_scores)
    """
    score, components = calculate_track_similarity(track1, track2, **kwargs)
    is_match = (
        score >= threshold
        and components.get("title", 0.0) >= min_title_score
        and components.get("artist", 0.0) >= min_artist_score
    )
    return (is_match, score, components)


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
