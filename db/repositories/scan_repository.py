"""
db/repositories/scandb/repositories/scan_repository.py

Design rules:
- No API calls
- No Navidrome logic
- No orchestration
- Database-focused only
"""

from __future__ import annotations

import logging
import os
from typing import Any

from sqlalchemy import text

from db.engine import db_session
from helpers.text_utils import (
    _normalize_artist_key,
    _resolve_navidrome_file_path_for_storage,
)

# =============================================================================
# READ HELPERS
# =============================================================================


def lookup_artist_id(artist_name: str) -> str | None:
    """Return cached Navidrome artist_id for artist_name."""
    if not artist_name:
        return None

    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT artist_id
                    FROM artist_stats
                    WHERE artist_name = :artist_name
                    LIMIT 1
                """),
                {"artist_name": artist_name},
            )
            row = result.fetchone()

        return row[0] if row else None

    except Exception as exc:
        logging.debug("[SCAN_DB] Artist ID lookup failed for '%s': %s", artist_name, exc)
        return None


def lookup_track_artist_count(artist_name: str) -> int:
    """Return number of tracks where artist_name is the track artist."""
    if not artist_name:
        return 0

    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT COUNT(*) AS cnt
                    FROM tracks
                    WHERE artist = :artist_name
                """),
                {"artist_name": artist_name},
            )
            row = result.fetchone()

        return int(row[0] or 0) if row else 0

    except Exception as exc:
        logging.debug("[SCAN_DB] Track artist count failed for '%s': %s", artist_name, exc)
        return 0


def get_database_library_stats() -> dict[str, int]:
    """Return total album + track counts."""
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT COUNT(DISTINCT album) AS cnt
                    FROM tracks
                    WHERE album IS NOT NULL AND album != ''
                """),
            )
            total_albums = int(result.scalar() or 0)

            result = session.execute(
                text("SELECT COUNT(*) AS cnt FROM tracks"),
            )
            total_tracks = int(result.scalar() or 0)

        return {
            "total_albums": total_albums,
            "total_tracks": total_tracks,
        }

    except Exception as exc:
        logging.debug("[SCAN_DB] Failed to get DB stats: %s", exc, exc_info=True)
        return {"total_albums": 0, "total_tracks": 0}


def get_existing_track_ids() -> set[str]:
    """Return all known track IDs."""
    try:
        with db_session() as session:
            result = session.execute(text("SELECT id FROM tracks"))
            rows = result.fetchall() or []

        return {
            str(row[0])
            for row in rows
            if row[0]
        }

    except Exception as exc:
        logging.debug("[SCAN_DB] Failed to fetch track IDs: %s", exc, exc_info=True)
        return set()


def get_existing_artist_track_counts() -> dict[str, int]:
    """Return track counts grouped by artist."""
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT artist, COUNT(*) AS track_count
                    FROM tracks
                    WHERE artist IS NOT NULL AND artist != ''
                    GROUP BY artist
                """),
            )
            rows = result.fetchall() or []

        result_dict: dict[str, int] = {}

        for row in rows:
            artist = str(row[0]) if row[0] else None
            count = int(row[1] or 0) if len(row) > 1 else 0

            if artist:
                result_dict[artist] = count

        return result_dict

    except Exception as exc:
        logging.debug("[SCAN_DB] Failed to fetch artist counts: %s", exc, exc_info=True)
        return {}


# =============================================================================
# VALIDATION HELPERS
# =============================================================================


