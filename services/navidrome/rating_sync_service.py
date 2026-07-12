"""Navidrome rating sync helpers.

Handles synchronisation of ratings between Popularr and Navidrome.
Provides:
    - navidrome_scan_running(): Check if Navidrome is currently scanning.
    - load_navidrome_users_from_config(): Load Navidrome user credentials.

Uses a progress file to detect active Navidrome scans, preventing
concurrent operations that could cause conflicts.
"""
from __future__ import annotations
import json

from helpers.config_helpers import get_navidrome_progress_file

NAVIDROME_PROGRESS_FILE = get_navidrome_progress_file()


def navidrome_scan_running() -> bool:
    try:
        if os.path.exists(NAVIDROME_PROGRESS_FILE):
            with open(NAVIDROME_PROGRESS_FILE, "r", encoding="utf-8") as handle:
                return bool(json.load(handle).get("is_running"))
    except Exception:
        return False
    return False


def load_navidrome_users_from_config() -> list[dict]:
    try:
        from helpers.config_helpers import get_config
        cfg = get_config() or {}
        users = cfg.get("navidrome_users") or []
        if not users and cfg.get("navidrome"):
            users = [cfg["navidrome"]]
        return users
    except Exception:
        return []


def is_sync_ratings_to_all_users_enabled() -> bool:
    try:
        from helpers.config_helpers import get_config
        return bool((get_config() or {}).get("features", {}).get("sync_ratings_to_all_users", False))
    except Exception:
        return False


def sync_track_rating_to_navidrome(track_id: str, stars: int) -> bool:
    try:
        from api_clients.navidrome import NavidromeClient
        users = load_navidrome_users_from_config()
        if not is_sync_ratings_to_all_users_enabled() and users:
            users = users[:1]
        ok = False
        for user in users:
            client = NavidromeClient(base_url=user.get("base_url", ""), username=user.get("user", user.get("username", "")), password=user.get("pass", user.get("password", "")))
            ok = bool(client.set_rating(track_id, stars)) or ok
        return ok
    except Exception:
        return False

