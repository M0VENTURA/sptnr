import re
import ssl
import requests
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context


# Default patterns inside parentheses that are stripped during popularity/single
# detection lookups.  Users can extend or override this list via the
# ``strip_parentheses_filters`` key in config.yaml.
DEFAULT_STRIP_PARENTHESES_FILTERS: list[str] = [
    "live",
    "demo",
    "acoustic",
    "remix",
    "radio edit",
    "single version",
    "album version",
    "remaster",
    "remastered",
    "cover",
    "instrumental",
    "unplugged",
    "edit",
]


def strip_parentheses(
    s: str,
    trailing_only: bool = False,
    extra_patterns: list[str] | None = None,
) -> str:
    """Remove parenthesised text from a string.

    This is the single, unified implementation used by both the helpers
    layer and popularity/single-detection code.

    Args:
        s: Input string to clean.
        trailing_only: When ``True`` only the *last* parenthetical group in
            the string is removed, e.g.::

                "Track (Live)" → "Track"
                "Track (One) Two" → "Track (One) Two"  ← no change

            When ``False`` (the default) *all* parenthetical groups are
            removed::

                "Song (Live) (Acoustic)" → "Song"

        extra_patterns: Optional list of keyword patterns.  When provided,
            only parenthetical groups whose content **contains** one of the
            listed words are removed (case-insensitive).  Other parenthetical
            groups are left unchanged.  This replaces the behaviour of the old
            ``helpers.strip_parentheses()`` / ``popularity.strip_parentheses()``
            pair by letting callers decide exactly which version tags to strip.

            Example::

                strip_parentheses("Song (Radio Edit) (Live)", extra_patterns=["radio edit"])
                # → "Song (Live)"

    Returns:
        The cleaned string with the requested parenthetical groups removed.
    """
    if not s:
        return s or ""

    if extra_patterns:
        result = s
        for pattern in extra_patterns:
            escaped = re.escape(pattern)
            if trailing_only:
                result = re.sub(
                    rf'\s*\([^)]*{escaped}[^)]*\)\s*$',
                    '',
                    result,
                    flags=re.IGNORECASE,
                ).strip()
            else:
                result = re.sub(
                    rf'\s*\([^)]*{escaped}[^)]*\)\s*',
                    ' ',
                    result,
                    flags=re.IGNORECASE,
                ).strip()
        return result

    if trailing_only:
        return re.sub(r'\s*\([^)]*\)\s*$', '', s).strip()

    return re.sub(r"\s*\(.*?\)\s*", " ", s).strip()


def strip_cover_attribution(title: str) -> str:
    """
    Strip cover attributions from track titles for cleaner API searches.
    
    Removes parenthetical cover attributions like:
    - "(Artist Name Cover)"
    - "(Artist Cover)"
    - "(Cover Version)"
    - "(Cover by Artist Name)"
    
    Also removes common cover labels without artist names:
    - "(Cover)"
    
    Examples:
        "The Pretender (Foo Fighters Cover)" -> "The Pretender"
        "Song Name (Cover Version)" -> "Song Name"
        "Track (Acoustic)" -> "Track (Acoustic)"  [keeps other version tags]
        "Title (One) Two" -> "Title (One) Two"  [keeps middle parentheses]
    
    Args:
        title: Track title to clean
        
    Returns:
        Title with cover attributions removed
    """
    if not title:
        return title
    
    # Remove trailing cover attributions in parentheses
    # Pattern matches patterns like:
    # - (Artist Name Cover) 
    # - (Artist Cover)
    # - (Cover Version)
    # - (Cover by Artist)
    # - (Cover)
    # But only from the end of the string (trailing)
    
    patterns = [
        r'\s*\([^)]*cover[^)]*\)\s*$',  # Any trailing parentheses containing "cover" (case-insensitive)
    ]
    
    result = title
    for pattern in patterns:
        result = re.sub(pattern, '', result, flags=re.IGNORECASE).strip()
    
    return result


def clean_discogs_biography(text: str) -> str:
    """
    Clean up Discogs biography text by removing artist ID references.
    
    Discogs biographies often contain artist IDs in square brackets like [a755006]
    which should be removed for cleaner display.
    
    Args:
        text: Raw biography text from Discogs
        
    Returns:
        Cleaned biography text
    """
    if not text:
        return text
    
    # Remove artist ID references like [a755006], [a2891826], etc.
    # Pattern: [a followed by digits]
    cleaned = re.sub(r'\[a\d+\]', '', text)
    
    # Remove orphaned "aka" when both sides were artist IDs
    # Example: "[a123] aka [a456]" becomes " aka " which should be removed
    # Matches "aka" followed by whitespace and then specific punctuation, parentheses, or end of string
    # (indicating there's no actual name after the "aka")
    cleaned = re.sub(r'\baka\s*(?=\s*\(|,|\.|$)', '', cleaned)
    
    # Remove leading "aka " at the start of content after removing IDs
    # Example: "[a111111] aka John Smith" becomes "aka John Smith" -> "John Smith"
    cleaned = re.sub(r'^\s*aka\s+', '', cleaned)
    
    # Clean up multiple consecutive spaces left after removals
    cleaned = re.sub(r'\s+', ' ', cleaned)
    
    return cleaned.strip()