def has_valid_local_track_paths_for_mp3_import(
    sample_size: int = 120,
) -> tuple[bool, str]:
    """Validate that DB file paths exist on this host."""
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT file_path
                    FROM tracks
                    WHERE file_path IS NOT NULL
                      AND file_path <> ''
                      AND file_path NOT LIKE '__queued_for_download__%%'
                    ORDER BY id DESC
                    LIMIT :sample_size
                """),
                {"sample_size": sample_size},
            )
            rows = result.fetchall() or []

        if not rows:
            return False, "No track file paths available"

        checked = 0
        existing = 0

        for row in rows:
            file_path = str(row[0] or "").strip() if row else ""

            if not file_path:
                continue

            checked += 1

            if os.path.exists(file_path):
                existing += 1

        if checked == 0:
            return False, "No usable file paths"

        if existing == 0:
            return False, f"0/{checked} paths exist"

        return True, f"{existing}/{checked} paths valid"

    except Exception as exc:
        return False, f"Validation error: {exc}"


# =============================================================================
# CLEANUP HELPERS (WRITE OPERATIONS)
# =============================================================================


def normalize_existing_artist_rows(
    session: Any | None = None,
    canonical_artist_name: str | None = None,
    aliases: list[str] | None = None,
) -> int:
    """Rewrite variant artist names to canonical value.
    
    Args:
        session: Optional SQLAlchemy session. If None, creates one.
        canonical_artist_name: The canonical name to normalize to.
        aliases: List of variant names to normalize.
        
    Returns:
        Number of rows updated.
    """
    if not canonical_artist_name:
        return 0

    canonical_key = _normalize_artist_key(canonical_artist_name)
    if not canonical_key:
        return 0

    updates = 0

    alias_candidates = {alias for alias in (aliases or []) if alias}
    alias_candidates.update({
        canonical_artist_name,
        canonical_artist_name.lower(),
        canonical_artist_name.upper(),
        canonical_artist_name.title(),
    })

    def _do_update(sess):
        nonlocal updates
        for original in alias_candidates:
            if not original or original == canonical_artist_name:
                continue

            if _normalize_artist_key(original) != canonical_key:
                continue

            for column_name in ("album_artist", "artist"):
                result = sess.execute(
                    text(f"""
                    UPDATE tracks
                    SET {column_name} = :canonical
                    WHERE {column_name} = :original
                    """),
                    {"canonical": canonical_artist_name, "original": original},
                )
                updates += max(int(result.rowcount or 0), 0)

    if session is not None:
        _do_update(session)
    else:
        with db_session() as sess:
            _do_update(sess)

    return updates


def sanitize_artist_file_paths_and_duplicates(
    artist_name: str,
    session: Any | None = None,
) -> dict[str, int]:
    """Normalize file paths and remove duplicate rows.
    
    Args:
        artist_name: Name of the artist to sanitize.
        session: Optional SQLAlchemy session. If None, creates one.
        
    Returns:
        Dict with path_updates and duplicates_removed counts.
    """
    if not artist_name:
        return {"path_updates": 0, "duplicates_removed": 0}

    def _do_sanitize(sess):
        result = sess.execute(
            text("""
                SELECT id, file_path, duration, mbid, last_scanned
                FROM tracks
                WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist_name
                  AND file_path IS NOT NULL
                  AND TRIM(file_path) != ''
            """),
            {"artist_name": artist_name},
        )

        rows = result.fetchall() or []
        if not rows:
            return {"path_updates": 0, "duplicates_removed": 0}

        music_root = os.environ.get("MUSIC_FOLDER") or os.environ.get("MUSIC_ROOT", "/music")

        grouped: dict[str, list[dict[str, Any]]] = {}
        path_updates = 0

        for row in rows:
            track_id = row[0]
            raw_path = str(row[1] or "")

            normalized_path = _resolve_navidrome_file_path_for_storage(raw_path, music_root)

            if normalized_path and normalized_path != raw_path:
                sess.execute(
                    text("UPDATE tracks SET file_path = :path WHERE id = :id"),
                    {"path": normalized_path, "id": track_id},
                )
                path_updates += 1

            effective_path = normalized_path or raw_path

            if not effective_path or effective_path.startswith("__queued_for_download__"):
                continue

            grouped.setdefault(effective_path.lower(), []).append({
                "id": track_id,
                "duration": row[2],
                "mbid": row[3],
                "last_scanned": row[4],
            })

        duplicates_removed = 0

        for rows_list in grouped.values():
            if len(rows_list) <= 1:
                continue

            def score(r):
                return (
                    100 if r["mbid"] else 0,
                    10 if r["duration"] else 0,
                    str(r["last_scanned"] or ""),
                )

            keeper = max(rows_list, key=score)

            for r in rows_list:
                if r["id"] == keeper["id"]:
                    continue

                sess.execute(
                    text("DELETE FROM tracks WHERE id = :id"),
                    {"id": r["id"]},
                )
                duplicates_removed += 1

        return {
            "path_updates": path_updates,
            "duplicates_removed": duplicates_removed,
        }

    if session is not None:
        return _do_sanitize(session)
    else:
        with db_session() as sess:
            return _do_sanitize(sess)

