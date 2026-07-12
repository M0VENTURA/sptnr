"""Low-level Last.fm HTTP client.

This module owns Last.fm request mechanics only:
- shared session use
- basic API params
- retry/backoff helper
- raw endpoint methods

Application behaviour such as multi-artist candidate ranking, recommendation
caching, album filtering, and popularity interpretation belongs in
``services.enrichment.lastfm_service``.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Callable

from requests.exceptions import ConnectionError, HTTPError, RequestException, Timeout

from api_clients import session

logger = logging.getLogger(__name__)

LASTFM_DEFAULTS = {
    "MAX_RETRIES": 3,
    "RETRY_BACKOFF": 1.5,
    "RATE_LIMIT_DELAY": 0.5,
}


def retry_with_backoff(
    func: Callable,
    max_retries: int = LASTFM_DEFAULTS["MAX_RETRIES"],
    backoff_factor: float = LASTFM_DEFAULTS["RETRY_BACKOFF"],
    rate_limit_delay: float = LASTFM_DEFAULTS["RATE_LIMIT_DELAY"],
):
    """Retry a callable returning a requests.Response with exponential backoff."""
    for attempt in range(max_retries):
        try:
            time.sleep(rate_limit_delay)
            result = func()

            if hasattr(result, "status_code") and result.status_code == 429:
                if attempt == max_retries - 1:
                    result.raise_for_status()
                retry_after = result.headers.get("Retry-After")
                try:
                    wait_time = float(retry_after) if retry_after else (backoff_factor ** attempt) * 2
                except ValueError:
                    wait_time = (backoff_factor ** attempt) * 2
                logger.warning("Last.fm rate limited; waiting %.2fs", wait_time)
                time.sleep(wait_time)
                continue

            return result

        except (ConnectionError, ConnectionResetError, Timeout, RequestException) as exc:
            if attempt == max_retries - 1:
                raise
            wait_time = (backoff_factor ** attempt) + random.uniform(0, 1)
            logger.debug("Last.fm retry %s/%s after %.2fs: %s", attempt + 1, max_retries, wait_time, exc)
            time.sleep(wait_time)

        except HTTPError as exc:
            response = getattr(exc, "response", None)
            status_code = getattr(response, "status_code", None)
            if status_code and status_code >= 500 and attempt < max_retries - 1:
                wait_time = (backoff_factor ** attempt) + random.uniform(0, 1)
                time.sleep(wait_time)
                continue
            raise

    return None


class LastFmHttpClient:
    """Raw Last.fm API wrapper."""

    def __init__(self, api_key: str, http_session=None, enabled: bool = True):
        self.api_key = api_key or ""
        self.enabled = enabled
        self.session = http_session or session
        self.base_url = "https://ws.audioscrobbler.com/2.0/"

    def build_params(self, method: str, **kwargs) -> dict[str, Any]:
        """Build standard Last.fm query params."""
        params = {"method": method, "api_key": self.api_key, "format": "json"}
        params.update(kwargs)
        return params

    def request(self, method: str, *, timeout: tuple[int, int] = (5, 10), **kwargs):
        """Perform a raw Last.fm request and return requests.Response.

        Uses ``retry_with_backoff`` for resilience against rate limits and
        transient network errors.
        """
        if not self.enabled or not self.api_key:
            raise RuntimeError("Last.fm client disabled or API key missing")

        def _do_request():
            return self.session.get(self.base_url, params=self.build_params(method, **kwargs), timeout=timeout)

        retry_kwargs = {}
        if hasattr(self, 'lastfm_config'):
            retry_kwargs["max_retries"] = self.lastfm_config.get("max_retries", 3)
            retry_kwargs["backoff_factor"] = self.lastfm_config.get("retry_backoff", 1.5)
            retry_kwargs["rate_limit_delay"] = self.lastfm_config.get("rate_limit_delay", 0.5)
        return retry_with_backoff(_do_request, **retry_kwargs)

    def get_json(self, method: str, *, timeout: tuple[int, int] = (5, 10), **kwargs) -> dict[str, Any]:
        """Perform a Last.fm request and return JSON dict.

        Handles Last.fm error responses (which return HTTP 200 with an
        ``error`` field in the JSON body) by raising an exception.
        """
        response = self.request(method, timeout=timeout, **kwargs)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            return {}
        # Last.fm returns errors as HTTP 200 with {"error": ..., "message": ...}
        if "error" in payload:
            error_code = payload["error"]
            error_msg = payload.get("message", "Unknown Last.fm error")
            logger.warning("Last.fm API error %s for '%s': %s", error_code, method, error_msg)
            return {"error": True, "error_code": error_code, "message": error_msg, "original": payload}
        return payload

    # ------------------------------------------------------------------
    # Endpoint wrappers (return raw Last.fm response dicts)
    # ------------------------------------------------------------------

    def _get(self, method: str, *, timeout: tuple[int, int] = (5, 10), **kwargs) -> dict[str, Any]:
        """Internal helper with retry support for GET requests."""
        func = lambda: self.request(method, timeout=timeout, **kwargs)
        try:
            result = retry_with_backoff(func)
            if result is not None:
                result.raise_for_status()
                payload = result.json()
                if isinstance(payload, dict) and "error" in payload:
                    logger.warning("Last.fm API error %s for '%s': %s",
                                   payload["error"], method, payload.get("message", ""))
                    return {}
                return payload if isinstance(payload, dict) else {}
            return {}
        except Exception as exc:
            logger.debug("Last.fm '%s' failed: %s", method, exc)
            return {}


class LastFmAuthClient(LastFmHttpClient):
    """Last.fm client with session-based authentication for write operations."""

    def __init__(self, api_key: str, api_secret: str, session_key: str = "", http_session=None, enabled: bool = True):
        super().__init__(api_key=api_key, http_session=http_session, enabled=enabled)
        self.api_secret = api_secret or ""
        self.session_key = session_key or ""

    def _sign_params(self, params: dict[str, Any]) -> str:
        """Create an API signature (md5) for authenticated requests."""
        import hashlib
        sorted_keys = sorted(params.keys())
        raw = "".join(f"{k}{params[k]}" for k in sorted_keys) + self.api_secret
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    def _signed_request(self, method: str, *, timeout: tuple[int, int] = (10, 20), **kwargs) -> dict[str, Any]:
        """Perform an authenticated (signed) Last.fm request."""
        from api_clients.lastfm_http import retry_with_backoff

        params = self.build_params(method, **kwargs)
        params["api_key"] = self.api_key
        if self.session_key:
            params["sk"] = self.session_key
        params["api_sig"] = self._sign_params(params)

        def _do_request():
            return self.session.get(self.base_url, params=params, timeout=timeout)

        try:
            result = retry_with_backoff(_do_request)
            if result is not None:
                result.raise_for_status()
                payload = result.json()
                if isinstance(payload, dict) and "error" in payload:
                    logger.warning("Last.fm auth API error %s for '%s': %s",
                                   payload["error"], method, payload.get("message", ""))
                    return {}
                return payload if isinstance(payload, dict) else {}
            return {}
        except Exception as exc:
            logger.debug("Last.fm auth '%s' failed: %s", method, exc)
            return {}
