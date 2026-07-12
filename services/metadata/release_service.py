"""MusicBrainz release metadata service.

Provides release-level queries from the cached MusicBrainz tables:
- ``get_release_details`` – Full release metadata including track list.
- ``get_active_releases_with_progress`` – Releases with download status.
- ``get_cached_missing_releases`` – Releases identified as missing.

All data comes from ``musicbrainz_releases`` / ``musicbrainz_release_tracks`` tables.
"""

from __future__ import annotations
import logging
from sqlalchemy import text
from db.engine import db_session
from db.context import db_cursor  # TODO: migrate to db_session
from db.repositories.musicbrainz_cache import get_active_musicbrainz_releases

logger = logging.getLogger(__name__)


from typing import Any


def _row_to_dict(
    cursor,
    row,
) -> dict[str, Any] | None:
    if row is None:
        return None

    colnames = [
        desc[0]
        for desc in cursor.description
    ]

    return dict(
        zip(
            colnames,
            row,
        )
    )


def get_release_details(release_id: str):
    with db_cursor() as (_, cursor):
        cursor.execute("SELECT * FROM musicbrainz_releases WHERE release_id = %s", (release_id,))
        release = _row_to_dict(cursor, cursor.fetchone())

        if not release:
            return None

        cursor.execute("SELECT * FROM musicbrainz_release_tracks WHERE release_id = %s", (release_id,))
        # Map all tracks to dicts
        tracks = [_row_to_dict(cursor, row) for row in cursor.fetchall()]

    return {"release": release, "tracks": tracks}

def get_cached_missing_releases(artist: str):
    if not artist:
        return {"success": False, "error": "Artist is required"}, 400

    try:
        with db_cursor() as (_, cursor):
            cursor.execute("""
                SELECT release_id, title, primary_type, first_release_date, 
                       cover_art_url, category, last_checked
                FROM missing_releases
                WHERE artist = %s
                ORDER BY first_release_date DESC
            """, (artist,))
            
            rows = [
                    r
                    for row in (cursor.fetchall() or [])
                    if (r := _row_to_dict(cursor, row)) is not None
                ]

        return {
            "artist": artist,
            "missing": [
                {
                    "id": r.get("release_id", ""),
                    "title": r.get("title", ""),
                    "primary_type": r.get("primary_type", "Album"),
                    "first_release_date": str(r.get("first_release_date", "")),
                    "cover_art_url": r.get("cover_art_url", ""),
                    "category": r.get("category", "Album"),
                    "last_checked": str(r.get("last_checked", "")),
                } for r in rows
            ],
            "from_cache": True
        }, 200

    except Exception as e:
        logger.error("[MISSING_RELEASES] %s", e)
        return {"success": False, "error": str(e)}, 500

def get_active_releases_with_progress():
    # Calling the repository function we defined earlier
    releases = get_active_musicbrainz_releases()
    
    for r in releases:
        total = r.get("total_tracks", 0) or 0
        discovered = r.get("discovered_count", 0) or 0
        r["progress_percent"] = int((discovered / total * 100) if total > 0 else 0)
        
    return releases