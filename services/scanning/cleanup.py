"""Scanning cleanup orchestration helpers.

Provides safe wrappers around repository cleanup operations for
artist row normalization, file path sanitisation, and duplicate
removal during scanning.

Key Functions:
    - normalize_existing_artist_rows_safe(): Normalize artist name
      variations in existing database rows.
    - sanitize_artist_rows_safe(): Clean stale file paths and
      remove duplicate track entries.

Architecture:
    Best-effort wrappers that log errors instead of raising exceptions.
    Delegates to ``db.repositories.scan_cleanup_repository``.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Set, Tuple

from db.repositories.scan_repository import normalize_existing_artist_rows, sanitize_artist_file_paths_and_duplicates
from db.repositories.tracks import delete_tracks_by_id
from helpers.logging_config import log_unified


def normalize_existing_artist_rows_safe(*, artist_name: str, canonical_artist_name: str) -> None:
    """Best-effort wrapper around artist row normalization."""
    try:
        updated_rows = normalize_existing_artist_rows(canonical_artist_name=canonical_artist_name, aliases=[artist_name])
        if updated_rows:
            logging.info("[ARTIST_NORMALIZE] %s: normalized %s existing rows", canonical_artist_name, updated_rows)
    except Exception as err:
        logging.debug("[ARTIST_NORMALIZE] Skipped for %s: %s", canonical_artist_name, err)


def sanitize_artist_rows_safe(*, canonical_artist_name: str) -> None:
    """Best-effort stale path and duplicate cleanup wrapper."""
    try:
        summary = sanitize_artist_file_paths_and_duplicates(canonical_artist_name)
        path_updates, duplicates_removed = normalize_sanitize_summary(summary)
        if path_updates or duplicates_removed:
            logging.info("[NAVIDROME_SANITIZE] %s: normalized_paths=%s, duplicates_removed=%s", canonical_artist_name, path_updates, duplicates_removed)
    except Exception as err:
        logging.debug("[NAVIDROME_SANITIZE] Skipped for %s: %s", canonical_artist_name, err)


def cleanup_stale_album_tracks_if_needed(*, artist_name: str, album_name: str, cached_ids_for_album: Set[str], navidrome_tracks: list[Dict[str, Any]]) -> None:
    """Delete DB tracks that no longer exist in a Navidrome album."""
    nav_ids = {track.get("id") for track in navidrome_tracks if track.get("id")}
    stale_ids = cached_ids_for_album - nav_ids
    if not stale_ids:
        return
    removed = delete_tracks_by_id(stale_ids, context=f"album '{album_name}' (diff_mode)")
    if removed:
        log_unified(f"Navidrome Import - {artist_name} - Removed {removed} stale track(s) from album '{album_name}'")


def cleanup_stale_artist_tracks_if_needed(*, artist_name: str, existing_track_ids: Set[str], navidrome_track_ids: Set[str]) -> None:
    """Delete DB tracks that no longer exist in Navidrome for an artist.

    Safety: if Navidrome returned NO track ids at all, we cannot verify what
    was removed (fetch may have failed).  Deleting ``existing - empty`` would
    wipe the artist's entire local library, silently leaving every later scan
    with "No tracks found".  In that case we preserve local tracks.
    """
    if not existing_track_ids:
        return
    if not navidrome_track_ids:
        logging.warning(
            "[NAVIDROME_SCAN] %s: Navidrome returned no track IDs — skipping stale-track cleanup to preserve %s existing local track(s)",
            artist_name,
            len(existing_track_ids),
        )
        return
    stale_ids = existing_track_ids - navidrome_track_ids
    if not stale_ids:
        return
    removed = delete_tracks_by_id(stale_ids, context=f"artist '{artist_name}'")
    if removed:
        log_unified(f"Navidrome Import - {artist_name} - Removed {removed} stale track(s) no longer in library")


def cleanup_empty_artist_dirs(*, artist_name: str, canonical_artist_name: str) -> None:
    """Remove empty album directories under the artist folder."""
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
                log_unified(f"Navidrome Import - {artist_name} - Removed empty directory: {os.path.basename(dirpath)}")
            except OSError as err:
                logging.debug("Could not remove directory '%s': %s", dirpath, err)
    except Exception as err:
        logging.debug("Empty-folder cleanup skipped for '%s': %s", artist_name, err)


def normalize_sanitize_summary(summary: Any) -> Tuple[int, int]:
    """Return (path_updates, duplicates_removed) from dict/tuple summary."""
    if isinstance(summary, dict):
        return int(summary.get("path_updates", 0) or 0), int(summary.get("duplicates_removed", 0) or summary.get("duplicate_deletes", 0) or 0)
    if isinstance(summary, tuple):
        return int(summary[0] or 0) if len(summary) > 0 else 0, int(summary[1] or 0) if len(summary) > 1 else 0
    return 0, 0