class SSLAdapter(HTTPAdapter):
    """
    Custom HTTPAdapter with improved SSL/TLS handling.
    
    This adapter creates a custom SSL context that is more resilient to
    SSL/TLS protocol errors, particularly the "EOF occurred in violation of protocol"
    error that can occur with some servers.
    """
    
    def init_poolmanager(self, *args, **kwargs):
        """Initialize the pool manager with a custom SSL context."""
        # Create a custom SSL context with improved compatibility
        ctx = create_urllib3_context()
        
        # Set minimum TLS version to TLSv1.2 for better compatibility
        # while still maintaining reasonable security
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        
        # Allow legacy server connect for better compatibility with older servers
        # This helps with servers that might not follow the TLS spec perfectly
        # Note: OP_LEGACY_SERVER_CONNECT is only available in Python 3.12+
        if hasattr(ssl, 'OP_LEGACY_SERVER_CONNECT'):
            ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT
        
        # Set the SSL context in kwargs
        kwargs['ssl_context'] = ctx
        
        return super().init_poolmanager(*args, **kwargs)


def detect_live_album(album_title: str) -> dict:
    """
    Detect if an album is a live or unplugged album based on its title.

    This function provides a conservative heuristic for initial detection only.
    MusicBrainz secondary album type is the authoritative source and will override
    this result during the popularity scan (see popularity.py album type detection).

    Only matches unambiguous format indicators, not words that could appear in
    regular album titles.
    Examples:
    - "Album Live at Venue" -> live=True
    - "(how to live) AS GHOSTS" -> live=False (live is part of title)
    - "13 Ways to Bleed on Stage" -> live=False (no unambiguous live indicator)
    - "Album (Live)" -> live=True
    - "Album - Live 2023" -> live=True
    
    Args:
        album_title: Album title to analyze
        
    Returns:
        dict with is_live and is_unplugged boolean flags
    """
    if not album_title:
        return {"is_live": False, "is_unplugged": False}
    
    title_lower = album_title.lower()
    
    # Check for SPECIFIC live format indicators (not just any "live" word)
    # These patterns require "live" to be in a format tag position (end, after separator, between brackets)
    # NOT inside the actual title like "(how to live)"
    # Note: Ambiguous patterns like "on stage" or standalone "concert" are intentionally
    # excluded to avoid false positives (e.g., "13 Ways to Bleed on Stage").
    # MusicBrainz secondary type detection in popularity.py is the authoritative source.
    live_patterns = [
        # Standalone format tags (anchored to end)
        r'\(live\)\s*$',           # "(live)" at the end
        r'\[live\]\s*$',           # "[live]" at the end
        r'-\s*live\s*$',           # "- live" at the end
        r',\s*live\s*$',           # ", live" at the end
        r'\+\s*live\s*$',          # "+ live" at the end
        
        # "Live at/in/from" patterns (more restrictive)
        r'live\s+at\b',            # "live at venue"
        r'live\s+in\b',            # "live in city"
        r'live\s+from\b',          # "live from venue"
        r'live\s+session',         # "live session"
        r'live\s+recording',       # "live recording"
        
        # Concert-related (only unambiguous forms)
        r'\bin\s+concert\b',       # "in concert"
    ]
    
    is_live = any(re.search(pattern, title_lower) for pattern in live_patterns)
    
    # Check for unplugged specifically
    unplugged_patterns = [
        r'\bunplugged\b',
        r'\bacoustic\b',
        r'\bacoustic\s+session\b',
    ]
    
    is_unplugged = any(re.search(pattern, title_lower) for pattern in unplugged_patterns)
    
    return {"is_live": is_live, "is_unplugged": is_unplugged}


