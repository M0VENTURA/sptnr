"""Low-level slskd HTTP client.

Owns raw slskd request mechanics only. Search/result interpretation,
quality filtering, and queue/download workflows live in services.downloads.

API reference: https://slskd-api.readthedocs.io/en/latest/api.html
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from api_clients import session

logger = logging.getLogger(__name__)


class SlskdHttpClient:
    """Raw slskd API wrapper."""

    def __init__(self, web_url: str, api_key: str = "", http_session=None, enabled: bool = True, default_timeout: Optional[int] = 15):
        self.web_url = (web_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.session = http_session or session
        self.enabled = enabled
        self.default_timeout = default_timeout
        self.base_url = f"{self.web_url}/api/v0"
        self.headers = {"X-API-Key": self.api_key} if self.api_key else {}

    # ------------------------------------------------------------------
    # Core request helpers
    # ------------------------------------------------------------------

    def request(self, method: str, endpoint: str, *, timeout: Optional[int] = None, **kwargs):
        if not self.enabled:
            raise RuntimeError("slskd client is disabled")
        timeout = timeout or self.default_timeout
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        return self.session.request(method, url, headers=self.headers, timeout=timeout, **kwargs)

    def get_json(self, endpoint: str, *, timeout: Optional[int] = None, default=None, **kwargs):
        resp = self.request("GET", endpoint, timeout=timeout, **kwargs)
        resp.raise_for_status()
        payload = resp.json()
        return payload if payload is not None else default

    def post_json(self, endpoint: str, payload: Any, *, timeout: Optional[int] = None):
        return self.request("POST", endpoint, json=payload, timeout=timeout)

    def delete(self, endpoint: str, *, timeout: Optional[int] = None):
        return self.request("DELETE", endpoint, timeout=timeout)

    def put(self, endpoint: str, *, timeout: Optional[int] = None):
        return self.request("PUT", endpoint, timeout=timeout)

    # ------------------------------------------------------------------
    # Search management
    # ------------------------------------------------------------------

    def start_search(self, query: str, timeout: int = 20) -> str | None:
        """Start a new Soulseek search. Returns the search ID."""
        try:
            resp = self.post_json("searches", {"searchText": query}, timeout=timeout)
            data = resp.json() if hasattr(resp, "json") else resp
            return (data if isinstance(data, str) else data.get("id") or data.get("searchId")) or None
        except Exception as exc:
            logger.debug("Failed to start search: %s", exc)
            return None

    def list_searches(self, timeout: int = 8) -> list[dict[str, Any]]:
        """List all searches and their states."""
        try:
            return self.get_json("searches", timeout=timeout, default=[])
        except Exception as exc:
            logger.debug("Failed to list searches: %s", exc)
            return []

    def get_search_results(self, search_id: str, timeout: int = 10) -> list[dict[str, Any]]:
        """Get results for a completed search."""
        try:
            return self.get_json(f"searches/{search_id}/results", timeout=timeout, default=[])
        except Exception as exc:
            logger.debug("Failed to get search results: %s", exc)
            return []

    def stop_search(self, search_id: str) -> bool:
        """Stop a running search."""
        try:
            resp = self.post_json(f"searches/{search_id}/stop", {})
            return resp.status_code in (200, 204)
        except Exception as exc:
            logger.debug("Failed to stop search %s: %s", search_id, exc)
            return False

    def delete_search(self, search_id: str) -> bool:
        """Delete a search and its results."""
        try:
            resp = self.delete(f"searches/{search_id}")
            return resp.status_code in (200, 204)
        except Exception as exc:
            logger.debug("Failed to delete search %s: %s", search_id, exc)
            return False

    # ------------------------------------------------------------------
    # Transfer management
    # ------------------------------------------------------------------

    def enqueue_download(self, username: str, filename: str, timeout: int = 15) -> dict[str, Any]:
        """Queue a file for download from a user."""
        try:
            resp = self.post_json(
                "transfers/downloads",
                {"username": username, "filename": filename},
                timeout=timeout,
            )
            return resp.json() if hasattr(resp, "json") else {}
        except Exception as exc:
            logger.debug("Failed to enqueue download: %s", exc)
            return {}

    def get_active_downloads(self, timeout: int = 10) -> list[dict[str, Any]]:
        """Get all active/in-progress downloads."""
        try:
            return self.get_json("transfers/downloads", timeout=timeout, default=[])
        except Exception as exc:
            logger.debug("Failed to get active downloads: %s", exc)
            return []

    def retry_download(self, username: str, filename: str, timeout: int = 10) -> bool:
        """Retry a failed download."""
        try:
            resp = self.post_json(
                "transfers/downloads/retry",
                {"username": username, "filename": filename},
                timeout=timeout,
            )
            return resp.status_code in (200, 204) if hasattr(resp, "status_code") else True
        except Exception as exc:
            logger.debug("Failed to retry download: %s", exc)
            return False

    def get_download(self, download_id: str) -> dict[str, Any]:
        """Get details for a specific download."""
        try:
            return self.get_json(f"transfers/downloads/{download_id}", default={})
        except Exception as exc:
            logger.debug("Failed to get download %s: %s", download_id, exc)
            return {}

    def cancel_download(self, download_id_or_username: str, filename: str | None = None, transfer_id: str | None = None) -> bool:
        """Cancel a specific download.

        Accepts either a ``download_id`` (str) or ``(username, filename)``
        for backward compatibility with legacy route callers.
        """
        try:
            if filename is not None:
                # Legacy signature: (username, filename, transfer_id?)
                resp = self.delete(f"transfers/downloads/{download_id_or_username}/{filename}")
            else:
                resp = self.delete(f"transfers/downloads/{download_id_or_username}")
            return resp.status_code in (200, 204)
        except Exception as exc:
            logger.debug("Failed to cancel download: %s", exc)
            return False

    def get_events(self, timeout: int = 10) -> list[dict[str, Any]]:
        """Get recent slskd events."""
        try:
            return self.get_json("events", timeout=timeout, default=[])
        except Exception as exc:
            logger.debug("Failed to get events: %s", exc)
            return []

    def remove_completed_downloads(self) -> bool:
        """Remove all completed downloads from the queue."""
        try:
            resp = self.delete("transfers/downloads/completed")
            return resp.status_code in (200, 204)
        except Exception as exc:
            logger.debug("Failed to remove completed downloads: %s", exc)
            return False

    def get_queue_position(self, download_id: str) -> Optional[int]:
        """Get the queue position for a queued download."""
        try:
            data = self.get_json(f"transfers/downloads/{download_id}/position", default={})
            return data.get("position") if isinstance(data, dict) else None
        except Exception as exc:
            logger.debug("Failed to get queue position for %s: %s", download_id, exc)
            return None

    # ------------------------------------------------------------------
    # User operations (browse, info, status)
    # ------------------------------------------------------------------

    def browse_user(self, username: str) -> dict[str, Any]:
        """Browse a user's shared files."""
        try:
            return self.get_json(f"users/{username}/browse", default={}, timeout=30)
        except Exception as exc:
            logger.debug("Failed to browse user %s: %s", username, exc)
            return {}

    def get_user_info(self, username: str) -> dict[str, Any]:
        """Get user info (description, upload slots, etc.)."""
        try:
            return self.get_json(f"users/{username}/info", default={})
        except Exception as exc:
            logger.debug("Failed to get user info for %s: %s", username, exc)
            return {}

    def get_user_status(self, username: str) -> dict[str, Any]:
        """Get user online/away status."""
        try:
            return self.get_json(f"users/{username}/status", default={})
        except Exception as exc:
            logger.debug("Failed to get user status for %s: %s", username, exc)
            return {}

    # ------------------------------------------------------------------
    # Application & session helpers
    # ------------------------------------------------------------------

    def get_state(self) -> dict[str, Any]:
        """Get the full slskd application state."""
        try:
            return self.get_json("application/state", default={})
        except Exception as exc:
            logger.debug("Failed to get application state: %s", exc)
            return {}

    def is_connected(self) -> bool:
        """Return True when the Soulseek client is connected and logged in."""
        try:
            state = self.get_state()
            server = state.get("server", {}) if isinstance(state, dict) else {}
            return bool(server.get("isConnected") or server.get("isLoggedIn"))
        except Exception:
            return False


