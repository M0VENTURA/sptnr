"""Navidrome-specific database write helpers."""

from __future__ import annotations

from typing import Any

from db.utils import get_execute_values


def bulk_upsert_navidrome_tracks(conn: Any, tracks: list[dict[str, Any]]) -> None:
    """Bulk insert/update Navidrome tracks using an existing connection."""
    if not tracks:
        return
    execute_values = get_execute_values()
    cursor = conn.cursor()
    try:
        values = [
            (
                track.get("id"),
                track.get("title", ""),
                track.get("artist", ""),
                track.get("album", ""),
                track.get("duration", 0),
            )
            for track in tracks
        ]
        execute_values(
            cursor,
            """
            INSERT INTO tracks (id, title, artist, album, duration)
            VALUES %s
            ON CONFLICT (id) DO UPDATE SET
                title = EXCLUDED.title,
                artist = EXCLUDED.artist,
                album = EXCLUDED.album,
                duration = EXCLUDED.duration
            """,
            values,
            page_size=1000,
        )
        conn.commit()
    finally:
        try:
            cursor.close()
        except Exception:
            pass

# MusicBrainz freshness threshold: re-check recording metadata if older than this.
_MB_FRESHNESS_THRESHOLD_SECONDS = 86400  # 24 hours

# Seconds to wait after triggering a Navidrome scan before querying it.
_NAVIDROME_INDEXING_WAIT_SECONDS = 5

# Window (minutes) within which completed import groups are verified.
_IMPORT_GROUP_COMPLETION_WINDOW_MINUTES = 30