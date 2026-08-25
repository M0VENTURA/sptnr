"""Low-level Last.fm HTTP client.

This module owns Last.fm request mechanics only:
- identifiable User-Agent (per Last.fm's API etiquette)
- strict thread-locked throttling (Last.fm publishes no fixed limit; this is
  a conservative, self-imposed rate to avoid tripping abuse detection)
- correct GET/POST routing (write services must be POST, per API docs)
- correct method-signature calculation (excludes format/callback, per authspec)
- retry/backoff helper, with an opt-out for requests that must not be retried
  (e.g. track.updateNowPlaying, per the scrobbling docs)
- raw endpoint methods
"""

from __future__ import annotations

import hashlib
import random
import threading
import time
from typing import Any, Callable

import httpx
import structlog
from tenacity import Retrying, retry_if_exception, stop_after_attempt

from api_clients import session

logger = structlog.get_logger(__name__)

# Last.fm requests an identifiable User-Agent on all requests: "This helps
# our logging and reduces the risk of you getting banned."
USER_AGENT = "Popularr/2.0 ( https://github.com/M0VENTURA/Popularr )"

LASTFM_DEFAULTS = {
    "MAX_RETRIES": 3,
    "RETRY_BACKOFF": 1.5,
    "RATE_LIMIT_DELAY": 0.5,
}

# Parameters that must be excluded from the method-signature calculation.
# See https://www.last.fm/api/authspec - the signature is built from every
# parameter *except* `format` and `callback`.
_SIGNATURE_EXCLUDED_PARAMS = {"format", "callback"}

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
    """Thread-safe throttle (approx 4 requests per second).

    Last.fm does not publish a fixed rate limit like MusicBrainz does - the
    docs only say to "be reasonable" and avoid "an excessive number of calls
    per second." This value is a conservative, self-imposed ceiling.
    """
    global _LAST_FM_REQUEST_TIME

    if _rate_limiter:
        try:
            _rate_limiter.throttle_lastfm()
            return
        except Exception as exc:
            logger.debug("External Last.fm rate limiter failed, using local fallback", error=str(exc))

    with _THROTTLE_LOCK:
        now = time.monotonic()
        elapsed = now - _LAST_FM_REQUEST_TIME
        if elapsed < 0.25:
            time.sleep(0.25 - elapsed)
        _LAST_FM_REQUEST_TIME = time.monotonic()


def _parse_json_response(response: httpx.Response, method: str, log_prefix: str = "Last.fm") -> dict[str, Any]:
    """Shared response handling: raise on HTTP errors, then surface any
    Last.fm-level `error` payload as a structured dict instead of raising.
    """
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        return {}

    if "error" in payload:
        error_code = payload["error"]
        error_msg = payload.get("message", "Unknown Last.fm error")
        if error_code == 6:
            # Code 6 ("Parameter Error") is a catch-all: no results, an
            # invalid/missing parameter, or an unavailable resource. It is
            # frequently just "no results for this search", so we log it at
            # debug rather than warning to avoid noisy logs during normal use.
            logger.debug(f"{log_prefix} API parameter error (no results / bad param)", method=method, error_msg=error_msg)
        else:
            logger.warning(f"{log_prefix} API error", error_code=error_code, method=method, error_msg=error_msg)
        return {"error": True, "error_code": error_code, "message": error_msg, "original": payload}

    return payload


class LastFmHttpClient:
    """Raw Last.fm API wrapper."""

    def __init__(self, api_key: str, http_session: Any = None, enabled: bool = True):
        self.api_key = api_key or ""
        self.enabled = enabled
        self.session = http_session or session
        self.base_url = "https://ws.audioscrobbler.com/2.0/"
        self.headers = {"User-Agent": USER_AGENT}

    def build_params(self, method: str, **kwargs: Any) -> dict[str, Any]:
        params = {"method": method, "api_key": self.api_key, "format": "json"}
        params.update(kwargs)
        return params

    def request(
        self,
        method: str,
        *,
        timeout: float = 10.0,
        retryable: bool = True,
        **kwargs: Any,
    ) -> Any:
        """Issue a read (GET) request to a Last.fm method.

        `retryable=False` should be used for calls where the docs explicitly
        say not to retry on failure (e.g. track.updateNowPlaying).
        """
        if not self.enabled or not self.api_key:
            raise RuntimeError("Last.fm client disabled or API key missing")

        def _do_request() -> httpx.Response:
            _strict_throttle()
            return self.session.get(
                self.base_url,
                params=self.build_params(method, **kwargs),
                headers=self.headers,
                timeout=timeout,
            )

        if not retryable:
            return _do_request()

        retry_kwargs = {}
        if hasattr(self, "lastfm_config"):
            retry_kwargs["max_retries"] = self.lastfm_config.get("max_retries", 3)
            retry_kwargs["backoff_factor"] = self.lastfm_config.get("retry_backoff", 1.5)
            retry_kwargs["rate_limit_delay"] = self.lastfm_config.get("rate_limit_delay", 0.5)
        return retry_with_backoff(_do_request, **retry_kwargs)

    def get_json(
        self,
        method: str,
        *,
        timeout: float = 10.0,
        retryable: bool = True,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Issue a read request and return the parsed JSON payload.

        Last.fm-level errors (payload contains an "error" key) are returned
        as a structured `{"error": True, ...}` dict rather than raised, so
        callers can distinguish "no results" from a hard failure.
        """
        try:
            response = self.request(method, timeout=timeout, retryable=retryable, **kwargs)
            return _parse_json_response(response, method)
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
        """Compute the Last.fm method signature.

        Per https://www.last.fm/api/authspec, the signature is the MD5 of
        every parameter *except* `format` and `callback`, concatenated as
        key+value in ASCII-sorted key order, with the shared secret appended.
        Including `format` here (a common mistake) produces an
        "Invalid Method Signature" error from Last.fm.
        """
        signable = {k: v for k, v in params.items() if k not in _SIGNATURE_EXCLUDED_PARAMS}
        sorted_keys = sorted(signable.keys())
        raw = "".join(f"{k}{signable[k]}" for k in sorted_keys) + self.api_secret
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    def _signed_request(
        self,
        method: str,
        *,
        timeout: float = 20.0,
        http_method: str = "post",
        retryable: bool = True,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Issue a signed request.

        `http_method` defaults to "post" because virtually every method that
        requires a session-key signature is a write service, and the docs
        require POST for those (e.g. track.scrobble, track.love, tag.*).
        A small number of signed calls that are NOT writes (auth.getToken,
        auth.getSession) are GET per the auth how-to, so pass
        `http_method="get"` explicitly for those.
        """
        params = self.build_params(method, **kwargs)
        params["api_key"] = self.api_key
        if self.session_key:
            params["sk"] = self.session_key
        params["api_sig"] = self._sign_params(params)

        def _do_request() -> httpx.Response:
            _strict_throttle()
            if http_method.lower() == "get":
                return self.session.get(self.base_url, params=params, headers=self.headers, timeout=timeout)
            return self.session.post(self.base_url, data=params, headers=self.headers, timeout=timeout)

        try:
            if not retryable:
                response = _do_request()
            else:
                response = retry_with_backoff(_do_request)
            if response is None:
                return {}
            return _parse_json_response(response, method, log_prefix="Last.fm Auth")
        except Exception as exc:
            logger.debug("Last.fm signed request failed", method=method, error=str(exc))
            return {}
