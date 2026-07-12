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

    def get_download(self, download_id: str) -> dict[str, Any]:
        """Get details for a specific download."""
        try:
            return self.get_json(f"transfers/downloads/{download_id}", default={})
        except Exception as exc:
            logger.debug("Failed to get download %s: %s", download_id, exc)
            return {}

    def cancel_download(self, download_id: str) -> bool:
        """Cancel a specific download."""
        try:
            resp = self.delete(f"transfers/downloads/{download_id}")
            return resp.status_code in (200, 204)
        except Exception as exc:
            logger.debug("Failed to cancel download %s: %s", download_id, exc)
            return False

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