# =============================================================================
# Client factory (cached)
# =============================================================================

_slskd_client_cache: SlskdHttpClient | None = None


def get_slskd_client() -> SlskdHttpClient | None:
    """Return a configured, cached ``SlskdHttpClient`` instance.

    Reads ``slskd`` from config (``enabled`` / ``web_url`` / ``api_key``).
    Returns ``None`` when slskd is disabled or misconfigured so callers can
    treat it as "Soulseek unavailable" without raising.
    """
    global _slskd_client_cache
    if _slskd_client_cache is not None:
        return _slskd_client_cache

    try:
        from helpers.config_helpers import get_slskd_config
        cfg = get_slskd_config()

        if not cfg.get("enabled"):
            logger.warning("Soulseek (slskd) is not enabled in config")
            return None

        web_url = str(cfg.get("web_url") or "").strip() or "http://localhost:5030"
        api_key = str(cfg.get("api_key") or "").strip()

        # Use a plain session (no automatic 429 retries) for queue processing.
        # The shared api_clients session retries 429 responses with exponential
        # backoff, which turns a fast "slot busy" into a long wait per search
        # attempt. A plain session lets the caller's own retry loop control the
        # cadence (mirrors the legacy queue_processor factory).
        import httpx as _httpx
        _plain_session = _httpx.Client(timeout=30.0)

        _slskd_client_cache = SlskdHttpClient(
            web_url=web_url,
            api_key=api_key,
            http_session=_plain_session,
            enabled=True,
        )
        return _slskd_client_cache
    except Exception as exc:
        logger.error("Error getting SlskdHttpClient: %s", exc)
        return None
