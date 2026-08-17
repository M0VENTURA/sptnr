"""Low-level Discogs HTTP client.

This module is intentionally HTTP-focused. It owns:
- Discogs request throttling
- 429 Retry-After handling
- temporary 5xx retry/circuit-breaker behaviour
- raw Discogs endpoint calls

It does NOT own:
- single-detection business rules
- cache policy
- matching heuristics
- scoring decisions
- DB writes

Those belong in ``services.enrichment.discogs_service``.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from api_clients import session as shared_session, timeout_safe_session
from api_clients.http_utils import create_retry_client

logger = logging.getLogger(__name__)

DISCOGS_BASE_URL = "https://api.discogs.com"
DEFAULT_USER_AGENT = "Popularr/1.0 +https://github.com/M0VENTURA/Popularr"

_DISCOGS_LAST_REQUEST_TIME = 0.0
_DISCOGS_MIN_INTERVAL = 0.35
_DISCOGS_RATE_LIMIT_UNTIL = 0.0
_DISCOGS_CIRCUIT_BREAKER_OPEN = False
_DISCOGS_CIRCUIT_BREAKER_RESET_TIME = 0.0
_DISCOGS_CONSECUTIVE_ERRORS = 0
# The pacing state above is module-global but the check-then-sleep window is
# not atomic — concurrent scan threads could double-fire.  The lock makes the
# pacing decision atomic (the cooldown window itself stays shared).
_DISCOGS_THROTTLE_LOCK = threading.Lock()
# Cap on a single 429 cooldown: a long Retry-After must not stall the whole
# scan past the per-album track deadline (300s) — every per-track worker
# would otherwise block on the shared throttle and the album's futures all
# time out ("N (of N) futures unfinished").  A repeated 429 re-arms the
# window, so capping only bounds the worst single wait.
_DISCOGS_MAX_COOLDOWN = 60.0


def build_discogs_session():
    """Create a Discogs-specific session that does not auto-retry 429s."""
    return create_retry_client(
        user_agent=DEFAULT_USER_AGENT,
        retries=3,
        backoff=1.0,
        status_forcelist=(500, 502, 503, 504),
    )


def get_retry_after_seconds(response, default: float = 60.0) -> float:
    """Parse a Retry-After header safely."""
    retry_after_raw = response.headers.get("Retry-After") if response is not None else None
    try:
        retry_after = float(retry_after_raw) if retry_after_raw is not None else float(default)
    except (TypeError, ValueError):
        retry_after = float(default)
    return max(1.0, retry_after)


def _set_rate_limit_window(wait_seconds: float) -> None:
    """Record a shared rate-limit cooldown window (capped)."""
    global _DISCOGS_RATE_LIMIT_UNTIL
    capped = min(max(0.0, wait_seconds), _DISCOGS_MAX_COOLDOWN)
    _DISCOGS_RATE_LIMIT_UNTIL = max(_DISCOGS_RATE_LIMIT_UNTIL, time.time() + capped)


def throttle_discogs() -> None:
    """Respect Discogs request pacing and any active 429 cooldown.

    The cooldown sleep runs OUTSIDE the lock: holding the lock while sleeping
    through a long 429 window would block every other scan thread for the
    whole cooldown, making all per-track futures time out past the album
    deadline ("N (of N) futures unfinished").  The lock only guards the
    shared-state read/update; concurrent workers sleep in parallel.
    """
    global _DISCOGS_LAST_REQUEST_TIME

    with _DISCOGS_THROTTLE_LOCK:
        now = time.time()
        cooldown_wait = _DISCOGS_RATE_LIMIT_UNTIL - now
        elapsed = now - _DISCOGS_LAST_REQUEST_TIME
        min_wait = _DISCOGS_MIN_INTERVAL - elapsed
        _DISCOGS_LAST_REQUEST_TIME = time.time()

    if cooldown_wait > 0:
        time.sleep(cooldown_wait)
    if min_wait > 0:
        time.sleep(min_wait)


def check_circuit_breaker() -> bool:
    """Return True when Discogs requests should be allowed."""
    global _DISCOGS_CIRCUIT_BREAKER_OPEN, _DISCOGS_CIRCUIT_BREAKER_RESET_TIME

    if _DISCOGS_CIRCUIT_BREAKER_OPEN and time.time() < _DISCOGS_CIRCUIT_BREAKER_RESET_TIME:
        return False

    if _DISCOGS_CIRCUIT_BREAKER_OPEN and time.time() >= _DISCOGS_CIRCUIT_BREAKER_RESET_TIME:
        _DISCOGS_CIRCUIT_BREAKER_OPEN = False
        logger.warning("Discogs circuit breaker reset - retrying")

    return True


def record_discogs_error(error_type: str) -> None:
    """Record a Discogs server error and potentially open the circuit breaker."""
    global _DISCOGS_CIRCUIT_BREAKER_OPEN, _DISCOGS_CIRCUIT_BREAKER_RESET_TIME, _DISCOGS_CONSECUTIVE_ERRORS

    _DISCOGS_CONSECUTIVE_ERRORS += 1

    if _DISCOGS_CONSECUTIVE_ERRORS >= 5 and error_type in {"502", "503"}:
        _DISCOGS_CIRCUIT_BREAKER_OPEN = True
        _DISCOGS_CIRCUIT_BREAKER_RESET_TIME = time.time() + 300
        logger.error("Discogs circuit breaker OPEN after repeated %s errors", error_type)


def clear_discogs_errors() -> None:
    """Clear consecutive error count after a successful request."""
    global _DISCOGS_CONSECUTIVE_ERRORS
    _DISCOGS_CONSECUTIVE_ERRORS = 0


class DiscogsHttpClient:
    """Raw Discogs API wrapper."""

    def __init__(self, token: str, http_session=None, enabled: bool = True, user_agent: str = DEFAULT_USER_AGENT):
        self.token = token or ""
        self.enabled = enabled
        self.base_url = DISCOGS_BASE_URL
        self.user_agent = user_agent or DEFAULT_USER_AGENT

        if http_session is None or http_session is shared_session or http_session is timeout_safe_session:
            self.session = build_discogs_session()
        else:
            self.session = http_session

        self.headers = {
            "Authorization": f"Discogs token={self.token}" if self.token else "",
            "User-Agent": self.user_agent,
        }

    def _request(
        self,
        method: str,
        path_or_url: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: float = 10.0,
        max_retries: int = 2,
    ) -> dict[str, Any]:
        """Perform a Discogs request and return JSON dict.

        429s are handled from Retry-After. 502/503 responses are retried with
        exponential backoff and recorded against the circuit breaker.
        """
        if not self.enabled or not self.token:
            return {}

        if not check_circuit_breaker():
            logger.debug("Discogs request skipped because circuit breaker is open")
            return {}

        url = path_or_url if path_or_url.startswith("http") else f"{self.base_url}{path_or_url}"
        retry_delay = 1.0

        for attempt in range(max_retries + 1):
            throttle_discogs()

            response = self.session.request(method, url, headers=self.headers, params=params, timeout=timeout)

            if response.status_code == 429:
                wait_seconds = min(get_retry_after_seconds(response), _DISCOGS_MAX_COOLDOWN)
                _set_rate_limit_window(wait_seconds)
                logger.warning("Discogs rate limited; waiting %ss", int(wait_seconds))
                time.sleep(wait_seconds)
                continue

            if response.status_code in {502, 503}:
                record_discogs_error(str(response.status_code))
                if attempt < max_retries:
                    time.sleep(retry_delay)
                    retry_delay *= 2
                    continue
                return {}

            if response.status_code in {401, 403}:
                logger.error("Discogs authentication/permission failure: %s", response.status_code)
                return {}

            response.raise_for_status()
            clear_discogs_errors()
            payload = response.json()
            return payload if isinstance(payload, dict) else {}

        return {}

    def search_database(self, params: dict[str, Any], timeout: float = 10.0) -> list[dict[str, Any]]:
        """Call /database/search and return the results list."""
        payload = self._request("GET", "/database/search", params=params, timeout=timeout)
        results = payload.get("results", [])
        return results if isinstance(results, list) else []

    def get_release(self, release_id: str | int, timeout: float = 10.0) -> dict[str, Any]:
        """Fetch a release by ID."""
        if not release_id:
            return {}
        return self._request("GET", f"/releases/{release_id}", timeout=timeout)

    def get_master(self, master_id: str | int, timeout: float = 10.0) -> dict[str, Any]:
        """Fetch a master release by ID."""
        if not master_id:
            return {}
        return self._request("GET", f"/masters/{master_id}", timeout=timeout)

    def get_artist(self, artist_id: str | int, timeout: float = 10.0) -> dict[str, Any]:
        """Fetch an artist by ID."""
        if not artist_id:
            return {}
        return self._request("GET", f"/artists/{artist_id}", timeout=timeout)

    def get_artist_releases(self, artist_id: str | int, per_page: int = 100, timeout: float = 10.0) -> list[dict[str, Any]]:
        """Fetch the first page of artist releases."""
        if not artist_id:
            return []
        payload = self._request("GET", f"/artists/{artist_id}/releases", params={"per_page": per_page}, timeout=timeout)
        releases = payload.get("releases", [])
        return releases if isinstance(releases, list) else []

    def get_resource_url(self, url: str, timeout: float = 10.0) -> dict[str, Any]:
        """Fetch a Discogs resource_url returned by another Discogs endpoint."""
        if not url:
            return {}
        return self._request("GET", url, timeout=timeout)

    # ------------------------------------------------------------------
    # Pagination helper
    # ------------------------------------------------------------------

    def _get_all_paginated(self, path: str, params: dict[str, Any] | None = None, max_pages: int = 10) -> list[dict[str, Any]]:
        """Fetch all pages of a paginated Discogs endpoint.

        Args:
            path: API path (e.g. ``/artists/{id}/releases``).
            params: Optional query params.
            max_pages: Maximum pages to fetch (default 10, 0 for no limit).

        Returns:
            Combined list of items from all pages.
        """
        all_items: list[dict[str, Any]] = []
        page = 1
        p_params = dict(params or {})
        p_params.setdefault("per_page", 100)

        while True:
            p_params["page"] = page
            payload = self._request("GET", path, params=p_params)
            if not payload:
                break

            # Auto-detect the items key from response
            items_key = None
            for key in ("releases", "items", "results", "versions"):
                if isinstance(payload.get(key), list):
                    items_key = key
                    break

            if items_key:
                all_items.extend(payload[items_key])

            pagination = payload.get("pagination", {}) or {}
            pages = pagination.get("pages", 1) or 1

            if page >= pages or (max_pages and page >= max_pages):
                break
            page += 1

        return all_items

    def get_artist_releases_all(self, artist_id: str | int, max_pages: int = 10) -> list[dict[str, Any]]:
        """Fetch ALL releases for an artist (all pages).

        Args:
            artist_id: Discogs artist ID.
            max_pages: Maximum pages to fetch (default 10).

        Returns:
            Combined list of release dicts from all pages.
        """
        if not artist_id:
            return []
        return self._get_all_paginated(f"/artists/{artist_id}/releases", max_pages=max_pages)

    # ------------------------------------------------------------------
    # Label endpoints
    # ------------------------------------------------------------------

    def get_label(self, label_id: str | int, timeout: float = 10.0) -> dict[str, Any]:
        """Fetch a label by ID."""
        if not label_id:
            return {}
        return self._request("GET", f"/labels/{label_id}", timeout=timeout)

    def get_label_releases(self, label_id: str | int, per_page: int = 100, timeout: float = 10.0) -> list[dict[str, Any]]:
        """Fetch releases for a label (first page)."""
        if not label_id:
            return []
        payload = self._request("GET", f"/labels/{label_id}/releases", params={"per_page": per_page}, timeout=timeout)
        releases = payload.get("releases", [])
        return releases if isinstance(releases, list) else []

    def get_label_releases_all(self, label_id: str | int, max_pages: int = 10) -> list[dict[str, Any]]:
        """Fetch ALL releases for a label (all pages)."""
        if not label_id:
            return []
        return self._get_all_paginated(f"/labels/{label_id}/releases", max_pages=max_pages)


def fetch_image_bytes(client_or_token, image_url: str) -> bytes | None:
    """Fetch image binary data from a Discogs resource URL using authenticated headers.

    Args:
        client_or_token: Either a ``DiscogsHttpClient`` instance or an API token string.
        image_url: The Discogs image URL to fetch.

    Returns:
        Raw image bytes, or None on failure.
    """
    try:
        if isinstance(client_or_token, DiscogsHttpClient):
            headers = client_or_token.headers
            session = client_or_token.session
        else:
            from api_clients.http_utils import create_retry_client
            session = create_retry_client()
            headers = {"Authorization": f"Discogs token={client_or_token}", "User-Agent": DEFAULT_USER_AGENT}

        response = session.get(image_url, headers=headers, timeout=15)
        if response.status_code == 200:
            return response.content
        return None
    except Exception as exc:
        logger.debug("Failed to fetch Discogs binary artwork: %s", exc)
        return None