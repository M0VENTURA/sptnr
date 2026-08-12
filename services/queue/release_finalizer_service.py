"""Release Finalization Service.

Migrated from ``old_system/musicbrainz_finalizer.py``.

Finalizes MusicBrainz releases when all tracks are discovered:
1. Finds releases where ``discovered_count >= total_tracks``
2. Creates final directory structure in the music library
3. Moves and renames files with track numbers
4. Updates DB status from ``active`` → ``finalized``
5. Cleans up empty monitoring folders
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from db.engine import db_session

logger = logging.getLogger(__name__)


def get_ready_releases() -> List[Dict[str, Any]]:
    """Find all active releases where all tracks have been discovered."""
    try:
        with db_session() as session:
            rows = session.execute(
                text("""
                    SELECT id, release_id, release_title, artist, release_year,
                           monitoring_folder_path, total_tracks, discovered_count,
                           created_at
                    FROM musicbrainz_releases
                    WHERE status = 'active'
                      AND discovered_count >= total_tracks
                      AND monitoring_folder_path IS NOT NULL
                    ORDER BY created_at ASC
                """)
            ).fetchall() or []
        releases: List[Dict[str, Any]] = []
        for row in rows:
            releases.append({
                "id": row[0],
                "release_id": row[1],
                "release_title": row[2],
                "artist": row[3],
                "release_year": row[4],
                "monitoring_folder_path": row[5],
                "total_tracks": row[6],
                "discovered_count": row[7],
                "created_at": row[8],
            })
        return releases
    except Exception as exc:
        logger.error("Failed to find ready releases: %s", exc)
        return []


def finalize_release(release: Dict[str, Any]) -> bool:
    """Finalize a single release: move files, update DB, clean up.

    Args:
        release: Dict from ``get_ready_releases()``.

    Returns:
        True if the release was finalised successfully.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        release_db_id = release["id"]
        release_id = release["release_id"]
        artist = release["artist"]
        title = release["release_title"]
        year = release.get("release_year") or ""
        monitoring_path = Path(str(release["monitoring_folder_path"]))

        if not monitoring_path.exists():
            logger.error("Monitoring folder not found: %s", monitoring_path)
            return False

        # Build final destination: /music/ARTIST/YEAR - ALBUM/
        music_root = (
            os.environ.get("MUSIC_FOLDER")
            or os.environ.get("MUSIC_ROOT")
            or "/music"
        )
        year_tag = f"{year} - " if year else ""
        dest_dir = Path(music_root) / artist / f"{year_tag}{title}"
        dest_dir.mkdir(parents=True, exist_ok=True)

        # Get track files and rename with track numbers
        audio_extensions = {".mp3", ".flac", ".m4a", ".ogg", ".wav", ".aac", ".wma"}
        cursor.execute("""
            SELECT track_number, track_title, file_path
            FROM musicbrainz_release_tracks
            WHERE release_id = %s AND status = 'completed'
            ORDER BY disc_number, track_number
        """, (release_id,))
        tracks = cursor.fetchall() or []

        for idx, row in enumerate(tracks):
            track_num = row[0] if not hasattr(row, "get") else row.get("track_number")
            track_title = row[1] if not hasattr(row, "get") else row.get("track_title")
            src_path_str = row[2] if not hasattr(row, "get") else row.get("file_path")

            if not src_path_str:
                continue

            src = Path(str(src_path_str))
            if not src.exists():
                continue

            ext = src.suffix.lower()
            if ext not in audio_extensions:
                continue

            # Build new filename: "01. Artist - Title.ext"
            num_str = f"{int(track_num):02d}" if track_num else f"{idx + 1:02d}"
            new_name = f"{num_str}. {artist} - {track_title}{ext}"
            # Sanitize filename
            new_name = "".join(c for c in new_name if c.isprintable() and c not in '<>:"/\\|?*')
            dest_path = dest_dir / new_name

            # Move file
            shutil.move(str(src), str(dest_path))
            logger.debug("Moved %s → %s", src, dest_path)

            # Update track record
            cursor.execute(
                "UPDATE musicbrainz_release_tracks SET file_path = %s WHERE release_id = %s AND track_number = %s",
                (str(dest_path), release_id, track_num),
            )

        # Update release status
        cursor.execute("""
            UPDATE musicbrainz_releases
            SET status = 'finalized',
                final_folder_path = %s,
                finalized_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
        """, (str(dest_dir), release_db_id))
        conn.commit()

        # Clean up empty monitoring folder
        _cleanup_folder(monitoring_path)

        logger.info(
            "Finalized release '%s' by '%s' → %s (%d tracks)",
            title, artist, dest_dir, len(tracks),
        )
        return True

    except Exception as exc:
        logger.error("Failed to finalize release %s: %s", release.get("release_id"), exc)
        try:
            conn.rollback()
        except Exception:
            pass
        return False
    finally:
        conn.close()


def _cleanup_folder(folder: Path) -> None:
    """Remove the monitoring folder if it's empty."""
    try:
        if folder.exists() and not any(folder.iterdir()):
            shutil.rmtree(folder, ignore_errors=True)
            logger.debug("Removed empty monitoring folder: %s", folder)
    except Exception as exc:
        logger.debug("Could not clean up folder %s: %s", folder, exc)


def run_finalization_check() -> Dict[str, Any]:
    """Find and finalize all ready releases.

    Called periodically by the queue processor or scheduler.

    Returns:
        Dict with ``finalized`` and ``checked`` counts.
    """
    ready = get_ready_releases()
    if not ready:
        return {"finalized": 0, "checked": 0}

    finalized = 0
    for release in ready:
        if finalize_release(release):
            finalized += 1

    logger.info("Release finalization: %d/%d finalized", finalized, len(ready))
    return {"finalized": finalized, "checked": len(ready)}
