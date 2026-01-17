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
# Using word boundaries to avoid false positives (e.g., "mix" shouldn't match "remix" in "Mix It Up")
ALTERNATE_VERSION_KEYWORDS = [
    "remix", "remaster", "remastered", "acoustic", "live", "unplugged",
    "orchestral", "symphonic", "demo", "instrumental", "edit", "extended",
    "version", "alt", "alternate", "radio edit", "single edit",
    "album version", "explicit version", "clean version"
]

# Keywords that should match as whole words only (to avoid false positives)
WHOLE_WORD_KEYWORDS = ["mix", "live", "edit", "demo", "alt"]


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
    
    # Check regular keywords (substring match)
    for keyword in ALTERNATE_VERSION_KEYWORDS:
        if keyword in title_lower:
            return True
    
    # Check whole-word keywords (word boundary match)
    import re
    for keyword in WHOLE_WORD_KEYWORDS:
        # Use word boundary regex to ensure it's a complete word
        if re.search(r'\b' + re.escape(keyword) + r'\b', title_lower):
            return True
    
    return False


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
        
        # Rule 4: Check ISRC match (if original ISRC is provided)
        # If original has ISRC and track has ISRC, they must match
        # If original has no ISRC, we don't filter based on track ISRC
        if original_isrc:
            # Original has ISRC - if track also has ISRC, they must match
            if track_isrc and original_isrc != track_isrc:
                continue
        
        # This track passed all filters - it's an exact match
        exact_matches.append(result)
    
    # Rule 6: Choose highest popularity among exact matches
    if exact_matches:
        return max(exact_matches, key=lambda r: r.get('popularity', 0))
    
    return None


def extract_version_tag(title: str) -> str | None:
    """
    Extract version tag from parentheses in a title.
    
    Examples:
        "Track Name (Live)" -> "live"
        "Track Name (Remix)" -> "remix"
        "Track Name" -> None
        "Track (Acoustic Version)" -> "acoustic"
    
    Args:
        title: Track title to extract from
        
    Returns:
        Normalized version tag (lowercase, no punctuation) or None if no tag found
    """
    if not title:
        return None
    
    # Match text inside parentheses
    match = re.search(r'\(([^)]+)\)', title)
    if not match:
        return None
    
    # Extract and normalize: lowercase and remove punctuation
    tag = match.group(1).lower()
    tag = re.sub(r'[^\w\s]', '', tag).strip()
    
    # Remove common words that aren't version indicators
    tag = re.sub(r'\b(version|edit|mix)\b', '', tag).strip()
    
    return tag if tag else None


def normalize_title_for_matching(title: str) -> str:
    """
    Normalize title for matching by removing trailing suffixes like "- Single" or "- EP".
    Also removes punctuation and extra whitespace.
    
    Args:
        title: Track or album title
        
    Returns:
        Normalized title
    """
    if not title:
        return ""
    
    # Convert to lowercase
    normalized = title.lower()
    
    # Remove trailing "- single" or "- ep" suffixes
    normalized = re.sub(r'\s*-\s*(single|ep)\s*$', '', normalized)
    
    # Remove punctuation
    normalized = re.sub(r'[^\w\s]', ' ', normalized)
    
    # Collapse whitespace
    normalized = ' '.join(normalized.split())
    
    return normalized.strip()


