"""Repository functions for editable metadata tags.

DB-only responsibilities from the old helpers/tag_manager.py live here.
Physical file writes live in services.metadata.tag_file_service.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from db.utils import get_db_connection, row_get
from services.metadata.tag_constants import ALBUM_LEVEL_FIELDS, EDITABLE_FIELDS, JSON_ARRAY_FIELDS

logger = logging.getLogger(__name__)


def _decode_field(field: str, value: Any) -> Any:
    if field in JSON_ARRAY_FIELDS and isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return []
    return value


def _encode_updates(tag_updates: dict[str, Any]) -> dict[str, Any]:
    validated: dict[str, Any] = {}
    for field, value in tag_updates.items():
        if field not in EDITABLE_FIELDS:
            logger.warning("Ignoring non-editable metadata field: %s", field)
            continue
        if field in JSON_ARRAY_FIELDS:
            if isinstance(value, list):
                value = json.dumps(value)
            elif isinstance(value, str):
                try:
                    json.loads(value)
                except Exception:
                    value = json.dumps([value])
        validated[field] = value
    return validated


def get_track_tags(track_id: str) -> dict[str, Any]:
    fields = sorted(EDITABLE_FIELDS)
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(f"SELECT {', '.join(fields)} FROM tracks WHERE id = %s", (track_id,))
        row = cursor.fetchone()
        if not row:
            return {}
        return {field: _decode_field(field, row_get(row, field, idx)) for idx, field in enumerate(fields)}
    except Exception as exc:
        logger.error("Failed to get tags for track %s: %s", track_id, exc)
        return {}
    finally:
        if conn:
            conn.close()


def get_album_tags(album: str, artist: str) -> dict[str, Any]:
    fields = sorted(ALBUM_LEVEL_FIELDS)
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT COUNT(*) AS track_count, {', '.join(fields)} FROM tracks WHERE album = %s AND artist = %s GROUP BY {', '.join(fields)} LIMIT 1",
            (album, artist),
        )
        row = cursor.fetchone()
        if not row:
            return {}
        tags = {"track_count": row_get(row, "track_count", 0)}
        for idx, field in enumerate(fields, start=1):
            tags[field] = _decode_field(field, row_get(row, field, idx))
        return tags
    except Exception as exc:
        logger.error("Failed to get album tags for %s - %s: %s", artist, album, exc)
        return {}
    finally:
        if conn:
            conn.close()


def check_field_conflicts(album: str, artist: str) -> dict[str, Any]:
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        conflicts: dict[str, Any] = {}
        cursor.execute("SELECT DISTINCT album_artist, albumartist FROM tracks WHERE album = %s AND artist = %s", (album, artist))
        rows = cursor.fetchall() or []
        album_artists = {row_get(row, "album_artist", 0) for row in rows if row_get(row, "album_artist", 0)}
        albumartists = {row_get(row, "albumartist", 1) for row in rows if row_get(row, "albumartist", 1)}
        if len(album_artists) > 1:
            conflicts["album_artist"] = sorted(album_artists)
        if len(albumartists) > 1:
            conflicts["albumartist"] = sorted(albumartists)
        if album_artists and albumartists and album_artists != albumartists:
            conflicts["album_artist_vs_albumartist"] = {"album_artist": sorted(album_artists), "albumartist": sorted(albumartists)}
        for field in ["label", "releasecountry", "releasetype"]:
            cursor.execute(f"SELECT DISTINCT {field} FROM tracks WHERE album = %s AND artist = %s AND {field} IS NOT NULL AND {field} <> ''", (album, artist))
            values = [row_get(row, field, 0) for row in cursor.fetchall() or []]
            if len(values) > 1:
                conflicts[field] = values
        return conflicts
    except Exception as exc:
        logger.error("Failed to check field conflicts for %s - %s: %s", artist, album, exc)
        return {}
    finally:
        if conn:
            conn.close()


def update_track_tags(track_id: str, tag_updates: dict[str, Any]) -> bool:
    validated = _encode_updates(tag_updates)
    if not validated:
        return False
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        set_clause = ', '.join([f"{field} = %s" for field in validated])
        cursor.execute(f"UPDATE tracks SET {set_clause} WHERE id = %s", [*validated.values(), track_id])
        conn.commit()
        return True
    except Exception as exc:
        logger.error("Failed to update tags for track %s: %s", track_id, exc)
        if conn:
            conn.rollback()
        return False
    finally:
        if conn:
            conn.close()


def update_album_tags(album: str, artist: str, tag_updates: dict[str, Any], selected_tracks: list[str] | None = None) -> int:
    validated = _encode_updates(tag_updates)
    if not validated:
        return 0
    set_clause = ', '.join([f"{field} = %s" for field in validated])
    values = list(validated.values()) + [album, artist]
    if selected_tracks:
        track_placeholders = ', '.join(['%s'] * len(selected_tracks))
        query = f"UPDATE tracks SET {set_clause} WHERE album = %s AND artist = %s AND id IN ({track_placeholders})"
        values.extend(selected_tracks)
    else:
        query = f"UPDATE tracks SET {set_clause} WHERE album = %s AND artist = %s"

    delay = 0.5
    for attempt in range(3):
        conn = None
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute(query, values)
            count = cursor.rowcount
            conn.commit()
            return count
        except Exception as exc:
            if conn:
                conn.rollback()
            if "database is locked" in str(exc).lower() and attempt < 2:
                time.sleep(delay)
                delay *= 2
                continue
            logger.error("Failed to update album tags for %s - %s: %s", artist, album, exc)
            return 0
        finally:
            if conn:
                conn.close()
    return 0
