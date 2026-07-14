"""Database and filesystem cleanup helpers for Navidrome imports."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Tuple, Set

from db.repositories.tracks import delete_tracks_by_id

from db.repositories.scan_repository import (
    normalize_existing_artist_rows,
    sanitize_artist_file_paths_and_duplicates,
)

logger = logging.getLogger(__name__)

def normalize_existing_artist_rows_safe(*, artist_name: str, canonical_artist_name: str) -> None:
    """Normalize already-imported artist rows to the canonical artist name."""
    try:
        updated_rows = normalize_existing_artist_rows(canonical_artist_name=canonical_artist_name, aliases=[artist_name])
        if updated_rows:
            logging.info("[ARTIST_NORMALIZE] %s: normalized %s existing rows", canonical_artist_name, updated_rows)
    except Exception as err:
        logging.debug("[ARTIST_NORMALIZE] Skipped for %s: %s", canonical_artist_name, err)


def sanitize_artist_rows_safe(*, canonical_artist_name: str) -> None:
    """Normalize stale file paths and remove duplicate rows for an artist."""
    try:
        summary = sanitize_artist_file_paths_and_duplicates(canonical_artist_name)
        path_updates, duplicates_removed = normalize_sanitize_summary(summary)
        if path_updates or duplicates_removed:
            logging.info(
                "[NAVIDROME_SANITIZE] %s: normalized_paths=%s, duplicates_removed=%s",
                canonical_artist_name,
                path_updates,
                duplicates_removed,
            )
    except Exception as err:
        logging.debug("[NAVIDROME_SANITIZE] Skipped for %s: %s", canonical_artist_name, err)


def cleanup_stale_album_tracks_if_needed(
    *,
    artist_name: str,
    album_name: str,
    cached_ids_for_album: Set[str],
    navidrome_tracks: list[Dict[str, Any]],
) -> None:
    """In diff_mode: remove stale tracks for this album only."""
    if not cached_ids_for_album:
        return
    nav_ids = {track.get("id") for track in navidrome_tracks if track.get("id")}
    stale_ids = cached_ids_for_album - nav_ids
    if not stale_ids:
        return
    removed = delete_tracks_by_id(stale_ids, context=f"album '{album_name}' (diff_mode)")
    if removed:
        logging.info("Removed %s stale track(s) from album '%s' (diff_mode)", removed, album_name)
        logger.info(
            "Navidrome Import - %s - Removed %s stale track(s)",
            artist_name,
            removed,
        )



def cleanup_stale_artist_tracks_if_needed(
    *,
    artist_name: str,
    existing_track_ids: Set[str],
    navidrome_track_ids: Set[str],
) -> None:
    """Remove tracks no longer present in Navidrome."""
    if not existing_track_ids:
        return
    stale_ids = existing_track_ids - navidrome_track_ids
    if not stale_ids:
        return
    removed = delete_tracks_by_id(stale_ids, context=f"artist '{artist_name}'")
    if removed:
        logging.info("Removed %s stale track(s) for artist '%s'", removed, artist_name)
        logger.info(
            "Navidrome Import - %s - Removed %s stale track(s) no longer in library",
            artist_name,
            removed,
        )

def cleanup_empty_artist_dirs(*, artist_name: str, canonical_artist_name: str) -> None:
    """Remove empty album directories beneath the artist folder."""
    try:
        music_root = os.environ.get("MUSIC_FOLDER") or os.environ.get("MUSIC_ROOT") or "/music"
        artist_dir = os.path.join(music_root, canonical_artist_name)
        if not os.path.isdir(artist_dir):
            return
        for dirpath, _, _ in os.walk(artist_dir, topdown=False):
            if dirpath == artist_dir or os.listdir(dirpath):
                continue
            try:
                os.rmdir(dirpath)
                logging.info("Removed empty directory: %s", dirpath)
                logger.info(
                    "Navidrome Import - %s - Removed empty directory: %s",
                    artist_name,
                    os.path.basename(dirpath),
                )
            except OSError as err:
                logging.debug("Could not remove directory '%s': %s", dirpath, err)
    except Exception as err:
        logging.debug("Empty-folder cleanup skipped for '%s': %s", artist_name, err)


def normalize_sanitize_summary(summary: Any) -> Tuple[int, int]:
    """Normalize sanitizer return structures into (path_updates, duplicates_removed)."""
    if isinstance(summary, dict):
        path_updates = int(summary.get("path_updates", 0) or 0)
        duplicates_removed = int(summary.get("duplicates_removed", 0) or summary.get("duplicate_deletes", 0) or 0)
        return path_updates, duplicates_removed
    if isinstance(summary, tuple):
        path_updates = int(summary[0] or 0) if len(summary) > 0 else 0
        duplicates_removed = int(summary[1] or 0) if len(summary) > 1 else 0
        return path_updates, duplicates_removed
    return 0, 0
