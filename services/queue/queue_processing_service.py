"""Queue processing service.

Handles post-download queue processing and organisation:
    - Matching completed downloads to queue items via metadata.
    - Extracting metadata from downloaded audio files.
    - Organising files into the library directory structure.
    - Updating queue status and library records.
    - Artist similarity scoring for match confidence.
"""

from __future__ import annotations

import difflib
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import structlog
from sqlalchemy import text

from db.engine import db_session
from db.repositories.queue import get_completed_group_queue_items, update_queue_item
from db.repositories.tracks import find_library_track
from helpers.logging_config import log_unified
from helpers.metadata_reader import read_mp3_metadata
from helpers.normalization_service import normalize_artist, queue_duration_seconds
from services.downloads.download_processing_service import add_to_queue
from services.metadata.tag_file_service import update_file_metadata

logger = structlog.get_logger(__name__)


def calculate_artist_similarity_score(expected_artist: str, candidate_artist: str) -> float:
    expected_norm = normalize_artist(expected_artist)
    candidate_norm = normalize_artist(candidate_artist)

    if not expected_norm or not candidate_norm:
        return 0.0

    if expected_norm == candidate_norm:
        return 1.0

    ratio = difflib.SequenceMatcher(None, expected_norm, candidate_norm).ratio()
    if expected_norm in candidate_norm or candidate_norm in expected_norm:
        ratio = max(ratio, 0.92)
    return ratio


def build_organize_group_target_path(
    music_root: str | os.PathLike[str],
    album_artist: str,
    year: Any,
    album_name: str,
    artist: str,
    title: str,
    track_number: Any,
    source_file: str | os.PathLike[str],
) -> Path:
    ext = Path(source_file).suffix.lower()
    track_prefix = f"{int(track_number):02d} - " if track_number is not None else ""
    album_folder = f"({year}) {album_name}" if year else album_name or "Unknown Album"
    relative_path = Path(album_artist or artist or "Unknown Artist") / album_folder
    return Path(music_root) / relative_path / f"{track_prefix}{title}{ext}"


