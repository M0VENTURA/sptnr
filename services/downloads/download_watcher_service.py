"""
Downloads folder watcher service.

Monitors the configured downloads directory for new audio files,
extracts metadata, matches them against queued downloads, and
auto-queues unmatched files for MusicBrainz enrichment.

Designed to be called periodically by APScheduler rather than
running a continuous loop.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from services.downloads.download_scan_service import (
    resolve_downloads_dir,
    resolve_torrents_dir,
    discover_audio_files,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Scan downloads folder
# ---------------------------------------------------------------------------

def scan_downloads_folder() -> list[dict[str, Any]]:
    """Scan the torrent downloads folder for audio files.

    Returns a list of result dicts describing what happened with each file:
        ``{"status": "success"|"skipped"|"error",
            "filename": str,
            "artist": str, "album": str, "title": str, (if success)
            "target_path": str, (if success)
            "error": str (if error)}``
    """
    from helpers.metadata_reader import read_mp3_metadata
    from db.repositories.queue import get_queue_status_counts

    downloads_dir = resolve_downloads_dir()
    torrents_dir = resolve_torrents_dir()

    if not os.path.isdir(torrents_dir):
        logger.info("Torrents folder not found: %s — skipping scan", torrents_dir)
        return []

    results: list[dict[str, Any]] = []
    audio_files = discover_audio_files()

    for file_info in audio_files:
        file_path = file_info.full_path
        if not os.path.isfile(file_path):
            continue

        try:
            metadata = read_mp3_metadata(file_path) or {}
            artist = (metadata.get("artist") or "").strip()
            title = (metadata.get("title") or "").strip()
            album = (metadata.get("album") or "").strip()

            if not artist or not title:
                logger.debug("Skipping %s: no artist/title in metadata", file_info.filename)
                results.append({"status": "skipped", "filename": file_info.filename})
                continue

            # Try to match against an active queue item.
            queue_item = _find_queue_match(artist, album, title)
            if queue_item:
                match_result = _handle_queue_match(file_path, metadata, queue_item)
                results.append(match_result)
            else:
                # No matching queue item — queue as unmatched for enrichment.
                _queue_unmatched(file_path, metadata)
                results.append({
                    "status": "queued",
                    "filename": file_info.filename,
                    "artist": artist,
                    "album": album,
                    "title": title,
                })

        except Exception as exc:
            logger.error("Error processing %s: %s", file_info.filename, exc)
            results.append({"status": "error", "filename": file_info.filename, "error": str(exc)})

    summary = {"success": 0, "queued": 0, "skipped": 0, "error": 0}
    for r in results:
        key = r.get("status")
        if key in summary:
            summary[key] += 1
    logger.info(
        "Downloads scan complete: %s (success=%s queued=%s skipped=%s error=%s)",
        downloads_dir,
        summary["success"],
        summary["queued"],
        summary["skipped"],
        summary["error"],
    )
    return results


def _find_queue_match(
    artist: str,
    album: str,
    title: str,
) -> dict[str, Any] | None:
    """Find an active queue item matching artist/album/title."""
    try:
        from sqlalchemy import text
        from db.engine import db_session

        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT *
                    FROM download_queue
                    WHERE LOWER(COALESCE(artist, '')) = LOWER(:artist)
                      AND LOWER(COALESCE(album, '')) = LOWER(:album)
                      AND LOWER(COALESCE(title, '')) = LOWER(:title)
                      AND status IN ('queued', 'searching', 'downloading')
                    ORDER BY updated_at DESC
                    LIMIT 1
                """),
                {"artist": artist, "album": album, "title": title},
            )
            row = result.fetchone()
            return dict(row._mapping) if row else None
    except Exception as exc:
        logger.debug("Error finding queue match: %s", exc)
        return None


def _handle_queue_match(
    file_path: str,
    metadata: dict,
    queue_item: dict,
) -> dict[str, Any]:
    """Process a file that matches an active queue item."""
    from difflib import SequenceMatcher

    def _similarity(a: str, b: str) -> float:
        return SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()

    file_artist = (metadata.get("artist") or "").strip()
    file_title = (metadata.get("title") or "").strip()
    queue_artist = (queue_item.get("artist") or "").strip()
    queue_title = (queue_item.get("title") or "").strip()

    title_score = _similarity(file_title, queue_title)
    if title_score < 0.70:
        return {"status": "skipped", "filename": os.path.basename(file_path),
                "error": f"Title mismatch (score={title_score:.2f})"}

    if queue_artist.lower() not in file_artist.lower():
        artist_score = _similarity(file_artist, queue_artist)
        if artist_score < 0.55:
            return {"status": "skipped", "filename": os.path.basename(file_path),
                    "error": f"Artist mismatch (score={artist_score:.2f})"}

    # Match found — copy to music dir and update queue.
    from services.downloads.download_verification_service import transfer_and_verify
    from db.repositories.queue import update_queue_item

    dest_dir = os.environ.get("MUSIC_ROOT", "/music")
    dest_path = os.path.join(dest_dir, queue_artist or "Unknown",
                             queue_item.get("album") or "Unknown",
                             os.path.basename(file_path))

    verify_result = transfer_and_verify(file_path, dest_path, queue_item.get("id"))
    if verify_result.get("success"):
        update_queue_item(queue_item["id"], status="completed", found_filename=os.path.basename(file_path))
        try:
            os.remove(file_path)
        except Exception:
            pass
        return {"status": "success", "filename": os.path.basename(file_path),
                "artist": queue_artist, "album": queue_item.get("album"), "title": queue_title,
                "target_path": dest_path}
    else:
        return {"status": "error", "filename": os.path.basename(file_path),
                "error": verify_result.get("error", "Move failed")}


def _queue_unmatched(file_path: str, metadata: dict) -> None:
    """Add an unmatched file to the queue for MusicBrainz enrichment."""
    from db.repositories.queue import insert_queue_item

    try:
        insert_queue_item(
            artist=(metadata.get("artist") or "").strip(),
            title=(metadata.get("title") or "").strip(),
            album=(metadata.get("album") or "").strip(),
            source="local",
            status="unmatched",
        )
    except Exception as exc:
        logger.debug("Failed to queue unmatched file %s: %s", file_path, exc)
