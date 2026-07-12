"""Download folder monitoring service.

Tracks physical download folders on disk and their metadata:
- Resolves folders to album/artist information.
- Checks folder groupings for MusicBrainz matches.
- Provides download folder contents for queue status display.

Uses ``services.infrastructure.filesystem_service`` for all disk I/O.
"""

from __future__ import annotations

import logging
from pathlib import Path
from datetime import datetime

from sqlalchemy import text
from db.engine import db_session
from services.infrastructure.filesystem_service import _get_files_in_folder, get_folder_group_details
from services.metadata.release_service import get_active_releases_with_progress

logger = logging.getLogger(__name__)


# =============================================================================
# FILE HELPERS
# =============================================================================

SUPPORTED_AUDIO_FORMATS = {
    ".mp3", ".flac", ".m4a", ".ogg", ".wav", ".aac", ".wma"
}


def get_folder_groups_with_musicbrainz():
    """
    Combined folder groups including MusicBrainz releases.
    """

    try:
        releases = get_active_releases_with_progress()
        folder_groups = []

        for release in releases:
            folder = release.get("monitoring_folder_path") or release.get("monitoring_folder")
            if not folder:
                continue

            folder_groups.append({
                "type": "musicbrainz",
                "name": folder,
                "display_name": (
                    f"{release.get('release_title') or release.get('title') or 'Unknown'} "
                    f"({release.get('artist') or 'Unknown'} - {release.get('release_year') or 'Unknown'})"
                ),
                "release_id": release.get("release_id"),
                "total_tracks": release.get("total_tracks", 0),
                "discovered_count": release.get("discovered_count", 0),
                "organized_count": release.get("organized_count", 0),
                "finalized_count": release.get("finalized_count", 0),
                "progress_percent": release.get("progress_percent", 0),
                "status": release.get("status", "active"),
                "files": _get_files_in_folder(folder),
                "metadata": {
                    "artist": release.get("artist"),
                    "album": release.get("release_title") or release.get("title"),
                    "year": release.get("release_year"),
                    "source": "musicbrainz",
                }
            })

        return {
            "success": True,
            "count": len(folder_groups),
            "folder_groups": folder_groups
        }

    except Exception as e:
        logger.error("[FOLDER_GROUPS] Error: %s", e, exc_info=True)
        return {"success": False, "error": str(e), "folder_groups": []}


def get_folder_groups():
    return get_folder_groups_with_musicbrainz()


def get_folder_details(folder_path: str):
    return get_folder_group_details(folder_path)


def cancel_folder(folder_path: str):
    return cancel_folder_downloads(folder_path)


# -----------------------------------------------------------------------------



# -----------------------------------------------------------------------------

def retry_matching_for_release(release_id: str):
    """
    Returns unmatched tracks (future matching hook).
    """

    try:
        with db_session() as session:

            result = session.execute(text("""
                SELECT id, monitoring_folder_path, total_tracks, discovered_count
                FROM musicbrainz_releases
                WHERE release_id = :release_id
            """), {"release_id": release_id})

            row = result.fetchone()

            if not row:
                return {"success": False, "error": "Release not found"}

            release_db_id = row[0]
            folder = row[1]
            total_tracks = row[2]
            discovered = row[3]

            result = session.execute(text("""
                SELECT track_number, track_title, track_artist
                FROM musicbrainz_release_tracks
                WHERE release_id = :release_id
                  AND status NOT IN ('discovered', 'finalized')
            """), {"release_id": release_id})

            unmatched = result.fetchall()

        return {
            "success": True,
            "release_id": release_id,
            "folder": folder,
            "total_tracks": total_tracks,
            "discovered_count": discovered,
            "unmatched_tracks": [
                {
                    "track_number": r[0],
                    "title": r[1],
                    "artist": r[2]
                }
                for r in unmatched
            ],
        }

    except Exception as e:
        logger.error("[RETRY_MATCH] Error: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}


# -----------------------------------------------------------------------------

def cancel_folder_downloads(folder_path: str):
    """
    Cancel downloads associated with a folder.
    """

    try:
        with db_session() as session:

            result = session.execute(text("""
                SELECT id, release_id
                FROM musicbrainz_releases
                WHERE monitoring_folder_path = :folder
            """), {"folder": folder_path})

            row = result.fetchone()

            if not row:
                return {"success": False, "error": "Folder not recognized"}

            release_db_id = row[0]
            release_id = row[1]

            session.execute(text("""
                DELETE FROM download_queue
                WHERE mb_release_download_id = :id
            """), {"id": release_db_id})

            session.execute(text("""
                UPDATE musicbrainz_releases
                SET status = 'cancelled',
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = :id
            """), {"id": release_db_id})

        return {
            "success": True,
            "release_id": release_id,
            "folder": folder_path,
            "message": "Cancelled release downloads",
        }

    except Exception as e:
        logger.error("[CANCEL_FOLDER] Error: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}


def check_folder_duplicates(folder_path: str, data: dict) -> dict:
    """Check a folder for duplicate queue items."""
    try:
        from db.repositories.queue import get_queue_items_by_folder
        items = get_queue_items_by_folder(folder_path)
        return {"success": True, "duplicates": items or [], "count": len(items or [])}
    except Exception as e:
        logger.error("[check_folder_duplicates] Error: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}


def process_album_existing(data: dict) -> dict:
    """Process an existing album match from queue data."""
    try:
        queue_id = (data or {}).get("queue_id")
        if not queue_id:
            return {"success": False, "error": "queue_id required"}
        from services.downloads.match_orchestrator import apply_mbid_match_batch
        return apply_mbid_match_batch(queue_ids=[int(queue_id)], new_mbid="", expand_tracks=False)
    except Exception as e:
        logger.error("[process_album_existing] Error: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}