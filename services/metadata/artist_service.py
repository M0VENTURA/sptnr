"""Artist metadata and correction service.

This replaces the duplicated combination of:
- artist_service.py
- artist_metadata_service.py
- artist_corrections_service.py

Scan-specific MusicBrainz comparison stays in artist_scan_service.py.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from sqlalchemy import text
from db.engine import db_session
from db.utils import get_db_connection
from db.repositories.metadata import (
    fetch_track_for_delete,
    delete_track_row,
    merge_album_names,
    count_album_disc_numbers,
    clear_album_disc_numbers,
    artist_track_count,
    fetch_cached_missing_releases,
    update_album_mbid_fields,
)

logger = logging.getLogger(__name__)


def delete_track(track_id: str, delete_file: bool = True):
    """Delete a track DB row and optionally remove its local file."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        row = fetch_track_for_delete(cursor, track_id)
        if not row:
            return {"success": False, "error": "Track not found"}, 404

        data = dict(row) if hasattr(row, "keys") else {
            "id": row[0],
            "file_path": row[1],
            "artist": row[2],
            "album": row[3],
            "title": row[4],
        }

        deleted_file = False
        file_path = data.get("file_path")
        if delete_file and file_path:
            normalized = str(file_path).replace("\\", "/")
            if not normalized.startswith("__queued_for_download__") and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    deleted_file = True
                except Exception as exc:
                    logger.warning("[CORRECTIONS] File delete failed: %s", exc)

        delete_track_row(conn, track_id)
        conn.commit()

    finally:
        conn.close()

    return {"success": True, "deleted_track_id": track_id, "deleted_file": deleted_file}, 200
   

def merge_albums(artist: str, source_albums: list[str], canonical_name: str):
    if not source_albums:
        return {"success": False, "error": "source_albums is required"}, 400
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        rows_updated = merge_album_names(cursor, artist, source_albums, canonical_name)
        conn.commit()
    finally:
        conn.close()
    return {"success": True, "rows_updated": rows_updated}, 200


def clear_disc_number(artist: str, album: str, force: bool = False):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        disc_count = count_album_disc_numbers(cursor, artist, album)
        if disc_count > 1 and not force:
            return {"success": False, "error": "Likely multi-disc album", "needs_manual_review": True}, 409
        cleared = clear_album_disc_numbers(conn, artist, album)
        conn.commit()
    finally:
        conn.close()
    return {"success": True, "cleared": cleared}, 200


def artist_exists(artist: str):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        count = artist_track_count(cursor, artist)
    finally:
        conn.close()
    return {"exists": count > 0}, 200


def get_cached_missing(artist: str):
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        rows = fetch_cached_missing_releases(conn, artist)
    finally:
        conn.close()
    return {
        "artist": artist,
        "missing": [{"title": r[0] if not hasattr(r, "get") else r.get("title"), "id": r[1] if not hasattr(r, "get") else r.get("release_id")} for r in rows],
    }, 200


def apply_album_mbid(payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Apply album MBID to all tracks for an artist/album and sync file tags."""
    artist_name = str(payload.get("artist") or "").strip()
    album_name = str(payload.get("album") or "").strip()
    album_mbid = str(payload.get("mbid") or "").strip()
    release_group_mbid = str(payload.get("release_group_mbid") or "").strip()

    if not artist_name or not album_name or not album_mbid:
        return {"success": False, "error": "artist, album, and mbid are required"}, 400

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        rows_updated = update_album_mbid_fields(conn, artist_name, album_name, album_mbid, release_group_mbid, None)
        conn.commit()
    finally:
        conn.close()

    # File tag updates (best-effort, outside transaction)
    files_updated = 0
    if rows_updated:
        try:
            from services.metadata.tag_file_service import update_file_tags as update_tags
            import os
            conn2 = get_db_connection()
            try:
                cursor2 = conn2.cursor()
                cursor2.execute("""
                    SELECT file_path FROM tracks
                    WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s AND album = %s
                """, (artist_name, album_name))
                for row in cursor2.fetchall() or []:
                    fp = str(row[0] or "").strip()
                    if fp and os.path.exists(fp):
                        try:
                            tags = {"musicbrainz_album_mbid": album_mbid}
                            if release_group_mbid:
                                tags["musicbrainz_releasegroupid"] = release_group_mbid
                            if update_tags(fp, tags):
                                files_updated += 1
                        except Exception:
                            pass
            finally:
                conn2.close()
        except Exception:
            pass

    return {
        "success": True,
        "rows_updated": rows_updated or 0,
        "files_updated": files_updated,
        "message": f"Applied album MBID to {rows_updated or 0} track(s)",
    }, 200


def get_correction_albums(artist_name: str) -> tuple[dict[str, Any], int]:
    """Return per-album correction data for an artist (for corrections UI).

    Each album includes:
      - disc_issues: True when tracks have missing/inconsistent disc numbers
      - mbid_issues: True when tracks are missing MusicBrainz album MBIDs
      - missing_tracks: True when tracks are missing file paths
      - track_count: number of tracks in the album
      - has_mbid: whether any track has an album MBID
    """
    artist_name = str(artist_name or "").strip()
    if not artist_name:
        return {"success": False, "error": "artist required", "albums": []}, 400

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT
                album,
                COUNT(*) AS track_count,
                COUNT(*) FILTER (WHERE disc_number IS NULL OR disc_number = '') AS disc_issue_count,
                COUNT(*) FILTER (WHERE mbid IS NULL OR mbid = '') AS mbid_issue_count,
                COUNT(*) FILTER (WHERE file_path IS NULL OR file_path = '') AS missing_track_count,
                MAX(CASE WHEN musicbrainz_album_mbid IS NOT NULL AND musicbrainz_album_mbid != '' THEN 1 ELSE 0 END) AS has_mbid
            FROM tracks
            -- Match both exact canonical and feat-stripped variants so the
            -- correction UI works regardless of which spelling is passed.
            WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(%s)
               OR LOWER(REGEXP_REPLACE(
                      COALESCE(NULLIF(album_artist, ''), artist),
                      '(\s+[\[\(]?\s*(feat\.?|ft\.?|featuring|with|w\/|&|and)\s+.*?[\]\)]?$)',
                      '',
                      'i'
                  )) = LOWER(%s)
            GROUP BY album
            ORDER BY album
        """, (artist_name, artist_name))
        rows = cursor.fetchall()
        albums = []
        for r in rows:
            album = r[0]
            track_count = int(r[1] or 0)
            disc_issue_count = int(r[2] or 0)
            mbid_issue_count = int(r[3] or 0)
            missing_track_count = int(r[4] or 0)
            has_mbid = bool(r[5])
            albums.append({
                "album": album,
                "track_count": track_count,
                "disc_issues": disc_issue_count > 0,
                "disc_issue_count": disc_issue_count,
                "mbid_issues": mbid_issue_count > 0,
                "mbid_issue_count": mbid_issue_count,
                "missing_tracks": missing_track_count > 0,
                "missing_track_count": missing_track_count,
                "has_mbid": has_mbid,
            })
    except Exception:
        albums = []
    finally:
        conn.close()

    return {"success": True, "albums": albums}, 200
