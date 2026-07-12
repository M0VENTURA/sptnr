"""Navidrome scan/data access wrappers.

Lightweight wrappers around Navidrome API client for scan workflow
integration. Provides cached client instances and config resolution.

Key Functions:
    - get_navidrome_config(): Load Navidrome connection configuration
      from config.yaml (supports both single and multi-user configs).

Architecture:
    Maintains a module-level client cache to avoid re-authentication
    on every call. This is a scan helper, not a raw HTTP client.
"""

from __future__ import annotations

from api_clients.navidrome import NavidromeClient
from db.utils import get_db_connection, row_get
from helpers.config_helpers import get_config

_nav_client_cache: NavidromeClient | None = None


def get_navidrome_config() -> dict | None:
    try:
        

        cfg = get_config() or {}

        users = cfg.get("navidrome_users") or []

        if not users:
            nav = cfg.get("navidrome") or {}
            if nav.get("base_url"):
                users = [nav]

        if users:
            first = users[0]
            return {
                "base_url": first.get("base_url", ""),
                "user": first.get("user", first.get("username", "")),
                "pass": first.get("pass", first.get("password", "")),
            }

    except Exception as exc:
        logger.debug("Could not load Navidrome config: %s", exc)

    return None


def get_nav_client() -> NavidromeClient:
    global _nav_client_cache

    if _nav_client_cache:
        return _nav_client_cache

    cfg = get_navidrome_config()
    if not cfg:
        raise RuntimeError("Navidrome config missing")

    _nav_client_cache = NavidromeClient(
        base_url=cfg["base_url"],
        username=cfg["user"],
        password=cfg["pass"],
    )

    return _nav_client_cache


def fetch_artist_albums(artist_id):
    return get_nav_client().fetch_artist_albums(artist_id)


def fetch_album_tracks(album_id):
    return get_nav_client().fetch_album_tracks(album_id)


def build_artist_index():
    return get_nav_client().build_artist_index()


def load_artist_map():
    conn = get_db_connection()

    try:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT artist_id, artist_name, album_count, track_count
            FROM artist_stats
        """)

        return {
            row_get(row, "artist_name", 1): {
                "id": row_get(row, "artist_id", 0),
                "album_count": row_get(row, "album_count", 2),
                "track_count": row_get(row, "track_count", 3),
            }
            for row in cursor.fetchall() or []
        }

    finally:
        conn.close()