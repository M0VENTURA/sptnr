import re
import requests
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter


def strip_parentheses(s: str) -> str:
    """Remove text inside parentheses from a string."""
    return re.sub(r"\s*\(.*?\)\s*", " ", (s or "")).strip()


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
