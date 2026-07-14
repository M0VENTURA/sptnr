"""Mood scan pipeline: MBID-first AcousticBrainz mood enrichment for tracks."""

import json
import logging
import os
from typing import Any, Dict, List

from api_clients.acousticbrainz import AcousticBrainzClient
from helpers.db_utils import get_db_connection
from helpers.tag_manager import sync_track_tags_to_file

logger = logging.getLogger(__name__)


def _row_get(row: Any, key: str, index: int = 0, default=None):
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    if hasattr(row, "keys"):
        try:
            return row[key]
        except Exception:
            pass
    try:
        return row[index]
    except Exception:
        return default


def _stop_requested(progress_file: str) -> bool:
    if not progress_file or not os.path.exists(progress_file):
        return False
    try:
        with open(progress_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        return bool(data.get("stop_requested")) or (
            data.get("status") == "stopped" and not bool(data.get("is_running", False))
        )
    except Exception:
        return False


def _write_progress(progress_file: str, payload: Dict[str, Any]) -> None:
    if not progress_file:
        return
    try:
        os.makedirs(os.path.dirname(progress_file), exist_ok=True)
        with open(progress_file, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except Exception as exc:
        logger.debug(f"Failed writing mood scan progress: {exc}")


def run_mood_scan(force: bool = False, progress_file: str = "") -> Dict[str, Any]:
    """Scan tracks with MBIDs and store best mood labels in DB and file tags."""
    conn = get_db_connection()
    cursor = conn.cursor()
    placeholder = "%s"

    conditions: List[str] = [
        "mbid IS NOT NULL",
        "mbid != ''",
        f"COALESCE(file_path, '') NOT LIKE {placeholder}",
        f"CAST(id AS TEXT) NOT LIKE {placeholder}",
    ]
    params: List[Any] = ["__queued_for_download__%", "queue_%"]

    if not force:
        conditions.append("(mood IS NULL OR mood = '')")

    where_sql = " AND ".join(conditions)
    cursor.execute(
        f"""
        SELECT id, title, album, artist, album_artist, mbid, mood
        FROM tracks
        WHERE {where_sql}
        ORDER BY COALESCE(NULLIF(album_artist, ''), artist), album, track_number, title
        """,
        tuple(params),
    )
    rows = cursor.fetchall() or []

    artists = []
    seen = set()
    for row in rows:
        artist_key = (_row_get(row, "album_artist", 4) or _row_get(row, "artist", 3) or "Unknown").strip()
        if artist_key not in seen:
            seen.add(artist_key)
            artists.append(artist_key)

    total_artists = len(artists)
    processed_artists = 0
    scanned_tracks = 0
    updated_tracks = 0
    synced_files = 0

    client = AcousticBrainzClient()
    current_artist = None

    for row in rows:
        if _stop_requested(progress_file):
            conn.commit()
            conn.close()
            _write_progress(progress_file, {
                "is_running": False,
                "scan_type": "mood_scan",
                "status": "stopped",
                "processed_artists": processed_artists,
                "total_artists": total_artists,
                "scanned_tracks": scanned_tracks,
                "updated_tracks": updated_tracks,
                "synced_files": synced_files,
                "current_artist": current_artist,
            })
            return {
                "stopped": True,
                "processed_artists": processed_artists,
                "total_artists": total_artists,
                "scanned_tracks": scanned_tracks,
                "updated_tracks": updated_tracks,
                "synced_files": synced_files,
            }

        track_id = _row_get(row, "id", 0)
        mbid = _row_get(row, "mbid", 5)
        artist_key = (_row_get(row, "album_artist", 4) or _row_get(row, "artist", 3) or "Unknown").strip()

        if artist_key != current_artist:
            current_artist = artist_key
            processed_artists = min(processed_artists + 1, total_artists)

        scanned_tracks += 1

        mood_data = client.get_primary_mood(mbid)
        if not mood_data:
            _write_progress(progress_file, {
                "is_running": True,
                "scan_type": "mood_scan",
                "status": "running",
                "processed_artists": processed_artists,
                "total_artists": total_artists,
                "scanned_tracks": scanned_tracks,
                "updated_tracks": updated_tracks,
                "synced_files": synced_files,
                "current_artist": current_artist,
            })
            continue

        mood = mood_data.get("mood")
        confidence = mood_data.get("confidence")
        if not mood:
            continue

        cursor.execute(
            f"""
            UPDATE tracks
            SET mood = {placeholder},
                mood_confidence = {placeholder},
                mood_source = {placeholder},
                mood_last_updated = CURRENT_TIMESTAMP
            WHERE id = {placeholder}
            """,
            (mood, confidence, "acousticbrainz", track_id),
        )

        if cursor.rowcount and cursor.rowcount > 0:
            updated_tracks += 1
            # Commit before file sync so the sync query sees the latest mood fields.
            conn.commit()
            if sync_track_tags_to_file(track_id):
                synced_files += 1

        _write_progress(progress_file, {
            "is_running": True,
            "scan_type": "mood_scan",
            "status": "running",
            "processed_artists": processed_artists,
            "total_artists": total_artists,
            "scanned_tracks": scanned_tracks,
            "updated_tracks": updated_tracks,
            "synced_files": synced_files,
            "current_artist": current_artist,
        })

    conn.commit()
    conn.close()

    return {
        "stopped": False,
        "processed_artists": processed_artists,
        "total_artists": total_artists,
        "scanned_tracks": scanned_tracks,
        "updated_tracks": updated_tracks,
        "synced_files": synced_files,
    }
