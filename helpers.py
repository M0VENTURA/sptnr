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
