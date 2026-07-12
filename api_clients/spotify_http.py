"""Low-level Spotify HTTP client.

Owns token acquisition and raw Spotify endpoint calls only.
Spotify metadata/single interpretation lives in services.enrichment.spotify_service.
"""

from __future__ import annotations

import base64
import logging
import threading
import time
from typing import Any

from api_clients import session

logger = logging.getLogger(__name__)

_spotify_token = None
_spotify_token_exp = 0
_token_lock = threading.Lock()


class SpotifyHttpClient:
    """Raw Spotify Web API wrapper using Client Credentials auth."""

    def __init__(self, client_id: str, client_secret: str, http_session=None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.session = http_session or session
        self.base_url = "https://api.spotify.com/v1"

    def get_token(self) -> str:
        global _spotify_token, _spotify_token_exp
        if _spotify_token and time.time() < (_spotify_token_exp - 60):
            return _spotify_token
        with _token_lock:
            if _spotify_token and time.time() < (_spotify_token_exp - 60):
                return _spotify_token
        auth_str = f"{self.client_id}:{self.client_secret}"
        headers = {"Authorization": "Basic " + base64.b64encode(auth_str.encode()).decode(), "Content-Type": "application/x-www-form-urlencoded"}
        resp = self.session.post("https://accounts.spotify.com/api/token", headers=headers, data={"grant_type": "client_credentials"}, timeout=10.0)
        resp.raise_for_status()
        payload = resp.json()
        with _token_lock:
            _spotify_token = payload["access_token"]
            _spotify_token_exp = time.time() + int(payload.get("expires_in", 3600))
        return _spotify_token

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.get_token()}", "Content-Type": "application/json"}

    def get(self, endpoint_or_url: str, *, params: dict[str, Any] | None = None, timeout: float = 10.0):
        url = endpoint_or_url if endpoint_or_url.startswith("http") else f"{self.base_url}/{endpoint_or_url.lstrip('/')}"
        return self.session.get(url, headers=self.headers(), params=params, timeout=timeout)

    def get_json(self, endpoint_or_url: str, *, params: dict[str, Any] | None = None, timeout: float = 10.0, default=None):
        resp = self.get(endpoint_or_url, params=params, timeout=timeout)
        resp.raise_for_status()
        payload = resp.json()
        return payload if payload is not None else default

    def search(self, query: str, search_type: str, limit: int = 10) -> dict:
        return self.get_json("search", params={"q": query, "type": search_type, "limit": limit}, timeout=10.0, default={})
