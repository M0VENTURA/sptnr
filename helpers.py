import re
import requests
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter


def strip_parentheses(s: str) -> str:
    """Remove text inside parentheses from a string."""
    return re.sub(r"\s*\(.*?\)\s*", " ", (s or "")).strip()


def detect_live_album(album_title: str) -> dict:
    """
    Detect if an album is a live or unplugged album based on its title.
    
    Args:
        album_title: Album title to analyze
        
    Returns:
        dict with is_live and is_unplugged boolean flags
    """
    if not album_title:
        return {"is_live": False, "is_unplugged": False}
    
    title_lower = album_title.lower()
    
    # Check for live indicators
    live_patterns = [
        r'\blive\b',
        r'\bconcert\b',
        r'\bon stage\b',
        r'\bin concert\b',
        r'\blive at\b',
        r'\blive in\b',
        r'\blive from\b',
        r'\blive session\b',
    ]
    
    is_live = any(re.search(pattern, title_lower) for pattern in live_patterns)
    
    # Check for unplugged specifically
    unplugged_patterns = [
        r'\bunplugged\b',
        r'\bacoustic\b',
        r'\bacoustic session\b',
    ]
    
    is_unplugged = any(re.search(pattern, title_lower) for pattern in unplugged_patterns)
    
    return {"is_live": is_live, "is_unplugged": is_unplugged}


def create_retry_session(user_agent: str | None = None, retries: int = 5, backoff: float = 1.2,
                         status_forcelist: tuple = (429, 500, 502, 503, 504),
                         allowed_methods: tuple = ("GET", "POST")) -> requests.Session:
    """Create a requests.Session preconfigured with retry/backoff and optional User-Agent.

    Returns a configured `requests.Session` ready to be used by callers.
    """
    s = requests.Session()
    retry = Retry(
        total=retries,
        connect=retries,
        read=retries,
        backoff_factor=backoff,
        status_forcelist=status_forcelist,
        allowed_methods=frozenset(allowed_methods)
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    if user_agent:
        s.headers.update({"User-Agent": user_agent})
    return s


def normalize_title(title: str) -> str:
    """
    Normalize track title for strict matching.
    Removes special characters, extra whitespace, and converts to lowercase.
    
    Args:
        title: Track title to normalize
        
    Returns:
        Normalized title string
    """
    if not title:
        return ""
    
    # Convert to lowercase
    normalized = title.lower()
    
    # Remove common punctuation and special characters
    # Keep alphanumeric and spaces
    normalized = re.sub(r'[^\w\s]', '', normalized)
    
    # Normalize whitespace
    normalized = ' '.join(normalized.split())
    
    return normalized.strip()


# Alternate version keywords for strict filtering
# Tracks containing these keywords should be rejected in strict mode
ALTERNATE_VERSION_KEYWORDS = [
    "remix", "remaster", "remastered", "acoustic", "live", "unplugged",
    "orchestral", "symphonic", "demo", "instrumental", "edit", "extended",
    "version", "alt", "alternate", "mix", "radio edit", "single edit",
    "album version", "explicit version", "clean version"
]


def is_alternate_version(title: str) -> bool:
    """
    Check if a track title indicates an alternate version.
    
    Args:
        title: Track title to check
        
    Returns:
        True if title contains alternate version keywords
    """
    if not title:
        return False
    
    title_lower = title.lower()
    return any(keyword in title_lower for keyword in ALTERNATE_VERSION_KEYWORDS)


def select_best_spotify_match_strict(
    spotify_results: list,
    original_title: str,
    original_duration_ms: int = None,
    original_isrc: str = None,
    duration_tolerance_sec: int = 2
) -> dict | None:
    """
    Select the best Spotify match using strict exact-match rules.
    
    Version Matching Rules (Strict Exact Match):
    1. Only include Spotify results where:
       - normalized_title == normalized_original_title
       - AND duration difference <= tolerance seconds
       - AND (ISRC matches OR ISRC is missing)
    
    2. Reject any track where the title contains alternate version keywords:
       remix, remaster, acoustic, live, unplugged, orchestral, symphonic,
       demo, instrumental, edit, extended, version, alt, alternate, mix
    
    3. Reject any track where the duration differs by more than ±tolerance seconds.
    
    4. Reject any track where ISRC differs (if ISRC exists).
    
    5. Only compare popularity across the remaining exact-match versions.
    
    6. If multiple exact matches remain:
       - choose the highest popularity among exact matches only.
    
    Args:
        spotify_results: List of Spotify track search results
        original_title: Original track title to match against
        original_duration_ms: Original track duration in milliseconds (optional)
        original_isrc: Original track ISRC (optional)
        duration_tolerance_sec: Maximum allowed duration difference in seconds (default: 2)
        
    Returns:
        Best matching track dict or None if no exact matches found
    """
    if not spotify_results:
        return None
    
    normalized_original = normalize_title(original_title)
    original_duration_sec = original_duration_ms / 1000.0 if original_duration_ms else None
    
    exact_matches = []
    
    for result in spotify_results:
        # Get track details
        track_title = result.get("name", "")
        track_duration_ms = result.get("duration_ms", 0)
        track_isrc = result.get("external_ids", {}).get("isrc")
        
        # Rule 2: Reject alternate versions based on keywords
        if is_alternate_version(track_title):
            continue
        
        # Rule 1a: Check normalized title match
        normalized_track = normalize_title(track_title)
        if normalized_track != normalized_original:
            continue
        
        # Rule 3: Check duration match (if original duration provided)
        if original_duration_sec is not None and track_duration_ms > 0:
            track_duration_sec = track_duration_ms / 1000.0
            duration_diff = abs(track_duration_sec - original_duration_sec)
            if duration_diff > duration_tolerance_sec:
                continue
        
        # Rule 4: Check ISRC match (if both ISRCs exist)
        if original_isrc and track_isrc:
            if original_isrc != track_isrc:
                continue
        
        # This track passed all filters - it's an exact match
        exact_matches.append(result)
    
    # Rule 6: Choose highest popularity among exact matches
    if exact_matches:
        return max(exact_matches, key=lambda r: r.get('popularity', 0))
    
    return None
