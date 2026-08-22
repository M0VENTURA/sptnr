"""Low-level Last.fm HTTP client.

This module owns Last.fm request mechanics only:
- strict thread-locked throttling
- basic API params
- retry/backoff helper
- raw endpoint methods
"""

from __future__ import annotations

import random
import threading
import time
from typing import Any, Callable

import httpx
import structlog
from tenacity import Retrying, retry_if_exception, stop_after_attempt

from api_clients import session

logger = structlog.get_logger(__name__)

LASTFM_DEFAULTS = {
    "MAX_RETRIES": 3,
    "RETRY_BACKOFF": 1.5,
    "RATE_LIMIT_DELAY": 0.5,
}

_THROTTLE_LOCK = threading.Lock()
_LAST_FM_REQUEST_TIME = 0.0

try:
    from services.infrastructure.api_rate_limiter import get_rate_limiter
    _rate_limiter = get_rate_limiter()
except Exception:
    _rate_limiter = None


class _RateLimited(Exception):
    """Marker raised when Last.fm answers 429 so tenacity can wait + retry."""
    def __init__(self, response: httpx.Response):
        super().__init__("Last.fm rate limited (429)")
        self.response = response


def retry_with_backoff(
    func: Callable,
    max_retries: int = LASTFM_DEFAULTS["MAX_RETRIES"],
    backoff_factor: float = LASTFM_DEFAULTS["RETRY_BACKOFF"],
    rate_limit_delay: float = LASTFM_DEFAULTS["RATE_LIMIT_DELAY"],
) -> Any:
    """Retry a callable returning an httpx.Response with exponential backoff."""
    def _is_retryable(exc: BaseException) -> bool:
        if isinstance(exc, _RateLimited):
            return True
        if isinstance(exc, (httpx.ConnectError, ConnectionResetError, httpx.TimeoutException, httpx.RequestError)):
            return True
        if isinstance(exc, httpx.HTTPStatusError):
            status_code = getattr(exc.response, "status_code", None)
            return bool(status_code and status_code >= 500)
        return False

    def _wait(retry_state: Any) -> float:
        n = retry_state.attempt_number
        exc = retry_state.outcome.exception()
        if isinstance(exc, _RateLimited):
            retry_after = exc.response.headers.get("Retry-After")
            try:
                base = float(retry_after) if retry_after else (backoff_factor ** (n - 1)) * 2
            except ValueError:
                base = (backoff_factor ** (n - 1)) * 2
        else:
            base = (backoff_factor ** (n - 1)) + random.uniform(0, 1)
        return rate_limit_delay if n == 1 else rate_limit_delay + base

    retrying = Retrying(
        stop=stop_after_attempt(max_retries),
        wait=_wait,
        retry=retry_if_exception(_is_retryable),
        reraise=True,
    )

    for attempt in retrying:
        with attempt:
            try:
                result = func()
                if hasattr(result, "status_code") and result.status_code == 429:
                    raise _RateLimited(result)
                return result
            except _RateLimited as exc:
                if attempt.retry_state.attempt_number >= max_retries:
                    exc.response.raise_for_status()
                raise

    return None


def _strict_throttle() -> None:
    """Thread-safe throttle (approx 4 requests per second)."""
    global _LAST_FM_REQUEST_TIME
    
    if _rate_limiter:
        try:
            _rate_limiter.throttle_lastfm()
            return
        except Exception:
            pass
            
    with _THROTTLE_LOCK:
        now = time.monotonic()
        elapsed = now - _LAST_FM_REQUEST_TIME
        if elapsed < 0.25:
            time.sleep(0.25 - elapsed)
        _LAST_FM_REQUEST_TIME = time.monotonic()


