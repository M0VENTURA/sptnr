"""Audio quality scoring + duplicate-file pruning for the download queue.

When a discovered orphan file matches an active queue item (same artist +
title), the inferior copy is removed from disk so the queue never piles up
multiple versions of the same track (e.g. several Spice Girls .flac rips).
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# Lossless container extensions get a huge base score so a 128kbps FLAC
# always beats a 320kbps MP3.
LOSSLESS_EXTS = {".flac", ".wav", ".alac", ".aiff", ".ape", ".wv"}

# Queue statuses that count as "active" for duplicate comparison.
ACTIVE_STATUSES = (
    "queued", "searching", "downloading", "processing",
    "completed", "unmatched", "possible_duplicate", "imported", "in_collection",
)


def calculate_audio_quality_score(file_path: str) -> int:
    """Score a file by container format, bitrate and sample rate.

    FLAC/WAV/ALAC = 100 000+ pts; MP3 320kbps ≈ 320 pts; MP3 128kbps ≈ 128 pts.
    """
    if not file_path or not os.path.isfile(file_path):
        return 0

    ext = os.path.splitext(file_path)[1].lower()
    score = 100000 if ext in LOSSLESS_EXTS else 0
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
    """Return the oldest active queue row for the same (artist, title) that
    already has a file on disk, or ``None``."""
    from sqlalchemy import text
    from db.engine import db_session

    # Inline the statuses — psycopg2 cannot adapt a Python list for
    # ``ANY(:statuses)`` (see get_completed_queue in db/repositories/queue.py).
    status_sql = ", ".join(f"'{s}'" for s in ACTIVE_STATUSES)
    with db_session() as session:
        row = session.execute(
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
        ).fetchone()
        return dict(row._mapping) if row else None


def prune_inferior_duplicate(meta: dict[str, Any], new_file_path: str) -> dict[str, Any]:
    """Compare ``new_file_path`` against the active queue file for the same
    track and delete the inferior copy from disk.

    - New file better   → replace the stored path, delete the old file.
    - New file equal or worse → delete the new file (first copy wins).
    """
    result: dict[str, Any] = {"action": "none"}
    artist = str(meta.get("artist") or "").strip()
    title = str(meta.get("title") or "").strip()
    if not artist or not title:
        return result

    try:
        from db.repositories.queue import update_queue_item

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
            "[DEDUP] Quality — existing '%s' = %s pts vs new '%s' = %s pts",
            existing_path, existing_score, new_file_path, new_score,
        )

        if new_score > existing_score:
            os.remove(existing_path)
            update_queue_item(
                int(existing["id"]),
                file_path=new_file_path,
                found_filename=os.path.basename(new_file_path),
            )
            result = {"action": "replaced", "removed_path": existing_path}
            logger.info("[DEDUP] New file is better — replaced %s", existing_path)
        else:
            os.remove(new_file_path)
            result = {"action": "removed_new", "removed_path": new_file_path}
            logger.info("[DEDUP] Existing file is equal or better — removed %s", new_file_path)
    except Exception as exc:
        logger.warning("[DEDUP] prune_inferior_duplicate failed: %s", exc)
    return result
