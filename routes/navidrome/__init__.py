"""Navidrome route package.

A single blueprint is shared by playlist, scan and ratings route modules.
"""

from __future__ import annotations

from quart import Blueprint, session

from api_clients.navidrome import NavidromeClient
from helpers.config_helpers import get_config

navidrome_bp = Blueprint("navidrome_api", __name__)


def get_navidrome_client() -> NavidromeClient | None:
    """Return a configured Navidrome client for the active user.

    Multi-user config is preferred when a Flask session username is present;
    otherwise the legacy single-user ``navidrome`` config is used.
    """
    cfg = get_config() or {}
    nav_users = cfg.get("navidrome_users", []) or []
    current_user = session.get("username")

    if nav_users and current_user:
        match = next((item for item in nav_users if item.get("user") == current_user), None)
        if match:
            return NavidromeClient(
                base_url=match.get("base_url", ""),
                username=match.get("user", match.get("username", "")),
                password=match.get("pass", match.get("password", "")),
            )

    nav_cfg = cfg.get("navidrome", {}) if isinstance(cfg.get("navidrome"), dict) else {}
    if nav_cfg.get("base_url"):
        return NavidromeClient(
            base_url=nav_cfg.get("base_url", ""),
            username=nav_cfg.get("user", nav_cfg.get("username", "")),
            password=nav_cfg.get("pass", nav_cfg.get("password", "")),
        )

    return None


from routes.navidrome import playlists  # noqa: E402,F401
from routes.navidrome import ratings  # noqa: E402,F401
from routes.navidrome import scan  # noqa: E402,F401

__all__ = ["navidrome_bp", "get_navidrome_client"]
