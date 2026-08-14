"""Folder-match repository queries.

Persists the folder → MusicBrainz release association for the downloads
page's two-phase "Matched Folders" flow:

- Phase 1 (Match): write a row here (folder + release MBID), NO files moved.
- Phase 2 (Confirm Match): the confirm pipeline tags/formats/moves the files
  to the library, then removes the row.

This is the ONLY layer that writes to the ``folder_matches`` table.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import text

from db.engine import db_session

logger = logging.getLogger(__name__)


def upsert_folder_match(
    *,
    folder_path: str,
    release_mbid: str,
    release_title: str | None = None,
    artist: str | None = None,
    release_year: int | None = None,
    status: str = "matched",
) -> Optional[dict[str, Any]]:
    """Create or update the folder → release association (no file movement).

    Returns the stored row as a dict, or None on failure.
    """
    if not folder_path or not release_mbid:
        return None
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    INSERT INTO folder_matches
                        (folder_path, release_mbid, release_title, artist,
                         release_year, status, updated_at)
                    VALUES
                        (:folder_path, :release_mbid, :release_title, :artist,
                         :release_year, :status, CURRENT_TIMESTAMP)
                    ON CONFLICT (folder_path) DO UPDATE SET
                        release_mbid = EXCLUDED.release_mbid,
                        release_title = EXCLUDED.release_title,
                        artist = EXCLUDED.artist,
                        release_year = EXCLUDED.release_year,
                        status = EXCLUDED.status,
                        updated_at = CURRENT_TIMESTAMP
                    RETURNING id, folder_path, release_mbid, release_title,
                              artist, release_year, status, created_at, updated_at
                """),
                {
                    "folder_path": folder_path,
                    "release_mbid": release_mbid,
                    "release_title": release_title,
                    "artist": artist,
                    "release_year": release_year,
                    "status": status,
                },
            )
            row = result.fetchone()
            return dict(row._mapping) if row else None
    except Exception as exc:
        logger.error("[FOLDER_MATCH] upsert failed for %s: %s", folder_path, exc)
        return None


def get_folder_match(folder_path: str) -> Optional[dict[str, Any]]:
    """Return the stored association for *folder_path*, or None."""
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT id, folder_path, release_mbid, release_title,
                           artist, release_year, status, created_at, updated_at
                    FROM folder_matches
                    WHERE folder_path = :folder_path
                    LIMIT 1
                """),
                {"folder_path": folder_path},
            )
            row = result.fetchone()
            return dict(row._mapping) if row else None
    except Exception as exc:
        logger.debug("[FOLDER_MATCH] lookup failed for %s: %s", folder_path, exc)
        return None


def get_all_folder_matches() -> list[dict[str, Any]]:
    """Return every stored folder → release association."""
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT id, folder_path, release_mbid, release_title,
                           artist, release_year, status, created_at, updated_at
                    FROM folder_matches
                    ORDER BY created_at DESC
                """)
            )
            return [dict(r._mapping) for r in result.fetchall()]
    except Exception as exc:
        logger.debug("[FOLDER_MATCH] list failed: %s", exc)
        return []


def delete_folder_match(folder_path: str) -> bool:
    """Remove the association for *folder_path* (post-confirm or on change)."""
    try:
        with db_session() as session:
            result = session.execute(
                text("DELETE FROM folder_matches WHERE folder_path = :folder_path"),
                {"folder_path": folder_path},
            )
            return bool(result.rowcount)
    except Exception as exc:
        logger.debug("[FOLDER_MATCH] delete failed for %s: %s", folder_path, exc)
        return False
