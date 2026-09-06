"""Audio quality scoring + duplicate-file pruning for the download queue.

When a discovered orphan file matches an active queue item (same artist +
title), the inferior copy is removed from disk so the queue never piles up
multiple versions of the same track (e.g. several Spice Girls .flac rips).
"""

from __future__ import annotations

import os
from typing import Any

import structlog
from sqlalchemy import text

from db.engine import db_session
from db.repositories.queue import update_queue_item

# Import unified lossless extensions
from services.downloads.download_quality_config import LOSSLESS_EXTENSIONS

logger = structlog.get_logger(__name__)

# Queue statuses that count as "active" for duplicate comparison.
ACTIVE_STATUSES = (
    "queued", "searching", "downloading", "processing",
    "completed", "unmatched", "possible_duplicate", "imported", "in_collection",
)


def calculate_audio_quality_score(file_path: str) -> int:
    """Score a file by container format, bitrate and sample rate."""
    if not file_path or not os.path.isfile(file_path):
        return 0

    ext = os.path.splitext(file_path)[1].lower()
    score = 100000 if ext in LOSSLESS_EXTENSIONS else 0
    
    try:
        from mutagen import File as MutagenFile

        audio = MutagenFile(file_path)
        if audio and hasattr(audio, "info"):
            info = audio.info
            score += int(getattr(info, "bitrate", 0) or 0) // 1000
            score += int(getattr(info, "sample_rate", 0) or 0) // 100
    except Exception:
        pass
        
    return score


def _find_active_duplicate(artist: str, title: str) -> dict[str, Any] | None:
    """Return the oldest active queue row for the same (artist, title)."""
    status_sql = ", ".join(f"'{s}'" for s in ACTIVE_STATUSES)
    
    try:
        with db_session() as session:
            result = session.execute(
                text(f"""
                    SELECT * FROM download_queue
                    WHERE LOWER(artist) = LOWER(:artist)
                      AND LOWER(title) = LOWER(:title)
                      AND status IN ({status_sql})
                      AND (file_path IS NOT NULL OR music_file_path IS NOT NULL
                           OR matched_file_path IS NOT NULL OR found_filename IS NOT NULL)
                    ORDER BY created_at ASC
                    LIMIT 1
                """),
                {"artist": artist, "title": title},
            )
            row = result.fetchone()
            return dict(row._mapping) if row else None
    except Exception as exc:
        logger.debug("Error finding active duplicate", error=str(exc))
        return None


def prune_inferior_duplicate(meta: dict[str, Any], new_file_path: str) -> dict[str, Any]:
    """Compare ``new_file_path`` against the active queue file for the same
    track and delete the inferior copy from disk."""
    result: dict[str, Any] = {"action": "none"}
    artist = str(meta.get("artist") or "").strip()
    title = str(meta.get("title") or "").strip()
    
    if not artist or not title:
        return result

    try:
        existing = _find_active_duplicate(artist, title)
        if not existing:
            return result

        existing_path = str(
            existing.get("file_path") or existing.get("music_file_path")
            or existing.get("matched_file_path") or existing.get("found_filename") or ""
        )
        if not existing_path or not os.path.isfile(existing_path):
            return result
            
        if os.path.abspath(existing_path) == os.path.abspath(new_file_path):
            return result

        new_score = calculate_audio_quality_score(new_file_path)
        existing_score = calculate_audio_quality_score(existing_path)
        
        logger.info(
            "Quality comparison for duplicate",
            existing_path=existing_path,
            existing_score=existing_score,
            new_path=new_file_path,
            new_score=new_score,
        )

        if new_score > existing_score:
            os.remove(existing_path)
            update_queue_item(
                int(existing["id"]),
                file_path=new_file_path,
                found_filename=os.path.basename(new_file_path),
            )
            result = {"action": "replaced", "removed_path": existing_path}
            logger.info("New file is better — replaced existing copy", removed_path=existing_path)
        else:
            os.remove(new_file_path)
            result = {"action": "removed_new", "removed_path": new_file_path}
            logger.info("Existing file is equal or better — removed new copy", removed_path=new_file_path)
            
    except Exception as exc:
        logger.warning("prune_inferior_duplicate failed", error=str(exc))
        
    return result