def detect_christmas_song(track_title: str, album_title: str) -> bool:
    """
    Detect if a song is a Christmas song based on its title or album title.
    
    Args:
        track_title: Track title to analyze
        album_title: Album title to analyze
        
    Returns:
        True if detected as a Christmas song, False otherwise
    """
    if not track_title and not album_title:
        return False
    
    # Combine both titles for checking
    combined = f"{track_title or ''} {album_title or ''}".lower()
    
    # Christmas-related keywords (comprehensive list)
    christmas_patterns = [
        r'\bchristmas\b',
        r'\bxmas\b',
        r'\bx-mas\b',
        r'\bholiday',  # "holiday" or "holidays"
        r'\bnoel\b',
        r'\bsanta\b',
        r'\bsleigh\b',
        r'\bjingle\b',
        r'\bsilent night\b',
        r'\bholy night\b',
        r'\bwinter wonderland\b',
        r'\bwhite christmas\b',
        r'\bjingle bells\b',
        r'\blast christmas\b',
        r'\bmariah carey christmas\b',  # Common Christmas album
        r'\bchristmas album\b',
        r'\bchristmas collection\b',
        r'\bchristmas carols\b',
        r'\bxmas album\b',
        r'\byule\b',
        r'\byuletide\b',
        r'\bfestive\b',
        r'\badvent\b',
        r'\breindeer\b',
        r'\bingles\b',  # "jingles"
        r'\bchristmastime\b',
    ]
    
    # Check if any pattern matches
    return any(re.search(pattern, combined) for pattern in christmas_patterns)


def create_retry_session(user_agent: str | None = None, retries: int = 5, backoff: float = 1.2,
                         status_forcelist: tuple = (429, 500, 502, 503, 504),
                         allowed_methods: tuple = ("GET", "POST"),
                         verify_ssl: bool = True) -> requests.Session:
    """Create a requests.Session preconfigured with retry/backoff and optional User-Agent.

    Handles HTTP errors, connection errors, and SSL errors with exponential backoff.
    Uses a custom SSL adapter to handle SSL/TLS protocol issues more gracefully.
    
    Args:
        user_agent: Optional User-Agent string to use for all requests
        retries: Number of retries for failed connections
        backoff: Exponential backoff factor between retries
        status_forcelist: HTTP status codes to retry on
        allowed_methods: HTTP methods to allow retries for
        verify_ssl: Whether to verify SSL certificates (default True)
    
    Returns a configured `requests.Session` ready to be used by callers.
    """
    s = requests.Session()
    retry = Retry(
        total=retries,
        connect=retries,
        read=retries,
        backoff_factor=backoff,
        status_forcelist=status_forcelist,
        allowed_methods=frozenset(allowed_methods),
        raise_on_status=False  # Don't raise exceptions on bad status codes
    )
    
    # Set SSL verification for the session
    s.verify = verify_ssl
    
    # Use the custom SSL adapter for better SSL/TLS handling
    ssl_adapter = SSLAdapter(max_retries=retry)
    s.mount("https://", ssl_adapter)
    s.mount("http://", HTTPAdapter(max_retries=retry))
    if user_agent:
        s.headers.update({"User-Agent": user_agent})
    return s


def normalize_title(title: str) -> str:
    """
    Normalize track title for strict matching.
    Removes special characters, extra whitespace, converts to lowercase,
    and strips leading articles (a, an, the).
    
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
    
    # Strip leading articles (a, an, the)
    normalized = re.sub(r'^(?:a|an|the)\s+', '', normalized)
    
    return normalized


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
    
    # Remove common filler words only when they're part of longer phrases
    # Don't remove them if they're the only word
    words = tag.split()
    if len(words) > 1:
        # Only filter out these words when combined with other words
        filtered_words = [w for w in words if w not in ('version', 'edit', 'mix')]
        if filtered_words:  # Make sure we don't end up with empty string
            tag = ' '.join(filtered_words)
    
    return tag if tag else None


def normalize_title_for_matching(title: str) -> str:
    """
    Normalize title for matching by removing trailing suffixes like "- Single" or "- EP".
    Also removes punctuation, extra whitespace, and strips leading articles (a, an, the).
    
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
    
    # Strip leading articles (a, an, the)
    normalized = re.sub(r'^(?:a|an|the)\s+', '', normalized)
    
    return normalized


