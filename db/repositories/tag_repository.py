"""Repository functions for editable metadata tags.

DB-only responsibilities from the old helpers/tag_manager.py live here.
Physical file writes live in services.metadata.tag_file_service.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from sqlalchemy import text

from db.engine import db_session
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
    try:
        with db_session() as session:
            result = session.execute(
                text(f"SELECT {', '.join(fields)} FROM tracks WHERE id = :id"),
                {"id": track_id},
            )
            row = result.fetchone()
            if not row:
                return {}
            return {field: _decode_field(field, row[idx]) for idx, field in enumerate(fields)}
    except Exception as exc:
        logger.error("Failed to get tags for track %s: %s", track_id, exc)
        return {}


def get_album_tags(album: str, artist: str) -> dict[str, Any]:
    fields = sorted(ALBUM_LEVEL_FIELDS)
    try:
        with db_session() as session:
            result = session.execute(
                text(f"SELECT COUNT(*) AS track_count, {', '.join(fields)} FROM tracks WHERE album = :album AND artist = :artist GROUP BY {', '.join(fields)} LIMIT 1"),
                {"album": album, "artist": artist},
            )
            row = result.fetchone()
            if not row:
                return {}
            tags = {"track_count": int(row[0])}
            for idx, field in enumerate(fields):
                tags[field] = _decode_field(field, row[idx + 1])
            return tags
    except Exception as exc:
        logger.error("Failed to get album tags for %s - %s: %s", artist, album, exc)
        return {}


def check_field_conflicts(album: str, artist: str) -> dict[str, Any]:
    try:
        with db_session() as session:
            result = session.execute(
                text("SELECT DISTINCT album_artist, albumartist FROM tracks WHERE album = :album AND artist = :artist"),
                {"album": album, "artist": artist},
            )
            rows = result.fetchall() or []
            conflicts: dict[str, Any] = {}
            album_artists = {str(row[0]) for row in rows if row[0]}
            albumartists = {str(row[1]) for row in rows if row[1]}
        if len(album_artists) > 1:
            conflicts["album_artist"] = sorted(album_artists)
        if len(albumartists) > 1:
            conflicts["albumartist"] = sorted(albumartists)
        if album_artists and albumartists and album_artists != albumartists:
            conflicts["album_artist_vs_albumartist"] = {"album_artist": sorted(album_artists), "albumartist": sorted(albumartists)}
        for field in ["label", "releasecountry", "releasetype"]:
            result = session.execute(
                text(f"SELECT DISTINCT {field} FROM tracks WHERE album = :album AND artist = :artist AND {field} IS NOT NULL AND {field} <> ''"),
                {"album": album, "artist": artist},
            )
            values = [str(row[0]) for row in result.fetchall() or [] if row[0]]
            if len(values) > 1:
                conflicts[field] = values
        return conflicts
    except Exception as exc:
        logger.error("Failed to check field conflicts for %s - %s: %s", artist, album, exc)
        return {}


def update_track_tags(track_id: str, tag_updates: dict[str, Any]) -> bool:
    validated = _encode_updates(tag_updates)
    if not validated:
        return False
    try:
        with db_session() as session:
            set_clause = ', '.join([f"{field} = :{field}" for field in validated])
            params = {**validated, "id": track_id}
            session.execute(text(f"UPDATE tracks SET {set_clause} WHERE id = :id"), params)
            return True
    except Exception as exc:
        logger.error("Failed to update tags for track %s: %s", track_id, exc)
        return False


def update_album_tags(album: str, artist: str, tag_updates: dict[str, Any], selected_tracks: list[str] | None = None) -> int:
    validated = _encode_updates(tag_updates)
    if not validated:
        return 0
    set_clause = ', '.join([f"{field} = :{field}" for field in validated])
    params = {**validated, "album": album, "artist": artist}
    if selected_tracks:
        track_placeholders = ', '.join([f":tid_{i}" for i in range(len(selected_tracks))])
        query = f"UPDATE tracks SET {set_clause} WHERE album = :album AND artist = :artist AND id IN ({track_placeholders})"
        params.update({f"tid_{i}": tid for i, tid in enumerate(selected_tracks)})
    else:
        query = f"UPDATE tracks SET {set_clause} WHERE album = :album AND artist = :artist"

    delay = 0.5
    for attempt in range(3):
        try:
            with db_session() as session:
                result = session.execute(text(query), params)
                return result.rowcount
        except Exception as exc:
            if "database is locked" in str(exc).lower() and attempt < 2:
                time.sleep(delay)
                delay *= 2
                continue
            logger.error("Failed to update album tags for %s - %s: %s", artist, album, exc)
            return 0
    return 0
