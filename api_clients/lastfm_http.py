"""Low-level Last.fm HTTP client.

This module owns Last.fm request mechanics only:
- identifiable User-Agent (per Last.fm's API etiquette)
- strict thread-locked throttling (Last.fm publishes no fixed limit; this is
  a conservative, self-imposed rate to avoid tripping abuse detection)
- correct GET/POST routing (write services must be POST, per API docs)
- correct method-signature calculation (excludes format/callback, per authspec)
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

from api_clients import session

logger = structlog.get_logger(__name__)

USER_AGENT = "Popularr/2.0 ( https://github.com/M0VENTURA/Popularr )"

_SIGNATURE_EXCLUDED_PARAMS = {"format", "callback"}

_THROTTLE_LOCK = threading.Lock()
_LAST_FM_REQUEST_TIME = 0.0

try:
    from services.infrastructure.api_rate_limiter import get_rate_limiter
    _rate_limiter = get_rate_limiter()
except Exception:
    _rate_limiter = None


def _strict_throttle() -> None:
    """Thread-safe throttle without lock-blocking (approx 4 requests per second)."""
    global _LAST_FM_REQUEST_TIME

    if _rate_limiter:
        try:
            _rate_limiter.throttle_lastfm()
            return
        except Exception as exc:
            logger.debug("External Last.fm rate limiter failed, using local fallback", error=str(exc))

    sleep_time = 0.0
    with _THROTTLE_LOCK:
        now = time.monotonic()
        elapsed = now - _LAST_FM_REQUEST_TIME
        if elapsed < 0.25:
            sleep_time = 0.25 - elapsed
            _LAST_FM_REQUEST_TIME = now + sleep_time
        else:
            _LAST_FM_REQUEST_TIME = now
            
    if sleep_time > 0:
        time.sleep(sleep_time)


def _parse_json_response(response: httpx.Response, method: str, log_prefix: str = "Last.fm") -> dict[str, Any]:
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        return {}

    if "error" in payload:
        error_code = payload["error"]
        error_msg = payload.get("message", "Unknown Last.fm error")
        if error_code == 6:
            logger.debug(f"{log_prefix} API parameter error (no results / bad param)", method=method, error_msg=error_msg)
        else:
            logger.warning(f"{log_prefix} API error", error_code=error_code, method=method, error_msg=error_msg)
        return {"error": True, "error_code": error_code, "message": error_msg, "original": payload}

    def _fix_tags_node(parent: dict[str, Any], node_key: str) -> None:
        if node_key in parent:
            node = parent[node_key]
            if isinstance(node, str):
                parent[node_key] = {"tag": []}
            elif isinstance(node, dict) and "tag" in node:
                if isinstance(node["tag"], dict):
                    node["tag"] = [node["tag"]]
                elif isinstance(node["tag"], str):
                    node["tag"] = [{"name": node["tag"]}]

    for entity in ["track", "album", "artist"]:
        if entity in payload and isinstance(payload[entity], dict):
            _fix_tags_node(payload[entity], "toptags")
            _fix_tags_node(payload[entity], "tags")

    _fix_tags_node(payload, "toptags")
    _fix_tags_node(payload, "tags")

    return payload


class LastFmHttpClient:
    def __init__(self, api_key: str, http_session: Any = None, enabled: bool = True):
        self.api_key = api_key or ""
        self.enabled = enabled
        self.session = http_session or session
        self.base_url = "https://ws.audioscrobbler.com/2.0/"
        self.headers = {"User-Agent": USER_AGENT}

    def build_params(self, method: str, **kwargs: Any) -> dict[str, Any]:
        params = {"method": method, "api_key": self.api_key, "format": "json"}
        if method in ("track.getInfo", "album.getInfo", "artist.getInfo"):
            params.setdefault("autocorrect", "1")
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
        if not self.enabled or not self.api_key:
            raise RuntimeError("Last.fm client disabled or API key missing")

        _strict_throttle()
        return self.session.get(
            self.base_url,
            params=self.build_params(method, **kwargs),
            headers=self.headers,
            timeout=timeout,
        )

    def get_json(
        self,
        method: str,
        *,
        timeout: float = 10.0,
        retryable: bool = True,
        **kwargs: Any,
    ) -> dict[str, Any]:
        try:
            response = self.request(method, timeout=timeout, retryable=retryable, **kwargs)
            return _parse_json_response(response, method)
        except Exception as exc:
            logger.debug("Last.fm GET failed", method=method, error=str(exc))
            return {}


class LastFmAuthClient(LastFmHttpClient):
    def __init__(self, api_key: str, api_secret: str, session_key: str = "", http_session: Any = None, enabled: bool = True):
        super().__init__(api_key=api_key, http_session=http_session, enabled=enabled)
        self.api_secret = api_secret or ""
        self.session_key = session_key or ""

    def _sign_params(self, params: dict[str, Any]) -> str:
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
            response = _do_request()
            if response is None:
                return {}
            return _parse_json_response(response, method, log_prefix="Last.fm Auth")
        except Exception as exc:
            logger.debug("Last.fm signed request failed", method=method, error=str(exc))
            return {}