def organize_group_sync(group_id: Any, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """Organize all completed queue items in an import group."""
    metadata = metadata or {}
    group_id = str(group_id)

    items = get_completed_group_queue_items(group_id)
    if not items:
        return {"success": False, "error": "No completed items found for this group"}

    album_artist = str(metadata.get("album_artist") or metadata.get("artist", "") or "").strip()
    year = str(metadata.get("year", "") or "").strip()
    album_name = str(metadata.get("album", "") or "").strip()
    artist_match_threshold = float(os.environ.get("QUEUE_ARTIST_MATCH_THRESHOLD", "0.78"))

    logger.info("Group organization started", group_id=group_id, album_artist=album_artist, album=album_name, year=year)

    updated_count = 0
    errors: list[str] = []

    for item in items:
        try:
            item_id = int(item.get("id") or 0)
            file_path = item.get("file_path")
            item_artist = item.get("artist") or ""
            item_title = item.get("title") or item.get("album") or ""
            item_track_number = item.get("track_number")
            item_disc_number = item.get("disc_number")
            item_album = item.get("album") or ""
            item_album_artist = item.get("album_artist") or ""
            item_year = item.get("year") or ""

            if not file_path or not os.path.exists(file_path):
                error_msg = f"File not found at {file_path}"
                errors.append(f"{item_title}: {error_msg}")
                logger.error("Item file not found", queue_id=item_id, path=file_path)
                continue

            resolved_album_artist = album_artist or item_album_artist or item_artist
            resolved_album_name = album_name or item_album or ""
            resolved_year = year or item_year

            try:
                with db_session() as session:
                    result = session.execute(
                        text("""
                            SELECT r.release_title, r.artist, r.release_year,
                                   rt.track_number, rt.track_title, rt.track_artist
                            FROM musicbrainz_release_tracks rt
                            JOIN musicbrainz_releases r ON r.release_id = rt.release_id
                            WHERE rt.queue_id = :id
                            LIMIT 1
                        """),
                        {"id": item_id},
                    )
                    mb_row = result.fetchone()
                if mb_row:
                    mapping = getattr(mb_row, "_mapping", None)
                    resolved_album_name = (mapping.get("release_title") if mapping else mb_row[0]) or resolved_album_name
                    resolved_album_artist = (mapping.get("artist") if mapping else mb_row[1]) or resolved_album_artist
                    resolved_year = (mapping.get("release_year") if mapping else mb_row[2]) or resolved_year
                    item_track_number = mapping.get("track_number") if mapping else mb_row[3]
                    item_title = (mapping.get("track_title") if mapping else mb_row[4]) or item_title
                    item_artist = (mapping.get("track_artist") if mapping else mb_row[5]) or item_artist
            except Exception as mb_item_err:
                logger.debug("MusicBrainz enrichment skipped", queue_id=item_id, error=str(mb_item_err))

            try:
                embedded_metadata = read_mp3_metadata(file_path) or {}
            except Exception as metadata_read_error:
                embedded_metadata = {}
                logger.warning("Could not read embedded metadata", queue_id=item_id, error=str(metadata_read_error))

            expected_artist = item_artist or resolved_album_artist
            artist_candidates = [embedded_metadata.get("artist"), embedded_metadata.get("album_artist")]
            scored_candidates = [
                (str(candidate), calculate_artist_similarity_score(expected_artist, candidate))
                for candidate in artist_candidates
                if candidate and str(candidate).strip()
            ]

            if not scored_candidates:
                error_msg = "Artist metadata check failed (no embedded artist/album_artist tags found)"
                errors.append(f"{item_title}: {error_msg}")
                update_queue_item(item_id, status="failed", failure_reason=error_msg)
                logger.warning("Artist metadata check failed", queue_id=item_id, error=error_msg)
                continue

            best_candidate, best_score = max(scored_candidates, key=lambda entry: entry[1])
            if best_score < artist_match_threshold:
                error_msg = (
                    f"Artist metadata mismatch (expected='{expected_artist}', found='{best_candidate}', "
                    f"score={best_score:.2f}, threshold={artist_match_threshold:.2f})"
                )
                errors.append(f"{item_title}: {error_msg}")
                update_queue_item(item_id, status="failed", failure_reason=error_msg)
                logger.warning("Artist metadata mismatch", queue_id=item_id, error=error_msg)
                continue

            update_queue_item(
                item_id,
                artist=item_artist,
                title=item_title,
                album=resolved_album_name,
                album_artist=resolved_album_artist,
                year=resolved_year,
                track_number=item_track_number,
            )

            file_metadata = {
                "title": item_title,
                "artist": item_artist,
                "album_artist": resolved_album_artist,
                "album": resolved_album_name,
                "year": resolved_year,
                "track_number": item_track_number,
                "disc_number": item_disc_number,
            }
            update_file_metadata(file_path, file_metadata)

            music_root = os.environ.get("MUSIC_ROOT", "/music")
            target_path = build_organize_group_target_path(
                music_root=music_root,
                album_artist=resolved_album_artist,
                year=resolved_year,
                album_name=resolved_album_name,
                artist=item_artist,
                title=item_title,
                track_number=item_track_number,
                source_file=file_path,
            )
            target_path.parent.mkdir(parents=True, exist_ok=True)
            if not target_path.exists():
                import shutil
                shutil.copy2(file_path, target_path)

            update_queue_item(
                item_id,
                status="imported",
                music_file_path=str(target_path),
                copied_individually=1,
                copied_individually_at=os.path.getmtime(target_path) if os.path.exists(target_path) else None,
            )
            updated_count += 1

        except Exception as exc:
            errors.append(f"{item.get('title') or item.get('album') or 'Unknown'}: {exc}")
            logger.exception("Error processing queue item during organization", queue_id=item.get("id"), error=str(exc))

    logger.info("Group organization complete", organized=updated_count, total=len(items))
    return {
        "success": True,
        "organized": updated_count,
        "total": len(items),
        "errors": errors,
        "message": f"Organized {updated_count}/{len(items)} files",
    }


def add_release_tracks_to_queue(
    release_id: str,
    tracks: list[dict[str, Any]],
    artist: str,
    album: str,
    album_artist: str | None = None,
    queue_source: str = "soulseek",
    year: int | None = None,
) -> list[int]:
    """Add normalized tracks to the download queue."""
    queue_ids: list[int] = []

    normalized_source = (queue_source or "soulseek").strip().lower()
    if normalized_source == "slskd":
        normalized_source = "soulseek"
    if normalized_source not in ("soulseek",):
        normalized_source = "soulseek"

    try:
        with db_session() as session:
            import_group = f"mbid_{release_id}"

            _active_statuses = {
                "queued", "searching", "downloading", "processing", "moving",
                "unmatched", "completed", "imported", "in_collection",
            }
            try:
                _existing_rows = session.execute(
                    text("""
                        SELECT id, status FROM download_queue
                        WHERE import_group = :grp OR release_id = :rid
                    """),
                    {"grp": import_group, "rid": release_id},
                ).fetchall() or []
                
                _active_rows = [
                    r for r in _existing_rows
                    if str((getattr(r, "_mapping", None) or {}).get("status") or r[1] or "").lower() in _active_statuses
                ]
                if _active_rows:
                    logger.info("Release already has active queue items — skipping re-queue", release_id=release_id, active_count=len(_active_rows))
                    return []
                    
                _stale_ids = [
                    (getattr(r, "_mapping", None) or {}).get("id") or r[0]
                    for r in _existing_rows
                ]
                if _stale_ids:
                    _in_placeholders = ", ".join(f":sid_{i}" for i in range(len(_stale_ids)))
                    session.execute(
                        text(f"DELETE FROM download_queue WHERE id IN ({_in_placeholders})"),
                        {f"sid_{i}": sid for i, sid in enumerate(_stale_ids)},
                    )
                    logger.info("Removed stale queue rows before re-queue", release_id=release_id, stale_count=len(_stale_ids))
            except Exception as _cleanup_exc:
                logger.debug("Stale-row cleanup skipped", release_id=release_id, error=str(_cleanup_exc))

            seen_recordings: set[str] = set()

            for track in tracks:
                track_title = track.get("title") or "Unknown Track"
                track_artist = track.get("artist") or artist
                track_number = track.get("track_number")
                disc_number = track.get("disc_number", 1)
                recording_mbid = track.get("recording_mbid")

                dedupe_key = str(recording_mbid or "").strip().lower()
                if not dedupe_key:
                    dedupe_key = re.sub(r"[^a-z0-9]+", " ", track_title.lower()).strip()
                if dedupe_key in seen_recordings:
                    logger.info("Skipping duplicate recording in release", artist=track_artist, title=track_title)
                    continue
                seen_recordings.add(dedupe_key)

                duration = queue_duration_seconds(
                    track.get("duration") or track.get("length")
                )

                existing = find_library_track(
                    artist=track_artist,
                    title=track_title,
                    album=album,
                )
                if existing:
                    logger.info("Skipping existing library track", artist=track_artist, title=track_title)
                    continue

                # ── Per-track dedupe against ANY active queue row ─────────
                # The release-level guard above only catches rows sharing the
                # SAME import_group/release_id.  The same track can already be
                # active under a DIFFERENT import group (matched-folder flows,
                # manual adds, upcoming-releases, a previous release ID) —
                # without this check the raw INSERT below would create yet
                # another soulseek row, piling up "20 versions of one song".
                # Match on normalized (artist, title) across all active
                # statuses and any source.
                _dup_row = session.execute(
                    text("""
                        SELECT id FROM download_queue
                        WHERE LOWER(COALESCE(artist, '')) = LOWER(:artist)
                          AND LOWER(COALESCE(title, '')) = LOWER(:title)
                          AND status IN
                            ('queued', 'searching', 'downloading', 'processing',
                             'moving', 'unmatched', 'completed', 'imported',
                             'in_collection', 'matched')
                        ORDER BY created_at ASC
                        LIMIT 1
                    """),
                    {"artist": track_artist, "title": track_title},
                ).fetchone()
                if _dup_row is not None:
                    logger.info(
                        "Skipping track — already active in queue",
                        artist=track_artist, title=track_title,
                        existing_queue_id=_dup_row[0],
                    )
                    continue

                search_query = f"{track_artist} - {track_title}"

                result = session.execute(
                    text("""
                        INSERT INTO download_queue
                        (
                            artist, album, title, search_query, source, status,
                            release_id, import_group, track_number, disc_number,
                            album_artist, recording_mbid, duration, year, release_year,
                            created_at, updated_at
                        )
                        VALUES
                        (
                            :artist, :album, :title, :search_query, :source, 'queued',
                            :release_id, :import_group, :track_number, :disc_number,
                            :album_artist, :recording_mbid, :duration, :year, :release_year,
                            CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                        )
                        RETURNING id
                    """),
                    {
                        "artist": track_artist,
                        "album": album,
                        "title": track_title,
                        "search_query": search_query,
                        "source": normalized_source,
                        "release_id": release_id,
                        "import_group": import_group,
                        "track_number": track_number,
                        "disc_number": disc_number,
                        "album_artist": album_artist or artist,
                        "recording_mbid": recording_mbid,
                        "duration": duration,
                        "year": str(year) if year else None,
                        "release_year": year,
                    },
                )
                
                queue_id = result.scalar_one_or_none()
                if queue_id is not None:
                    queue_ids.append(int(queue_id))

            logger.info("Added tracks to queue for release", count=len(queue_ids), release_id=release_id)
            if queue_ids:
                log_unified(f"[QUEUE] Queued {len(queue_ids)} track(s): {artist} - {album} (release {release_id})")
                try:
                    from services.queue.queue_diagnostics_service import log_queue_event
                    log_queue_event(
                        "queued",
                        f"Queued {len(queue_ids)} track(s): {artist} - {album} (release {release_id})",
                        queue_id=None,
                    )
                except Exception:
                    pass

        return queue_ids

    except Exception as e:
        logger.error("Failed adding tracks to queue", error=str(e), exc_info=True)
        raise


def handle_unmatched_file(file_path: str, file_metadata: dict[str, Any]) -> dict[str, Any] | None:
    """File found in /downloads but doesn't match queue."""
    artist = file_metadata.get("artist", "").strip()
    title = file_metadata.get("title", "").strip()
    album = file_metadata.get("album", "").strip()

    if not artist or not title:
        logger.warning("Cannot add unmatched file without artist/title", path=file_path)
        return None

    queue_id = add_to_queue(
        artist=artist,
        title=title,
        album=album,
        source="local",
        status="unmatched",
        matched_file_path=file_path,
    )

    if queue_id:
        qid = queue_id.get("id") if isinstance(queue_id, dict) else queue_id
        logger.info("Added unmatched file to queue", artist=artist, title=title, queue_id=qid)

    return queue_id
