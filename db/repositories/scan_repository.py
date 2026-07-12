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

from db.context import db_cursor
from db.utils import get_db_connection, row_get
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
        with db_cursor() as (_conn, cursor):
            cursor.execute(
                """
                SELECT artist_id
                FROM artist_stats
                WHERE artist_name = %s
                LIMIT 1
                """,
                (artist_name,),
            )
            row = cursor.fetchone()

        return row_get(row, "artist_id", 0)

    except Exception as exc:
        logging.debug("[SCAN_DB] Artist ID lookup failed for '%s': %s", artist_name, exc)
        return None


def lookup_track_artist_count(artist_name: str) -> int:
    """Return number of tracks where artist_name is the track artist."""
    if not artist_name:
        return 0

    try:
        with db_cursor() as (_conn, cursor):
            cursor.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM tracks
                WHERE artist = %s
                """,
                (artist_name,),
            )
            row = cursor.fetchone()

        return int(row_get(row, "cnt", 0, 0) or 0)

    except Exception as exc:
        logging.debug("[SCAN_DB] Track artist count failed for '%s': %s", artist_name, exc)
        return 0


def get_database_library_stats() -> dict[str, int]:
    """Return total album + track counts."""
    try:
        with db_cursor() as (_conn, cursor):
            cursor.execute(
                """
                SELECT COUNT(DISTINCT album) AS cnt
                FROM tracks
                WHERE album IS NOT NULL AND album != ''
                """
            )
            album_row = cursor.fetchone()
            total_albums = int(row_get(album_row, "cnt", 0, 0) or 0)

            cursor.execute("SELECT COUNT(*) AS cnt FROM tracks")
            track_row = cursor.fetchone()
            total_tracks = int(row_get(track_row, "cnt", 0, 0) or 0)

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
        with db_cursor() as (_conn, cursor):
            cursor.execute("SELECT id FROM tracks")
            rows = cursor.fetchall() or []

        return {
            str(row_get(row, "id", 0))
            for row in rows
            if row_get(row, "id", 0)
        }

    except Exception as exc:
        logging.debug("[SCAN_DB] Failed to fetch track IDs: %s", exc, exc_info=True)
        return set()


def get_existing_artist_track_counts() -> dict[str, int]:
    """Return track counts grouped by artist."""
    try:
        with db_cursor() as (_conn, cursor):
            cursor.execute(
                """
                SELECT artist, COUNT(*) AS track_count
                FROM tracks
                WHERE artist IS NOT NULL AND artist != ''
                GROUP BY artist
                """
            )
            rows = cursor.fetchall() or []

        result: dict[str, int] = {}

        for row in rows:
            artist = row_get(row, "artist", 0)
            count = row_get(row, "track_count", 1, 0)

            if artist:
                result[str(artist)] = int(count or 0)

        return result

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
    conn = None

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT file_path
            FROM tracks
            WHERE file_path IS NOT NULL
              AND file_path <> ''
              AND file_path NOT LIKE '__queued_for_download__%%'
            ORDER BY id DESC
            LIMIT %s
            """,
            (sample_size,),
        )

        rows = cursor.fetchall() or []

        if not rows:
            return False, "No track file paths available"

        checked = 0
        existing = 0

        for row in rows:
            file_path = str(row_get(row, "file_path", 0, "") or "").strip()

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

    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# =============================================================================
# CLEANUP HELPERS (WRITE OPERATIONS)
# =============================================================================


def normalize_existing_artist_rows(
    conn: Any,
    canonical_artist_name: str,
    aliases: list[str] | None = None,
) -> int:
    """Rewrite variant artist names to canonical value."""
    if not canonical_artist_name:
        return 0

    canonical_key = _normalize_artist_key(canonical_artist_name)
    if not canonical_key:
        return 0

    cursor = conn.cursor()
    updates = 0

    alias_candidates = {alias for alias in (aliases or []) if alias}
    alias_candidates.update({
        canonical_artist_name,
        canonical_artist_name.lower(),
        canonical_artist_name.upper(),
        canonical_artist_name.title(),
    })

    for original in alias_candidates:
        if not original or original == canonical_artist_name:
            continue

        if _normalize_artist_key(original) != canonical_key:
            continue

        for column_name in ("album_artist", "artist"):
            cursor.execute(
                f"""
                UPDATE tracks
                SET {column_name} = %s
                WHERE {column_name} = %s
                """,
                (canonical_artist_name, original),
            )

            updates += max(int(cursor.rowcount or 0), 0)

    if updates:
        conn.commit()

    return updates


def sanitize_artist_file_paths_and_duplicates(
    conn: Any,
    artist_name: str,
) -> dict[str, int]:
    """Normalize file paths and remove duplicate rows."""
    if not artist_name:
        return {"path_updates": 0, "duplicates_removed": 0}

    cursor = None

    try:
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT id, file_path, duration, mbid, last_scanned
            FROM tracks
            WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s
              AND file_path IS NOT NULL
              AND TRIM(file_path) != ''
            """,
            (artist_name,),
        )

        rows = cursor.fetchall() or []
        if not rows:
            return {"path_updates": 0, "duplicates_removed": 0}

        music_root = os.environ.get("MUSIC_FOLDER") or os.environ.get("MUSIC_ROOT", "/music")

        grouped: dict[str, list[dict[str, Any]]] = {}
        path_updates = 0

        for row in rows:
            track_id = row_get(row, "id", 0)
            raw_path = str(row_get(row, "file_path", 1, "") or "")

            normalized_path = _resolve_navidrome_file_path_for_storage(raw_path, music_root)

            if normalized_path and normalized_path != raw_path:
                cursor.execute(
                    "UPDATE tracks SET file_path = %s WHERE id = %s",
                    (normalized_path, track_id),
                )
                path_updates += int(cursor.rowcount or 0)

            effective_path = normalized_path or raw_path

            if not effective_path or effective_path.startswith("__queued_for_download__"):
                continue

            grouped.setdefault(effective_path.lower(), []).append({
                "id": track_id,
                "duration": row_get(row, "duration", 2),
                "mbid": row_get(row, "mbid", 3),
                "last_scanned": row_get(row, "last_scanned", 4),
            })

        duplicates_removed = 0

        for rows in grouped.values():
            if len(rows) <= 1:
                continue

            def score(r):
                return (
                    100 if r["mbid"] else 0,
                    10 if r["duration"] else 0,
                    str(r["last_scanned"] or ""),
                )

            keeper = max(rows, key=score)

            for r in rows:
                if r["id"] == keeper["id"]:
                    continue

                cursor.execute(
                    "DELETE FROM tracks WHERE id = %s",
                    (r["id"],),
                )
                duplicates_removed += int(cursor.rowcount or 0)

        if path_updates or duplicates_removed:
            conn.commit()

        return {
            "path_updates": path_updates,
            "duplicates_removed": duplicates_removed,
        }

    except Exception as exc:
        logging.debug("[SCAN_DB] Sanitize failed for '%s': %s", artist_name, exc)
        return {"path_updates": 0, "duplicates_removed": 0}

    finally:
        if cursor:
            try:
                cursor.close()
            except Exception:
                pass