def find_matching_spotify_single(
    spotify_results: list,
    track_title: str,
    track_duration_ms: int = None,
    duration_tolerance_sec: int = 2,
    logger=None
) -> dict | None:
    """
    Find a matching Spotify single using sophisticated version-aware matching logic.
    
    Implements the PR #131 requirements:
    1. Extract parenthetical version tags from both track and Spotify release
    2. Match version types (live matches live, remix matches remix, etc.)
    3. Override version matching for explicitly marked singles
    4. Normalize titles and match with tolerance
    5. Accept various album types (single, ep, album, compilation)
    6. Apply duration matching with ±2 seconds tolerance
    7. Comprehensive logging
    
    Args:
        spotify_results: List of Spotify search results
        track_title: Original track title from album
        track_duration_ms: Track duration in milliseconds (optional)
        duration_tolerance_sec: Tolerance for duration matching in seconds
        logger: Logger instance for debugging output
        
    Returns:
        Matching Spotify release dict or None if no match found
    """
    if not spotify_results:
        if logger:
            logger.debug(f"[DEBUG] No Spotify releases provided for matching: {track_title}")
        return None
    
    # Extract version tag from original track
    track_version_tag = extract_version_tag(track_title)
    track_normalized = normalize_title_for_matching(track_title)
    track_duration_sec = track_duration_ms / 1000.0 if track_duration_ms else None
    
    if logger:
        logger.debug(f"[DEBUG] Matching track: {track_title}")
        logger.debug(f"[DEBUG]   Version tag: {track_version_tag or 'None'}")
        logger.debug(f"[DEBUG]   Normalized: {track_normalized}")
        logger.debug(f"[DEBUG]   Duration: {track_duration_sec}s" if track_duration_sec else "[DEBUG]   Duration: N/A")
        logger.debug(f"[DEBUG] Total Spotify releases to check: {len(spotify_results)}")
    
    accepted_releases = []
    
    for idx, result in enumerate(spotify_results):
        # Get release details
        release_title = result.get("name", "")
        album_info = result.get("album", {})
        album_type = album_info.get("album_type", "").lower()
        album_name = album_info.get("name", "").lower()
        release_duration_ms = result.get("duration_ms", 0)
        
        if logger:
            logger.debug(f"[DEBUG] Release {idx + 1}: {release_title}")
            logger.debug(f"[DEBUG]   Album: {album_name} (type: {album_type})")
        
        # Extract version tag from Spotify release
        release_version_tag = extract_version_tag(release_title)
        release_normalized = normalize_title_for_matching(release_title)
        
        if logger:
            logger.debug(f"[DEBUG]   Version tag: {release_version_tag or 'None'}")
            logger.debug(f"[DEBUG]   Normalized: {release_normalized}")
        
        # Rule 3: Check if explicitly marked as a single
        is_explicit_single = (
            album_type == "single" or 
            release_title.lower().endswith("- single") or
            album_name.endswith("- single")
        )
        
        # Rule 2: Version-type matching
        version_match = False
        if track_version_tag and release_version_tag:
            # Both have version tags - they must match
            version_match = track_version_tag == release_version_tag
            if logger:
                logger.debug(f"[DEBUG]   Version match: {version_match} (track: {track_version_tag}, release: {release_version_tag})")
        elif not track_version_tag and not release_version_tag:
            # Neither has version tags - that's a match
            version_match = True
            if logger:
                logger.debug(f"[DEBUG]   Version match: True (both have no version tags)")
        elif is_explicit_single:
            # Rule 3: Override - explicitly marked singles can have different version tags
            version_match = True
            if logger:
                logger.debug(f"[DEBUG]   Version match: True (explicit single override)")
        else:
            # One has version tag, the other doesn't - no match unless it's an explicit single
            version_match = False
            if logger:
                logger.debug(f"[DEBUG]   Version match: False (version tag mismatch)")
        
        if not version_match:
            if logger:
                logger.debug(f"[DEBUG]   ❌ REJECTED: Version tag mismatch")
            continue
        
        # Rule 4: Title matching
        title_match = False
        if release_normalized == track_normalized:
            title_match = True
        elif release_normalized.startswith(track_normalized):
            # Allow "Track Name - Single" to match "Track Name"
            title_match = True
        
        if not title_match:
            if logger:
                logger.debug(f"[DEBUG]   ❌ REJECTED: Title mismatch")
            continue
        
        # Rule 5: Album type acceptance
        album_type_ok = album_type in ["single", "ep", "album", "compilation"]
        if not album_type_ok:
            if logger:
                logger.debug(f"[DEBUG]   ❌ REJECTED: Album type '{album_type}' not accepted")
            continue
        
        # Rule 6: Duration matching (±2 seconds)
        if track_duration_sec is not None and release_duration_ms > 0:
            release_duration_sec = release_duration_ms / 1000.0
            duration_diff = abs(release_duration_sec - track_duration_sec)
            if duration_diff > duration_tolerance_sec:
                if logger:
                    logger.debug(f"[DEBUG]   ❌ REJECTED: Duration difference {duration_diff:.1f}s > {duration_tolerance_sec}s")
                continue
            else:
                if logger:
                    logger.debug(f"[DEBUG]   ✓ Duration match: {duration_diff:.1f}s difference")
        
        # All checks passed - this is an accepted release
        # Track whether this was accepted via explicit single override
        is_override_match = (track_version_tag or release_version_tag) and is_explicit_single
        
        if logger:
            logger.debug(f"[DEBUG]   ✅ ACCEPTED: {release_title}" + (" (via override)" if is_override_match else ""))
        accepted_releases.append((result, is_override_match))
    
    # Prefer exact version matches over override matches
    if accepted_releases:
        # First try to find a non-override match
        exact_matches = [r for r, override in accepted_releases if not override]
        if exact_matches:
            best_match = exact_matches[0]
        else:
            # Fall back to override matches
            best_match = accepted_releases[0][0]
        
        if logger:
            logger.debug(f"[DEBUG] ✓ Best match: {best_match.get('name')} (album: {best_match.get('album', {}).get('name')})")
        return best_match
    
    if logger:
        logger.debug(f"[DEBUG] No Spotify singles matched for {track_title}")
    
    return None
