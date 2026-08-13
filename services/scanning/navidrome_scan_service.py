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
from sqlalchemy import text
from db.engine import db_session
from helpers.config_helpers import get_config

_nav_client_cache: NavidromeClient | None = None


def get_navidrome_config() -> dict | None:
    try:
        from helpers.config_helpers import get_navidrome_first_user
        return get_navidrome_first_user() or None
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
    with db_session() as session:
        result = session.execute(text("""
            SELECT artist_id, artist_name, album_count, track_count
            FROM artist_stats
        """))
        return {
            str(row[1]): {
                "id": str(row[0]),
                "album_count": int(row[2]) if row[2] else 0,
                "track_count": int(row[3]) if row[3] else 0,
            }
            for row in result.fetchall() or []
        }