def find_matching_spotify_single(
    spotify_results: list,
    track_title: str,
    track_duration_ms: int = None,
    track_artist: str = None,
    track_album: str = None,
    track_isrc: str = None,
    duration_tolerance_sec: int = 2,
    logger=None
) -> dict | None:
    """
    Find a matching Spotify single using sophisticated version-aware matching logic.
    
    Now enhanced with improved matching from matching_utils.py:
    - Full Unicode normalization (accent removal)
    - Collaboration handling (feat., ft., with, &, etc.)
    - Weighted fuzzy matching (title 35%, artist 25%, duration 25%, album 15%)
    - ISRC-based matching for perfect confidence
    
    Original PR #131 requirements:
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
        track_artist: Track artist name (optional, for improved matching)
        track_album: Track album name (optional, for improved matching)
        track_isrc: Track ISRC code (optional, for perfect matching)
        duration_tolerance_sec: Tolerance for duration matching in seconds
        logger: Logger instance for debugging output
        
    Returns:
        Matching Spotify release dict or None if no match found
    """
    if not spotify_results:
        if logger:
            logger.debug(f"[DEBUG] No Spotify releases provided for matching: {track_title}")
        return None
    
    # Import matching utilities for improved matching
    try:
        from matching_utils import (
            normalize_title as normalize_title_advanced,
            normalize_artist,
            calculate_track_similarity,
            matches_by_isrc
        )
        use_advanced_matching = True
    except ImportError:
        if logger:
            logger.debug("[DEBUG] matching_utils not available, using legacy matching")
        use_advanced_matching = False
    
    # Extract version tag from original track
    track_version_tag = extract_version_tag(track_title)
    track_normalized = normalize_title_for_matching(track_title)
    track_duration_sec = track_duration_ms / 1000.0 if track_duration_ms else None
    
    if logger:
        logger.debug(f"[DEBUG] Matching track: {track_title}")
        logger.debug(f"[DEBUG]   Version tag: {track_version_tag or 'None'}")
        logger.debug(f"[DEBUG]   Normalized: {track_normalized}")
        logger.debug(f"[DEBUG]   Duration: {track_duration_sec}s" if track_duration_sec else "[DEBUG]   Duration: N/A")
        logger.debug(f"[DEBUG]   ISRC: {track_isrc}" if track_isrc else "[DEBUG]   ISRC: N/A")
        logger.debug(f"[DEBUG] Total Spotify releases to check: {len(spotify_results)}")
    
    accepted_releases = []
    
    for idx, result in enumerate(spotify_results):
        # Get release details
        release_title = result.get("name", "")
        album_info = result.get("album", {})
        album_type = album_info.get("album_type", "").lower()
        album_name = album_info.get("name", "").lower()
        release_duration_ms = result.get("duration_ms", 0)
        
        # Get artist and ISRC safely
        artists_list = result.get("artists", [])
        release_artist = artists_list[0].get("name", "") if artists_list else ""
        release_isrc = result.get("external_ids", {}).get("isrc")
        
        if logger:
            logger.debug(f"[DEBUG] Release {idx + 1}: {release_title}")
            logger.debug(f"[DEBUG]   Album: {album_name} (type: {album_type})")
            logger.debug(f"[DEBUG]   Artist: {release_artist}")
        
        # PRIORITY: ISRC matching (if available)
        if use_advanced_matching and track_isrc and release_isrc:
            if matches_by_isrc(track_isrc, release_isrc):
                if logger:
                    logger.debug(f"[DEBUG]   ✅ ISRC MATCH: Perfect match via ISRC")
                # ISRC match is authoritative - accept immediately
                accepted_releases.append((result, False, 1.0))  # (result, is_override, confidence)
                continue
        
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
        
        # Rule 4: Title matching (with advanced fuzzy matching if available)
        title_match = False
        match_confidence = 0.0
        
        if use_advanced_matching and track_artist and release_artist:
            # Use advanced fuzzy matching
            track_dict = {
                "title": track_title,
                "artist": track_artist,
                "album": track_album or "",
                "duration": track_duration_ms or 0
            }
            release_dict = {
                "title": release_title,
                "artist": release_artist,
                "album": album_name,
                "duration": release_duration_ms
            }
            
            match_confidence, components = calculate_track_similarity(track_dict, release_dict)
            title_match = match_confidence >= 0.80  # Use fuzzy threshold
            
            if logger:
                logger.debug(f"[DEBUG]   Fuzzy match score: {match_confidence:.3f} (components: {components})")
        else:
            # Use legacy exact matching
            if release_normalized == track_normalized:
                title_match = True
                match_confidence = 0.95
            elif release_normalized.startswith(track_normalized):
                # Allow "Track Name - Single" to match "Track Name"
                title_match = True
                match_confidence = 0.90
        
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
        accepted_releases.append((result, is_override_match, match_confidence))
    
    # Prefer exact version matches over override matches
    # Sort by: 1) non-override first, 2) highest confidence, 3) first in list
    if accepted_releases:
        # Sort by: (not is_override, confidence, -idx)
        # This prefers non-overrides, then higher confidence, then earlier results
        accepted_releases.sort(key=lambda x: (not x[1], x[2]), reverse=True)
        best_match = accepted_releases[0][0]
        
        if logger:
            logger.debug(f"[DEBUG] ✓ Best match: {best_match.get('name')} (album: {best_match.get('album', {}).get('name')}, confidence: {accepted_releases[0][2]:.3f})")
        return best_match
    
    if logger:
        logger.debug(f"[DEBUG] No Spotify singles matched for {track_title}")
    
    return None