class LastFmHttpClient:
    """Raw Last.fm API wrapper."""

    def __init__(self, api_key: str, http_session: Any = None, enabled: bool = True):
        self.api_key = api_key or ""
        self.enabled = enabled
        self.session = http_session or session
        self.base_url = "https://ws.audioscrobbler.com/2.0/"

    def build_params(self, method: str, **kwargs: Any) -> dict[str, Any]:
        params = {"method": method, "api_key": self.api_key, "format": "json"}
        params.update(kwargs)
        return params

    def request(self, method: str, *, timeout: float = 10.0, **kwargs: Any) -> Any:
        if not self.enabled or not self.api_key:
            raise RuntimeError("Last.fm client disabled or API key missing")

        def _do_request() -> httpx.Response:
            _strict_throttle()
            return self.session.get(self.base_url, params=self.build_params(method, **kwargs), timeout=timeout)

        retry_kwargs = {}
        if hasattr(self, 'lastfm_config'):
            retry_kwargs["max_retries"] = self.lastfm_config.get("max_retries", 3)
            retry_kwargs["backoff_factor"] = self.lastfm_config.get("retry_backoff", 1.5)
            retry_kwargs["rate_limit_delay"] = self.lastfm_config.get("rate_limit_delay", 0.5)
            
        return retry_with_backoff(_do_request, **retry_kwargs)

    def get_json(self, method: str, *, timeout: float = 10.0, **kwargs: Any) -> dict[str, Any]:
        response = self.request(method, timeout=timeout, **kwargs)
        response.raise_for_status()
        payload = response.json()
        
        if not isinstance(payload, dict):
            return {}
            
        if "error" in payload:
            error_code = payload["error"]
            error_msg = payload.get("message", "Unknown Last.fm error")
            
            if error_code == 6: # Track not found
                logger.debug("Last.fm API track not found", method=method, error_msg=error_msg)
            else:
                logger.warning("Last.fm API error", error_code=error_code, method=method, error_msg=error_msg)
                
            return {"error": True, "error_code": error_code, "message": error_msg, "original": payload}
            
        return payload

    def _get(self, method: str, *, timeout: float = 10.0, **kwargs: Any) -> dict[str, Any]:
        func = lambda: self.request(method, timeout=timeout, **kwargs)
        try:
            result = retry_with_backoff(func)
            if result is not None:
                result.raise_for_status()
                payload = result.json()
                if isinstance(payload, dict) and "error" in payload:
                    logger.warning("Last.fm API error", error_code=payload["error"], method=method, message=payload.get("message", ""))
                    return {}
                return payload if isinstance(payload, dict) else {}
            return {}
        except Exception as exc:
            logger.debug("Last.fm GET failed", method=method, error=str(exc))
            return {}


class LastFmAuthClient(LastFmHttpClient):
    """Last.fm client with session-based authentication for write operations."""

    def __init__(self, api_key: str, api_secret: str, session_key: str = "", http_session: Any = None, enabled: bool = True):
        super().__init__(api_key=api_key, http_session=http_session, enabled=enabled)
        self.api_secret = api_secret or ""
        self.session_key = session_key or ""

    def _sign_params(self, params: dict[str, Any]) -> str:
        import hashlib
        sorted_keys = sorted(params.keys())
        raw = "".join(f"{k}{params[k]}" for k in sorted_keys) + self.api_secret
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    def _signed_request(self, method: str, *, timeout: float = 20.0, **kwargs: Any) -> dict[str, Any]:
        params = self.build_params(method, **kwargs)
        params["api_key"] = self.api_key
        if self.session_key:
            params["sk"] = self.session_key
        params["api_sig"] = self._sign_params(params)

        def _do_request() -> httpx.Response:
            _strict_throttle()
            return self.session.get(self.base_url, params=params, timeout=timeout)

        try:
            result = retry_with_backoff(_do_request)
            if result is not None:
                result.raise_for_status()
                payload = result.json()
                if isinstance(payload, dict) and "error" in payload:
                    logger.warning("Last.fm Auth API error", error_code=payload["error"], method=method, message=payload.get("message", ""))
                    return {}
                return payload if isinstance(payload, dict) else {}
            return {}
        except Exception as exc:
            logger.debug("Last.fm signed request failed", method=method, error=str(exc))
            return {}
