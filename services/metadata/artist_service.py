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
    row = fetch_track_for_delete(None, track_id)
    if not row:
        return {"success": False, "error": "Track not found"}, 404

    data = {
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

    delete_track_row(None, track_id)

    return {"success": True, "deleted_track_id": track_id, "deleted_file": deleted_file}, 200


def merge_albums(artist: str, source_albums: list[str], canonical_name: str):
    if not source_albums:
        return {"success": False, "error": "source_albums is required"}, 400
    rows_updated = merge_album_names(None, artist, source_albums, canonical_name)
    return {"success": True, "rows_updated": rows_updated}, 200


def clear_disc_number(artist: str, album: str, force: bool = False):
    disc_count = count_album_disc_numbers(None, artist, album)
    if disc_count > 1 and not force:
        return {"success": False, "error": "Likely multi-disc album", "needs_manual_review": True}, 409
    cleared = clear_album_disc_numbers(None, artist, album)
    return {"success": True, "cleared": cleared}, 200


def artist_exists(artist: str):
    count = artist_track_count(None, artist)
    return {"exists": count > 0}, 200


def get_cached_missing(artist: str):
    rows = fetch_cached_missing_releases(None, artist)
    return {
        "artist": artist,
        "missing": [{"title": r[0], "id": r[1]} for r in rows],
    }, 200


def apply_album_mbid(payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """Apply album MBID to all tracks for an artist/album and sync file tags."""
    artist_name = str(payload.get("artist") or "").strip()
    album_name = str(payload.get("album") or "").strip()
    album_mbid = str(payload.get("mbid") or "").strip()
    release_group_mbid = str(payload.get("release_group_mbid") or "").strip()

    if not artist_name or not album_name or not album_mbid:
        return {"success": False, "error": "artist, album, and mbid are required"}, 400

    rows_updated = update_album_mbid_fields(None, artist_name, album_name, album_mbid, release_group_mbid, None)

    # File tag updates (best-effort, outside transaction)
    files_updated = 0
    if rows_updated:
        try:
            from services.metadata.tag_file_service import update_file_tags as update_tags
            import os
            with db_session() as session:
                result = session.execute(text("""
                    SELECT file_path FROM tracks
                    WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist AND album = :album
                """), {"artist": artist_name, "album": album_name})
                for row in result.fetchall() or []:
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
      - album_type: detected album type (album, ep, single, compilation, live_album, remix)
      - album_year: release year from track data
      - is_missing: whether all tracks have no file_path
    """
    artist_name = str(artist_name or "").strip()
    if not artist_name:
        return {"success": False, "error": "artist required", "albums": []}, 400

    try:
        with db_session() as session:
            rows = session.execute(text("""
                SELECT
                    album,
                    COUNT(*) AS track_count,
                    COUNT(*) FILTER (WHERE disc_number IS NULL OR disc_number = '') AS disc_issue_count,
                    COUNT(*) FILTER (WHERE mbid IS NULL OR mbid = '') AS mbid_issue_count,
                    COUNT(*) FILTER (WHERE file_path IS NULL OR file_path = '') AS missing_track_count,
                    COUNT(*) FILTER (WHERE file_path IS NOT NULL AND file_path != '') AS present_track_count,
                    MAX(CASE WHEN musicbrainz_album_mbid IS NOT NULL AND musicbrainz_album_mbid != '' THEN 1 ELSE 0 END) AS has_mbid,
                    MAX(year) AS album_year,
                    MAX(spotify_album_type) AS spotify_album_type,
                    MAX(album_type) AS album_type
                FROM tracks
                WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:name)
                   OR LOWER(REGEXP_REPLACE(
                          COALESCE(NULLIF(album_artist, ''), artist),
                          '(\s+[\[\(]?\s*(feat\.?|ft\.?|featuring|with|w\/|&|and)\s+.*?[\]\)]?$)',
                          '',
                          'i'
                      )) = LOWER(:name)
                GROUP BY album
                ORDER BY album
            """), {"name": artist_name})

            # Helper to classify album type (mirrors classify_album from ui_routes)
            def _classify(album_row: dict) -> str:
                import re as _re
                raw_type = str(album_row.get("spotify_album_type") or album_row.get("album_type") or "").lower()
                album_name = str(album_row.get("album") or "").lower()
                if "compilation" in raw_type:
                    return "compilation"
                if "soundtrack" in raw_type or "soundtrack" in album_name:
                    return "compilation"
                if "live" in raw_type or _re.search(r'\blive\b', album_name) or "unplugged" in album_name:
                    return "live_album"
                if "remix" in raw_type or "remix" in album_name:
                    return "remix_album"
                if raw_type == "ep" or raw_type.startswith("ep") or " ep" in raw_type:
                    return "ep"
                if "single" in raw_type:
                    return "single"
                return "album"

            albums = []
            for r in rows.fetchall():
                row_dict = dict(r._mapping)
                present_count = int(row_dict.get("present_track_count") or 0)
                total_count = int(row_dict["track_count"] or 0)
                albums.append({
                    "album": row_dict["album"],
                    "track_count": total_count,
                    "disc_issues": int(row_dict["disc_issue_count"] or 0) > 0,
                    "disc_issue_count": int(row_dict["disc_issue_count"] or 0),
                    "mbid_issues": int(row_dict["mbid_issue_count"] or 0) > 0,
                    "mbid_issue_count": int(row_dict["mbid_issue_count"] or 0),
                    "missing_tracks": int(row_dict["missing_track_count"] or 0) > 0,
                    "missing_track_count": int(row_dict["missing_track_count"] or 0),
                    "has_mbid": bool(row_dict["has_mbid"]),
                    "album_year": int(row_dict["album_year"]) if row_dict.get("album_year") else None,
                    "is_missing": present_count == 0 and total_count > 0,
                    "album_type": _classify(row_dict),
                    "spotify_album_type": row_dict.get("spotify_album_type") or "",
                })
    except Exception as exc:
        logger.error("[get_correction_albums] Query failed for '%s': %s", artist_name, exc)
        albums = []

    # Sort: missing last, then by year desc, then by name
    albums.sort(key=lambda a: (a.get("is_missing") or False, a.get("album_year") is None, -(a.get("album_year") or 0), str(a.get("album") or "").lower()))

    return {"success": True, "albums": albums}, 200
