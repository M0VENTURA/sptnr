"""Release Finalization Service.

Finalizes MusicBrainz releases when all tracks are discovered:
1. Finds releases where ``discovered_count >= total_tracks``
2. Creates final directory structure in the music library
3. Moves and renames files with track numbers
4. Updates DB status from ``active`` → ``finalized``
5. Cleans up empty monitoring folders
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Dict, List

import structlog
from sqlalchemy import text

from db.engine import db_session

logger = structlog.get_logger(__name__)


def get_ready_releases() -> list[dict[str, Any]]:
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
            
        releases: list[dict[str, Any]] = []
        for row in rows:
            mapping = getattr(row, "_mapping", None)
            releases.append({
                "id": mapping.get("id") if mapping else row[0],
                "release_id": mapping.get("release_id") if mapping else row[1],
                "release_title": mapping.get("release_title") if mapping else row[2],
                "artist": mapping.get("artist") if mapping else row[3],
                "release_year": mapping.get("release_year") if mapping else row[4],
                "monitoring_folder_path": mapping.get("monitoring_folder_path") if mapping else row[5],
                "total_tracks": mapping.get("total_tracks") if mapping else row[6],
                "discovered_count": mapping.get("discovered_count") if mapping else row[7],
                "created_at": mapping.get("created_at") if mapping else row[8],
            })
        return releases
    except Exception as exc:
        logger.error("Failed to find ready releases", error=str(exc))
        return []


def finalize_release(release: dict[str, Any]) -> bool:
    """Finalize a single release: move files, update DB, clean up."""
    try:
        release_db_id = release["id"]
        release_id = release["release_id"]
        artist = release["artist"]
        title = release["release_title"]
        year = release.get("release_year") or ""
        monitoring_path = Path(str(release["monitoring_folder_path"]))

        if not monitoring_path.exists():
            logger.error("Monitoring folder not found", path=str(monitoring_path), release_id=release_id)
            return False

        music_root = (
            os.environ.get("MUSIC_FOLDER")
            or os.environ.get("MUSIC_ROOT")
            or "/music"
        )
        year_tag = f"{year} - " if year else ""
        dest_dir = Path(music_root) / artist / f"{year_tag}{title}"
        dest_dir.mkdir(parents=True, exist_ok=True)

        audio_extensions = {".mp3", ".flac", ".m4a", ".ogg", ".wav", ".aac", ".wma"}
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT track_number, track_title, file_path
                    FROM musicbrainz_release_tracks
                    WHERE release_id = :release_id AND status = 'completed'
                    ORDER BY disc_number, track_number
                """),
                {"release_id": release_id},
            )
            tracks = [dict(r._mapping) for r in result.fetchall() or []]

        for idx, row in enumerate(tracks):
            track_num = row.get("track_number")
            track_title = row.get("track_title")
            src_path_str = row.get("file_path")

            if not src_path_str:
                continue

            src = Path(str(src_path_str))
            if not src.exists():
                continue

            ext = src.suffix.lower()
            if ext not in audio_extensions:
                continue

            num_str = f"{int(track_num):02d}" if track_num else f"{idx + 1:02d}"
            new_name = f"{num_str}. {artist} - {track_title}{ext}"
            new_name = "".join(c for c in new_name if c.isprintable() and c not in '<>:"/\\|?*')
            dest_path = dest_dir / new_name

            shutil.move(str(src), str(dest_path))
            logger.debug("Moved release track", source=str(src), target=str(dest_path))

            with db_session() as session:
                session.execute(
                    text("""
                        UPDATE musicbrainz_release_tracks 
                        SET file_path = :fp 
                        WHERE release_id = :release_id AND track_number = :track_number
                    """),
                    {"fp": str(dest_path), "release_id": release_id, "track_number": track_num},
                )

        with db_session() as session:
            session.execute(
                text("""
                    UPDATE musicbrainz_releases
                    SET status = 'finalized',
                        final_folder_path = :path,
                        finalized_at = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = :id
                """),
                {"path": str(dest_dir), "id": release_db_id},
            )

        _cleanup_folder(monitoring_path)

        logger.info(
            "Successfully finalized release",
            title=title,
            artist=artist,
            destination=str(dest_dir),
            track_count=len(tracks),
        )
        return True

    except Exception as exc:
        logger.error("Failed to finalize release", release_id=release.get("release_id"), error=str(exc))
        return False


def _cleanup_folder(folder: Path) -> None:
    """Remove the monitoring folder if it's empty."""
    try:
        if folder.exists() and not any(folder.iterdir()):
            shutil.rmtree(folder, ignore_errors=True)
            logger.debug("Removed empty monitoring folder", path=str(folder))
    except Exception as exc:
        logger.debug("Could not clean up monitoring folder", path=str(folder), error=str(exc))


def run_finalization_check() -> dict[str, Any]:
    """Find and finalize all ready releases."""
    ready = get_ready_releases()
    if not ready:
        return {"finalized": 0, "checked": 0}

    finalized = 0
    for release in ready:
        if finalize_release(release):
            finalized += 1

    logger.info("Release finalization check complete", finalized=finalized, checked=len(ready))
    return {"finalized": finalized, "checked": len(ready)}
