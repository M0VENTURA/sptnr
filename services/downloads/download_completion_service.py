"""Download completion reconciliation service.

When the Soulseek pipeline requests a download it marks the queue item
``downloading`` and stores the remote ``found_filename``.  This service is
the missing final stage: it watches for the file actually landing on disk and
transfers it into the music library.

Matching precedence (highest first):
1. slskd's completed-transfers API — each entry carries a ``localFilePath``
   that maps the remote filename to the exact on-disk location (no walk).
2. Exact filename match — ``found_filename`` (or its basename) against the
   downloads folder walk.
3. Fuzzy match — ``_score_soulseek_candidate`` + metadata verification so
   wrong-version / false-positive files are rejected instead of imported.

After a match is found the file is verified against the queue item's expected
metadata/duration, moved to /music via ``move_track_to_library``, and the row
is promoted to the terminal ``imported`` state (removed from the active queue).

Items whose slskd transfer reached a terminal failed state, or that went stale
while marked ``downloading`` with no file present, are marked failed so the
automatic retry scheduler can re-download them instead of leaving them stuck.

Callers:
    - services/downloads/download_scan_service.check_completed_downloads
      (registered as a queue_orchestrator maintenance hook)
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text
from db.engine import db_session

from helpers.config_helpers import (
    _SLSKD_MIN_ACCEPT_SCORE,
)
from helpers.normalization_service import queue_duration_seconds

logger = logging.getLogger(__name__)

_MUSIC_ROOT = os.environ.get("MUSIC_ROOT", "/music")


def _log_queue_event(event_type: str, message: str, queue_id: int | None) -> None:
    """Record a queue event to the in-memory store and ``queue.log``."""
    try:
        from services.queue.queue_diagnostics_service import log_queue_event
        log_queue_event(event_type, message, queue_id=queue_id)
    except Exception:
        pass

# Files that have been downloading for longer than this with no progress and
# no file are considered abandoned.
_STALE_DOWNLOADING_MINUTES = 60

# Legacy parity timeouts for active/remotely-queued slskd transfers.
_SLSKD_ACTIVE_STATE_TIMEOUT_MINUTES = 60
_SLSKD_REMOTELY_QUEUED_TIMEOUT_MINUTES = 60


# =============================================================================
# TRANSFER KEY NORMALISATION
# =============================================================================

def _normalize_transfer_key(value: str) -> Optional[str]:
    """Normalise a transfer/filename for dict lookups (lower, forward slashes)."""
    if not value:
        return None
    norm = str(value).replace("\\", "/").strip()
    return norm.lower() or None


def _db_now_naive() -> datetime:
    """Return the DB server's current time as a *naive* datetime in the DB
    session's wall-clock.

    ``download_queue.updated_at`` is a naive ``TIMESTAMP`` written by Postgres
    ``CURRENT_TIMESTAMP`` (cast to the session's local wall-clock). Staleness
    must therefore be computed against the DB's own clock in that same
    wall-clock — comparing against Python's ``datetime.utcnow()`` breaks when
    the DB session timezone is not UTC (the two disagree by the timezone
    offset and stale recovery silently never fires, leaving items stuck in
    ``moving`` forever). Mirrors the old_system recovery which used SQL
    ``CURRENT_TIMESTAMP - INTERVAL``.
    """
    try:
        with db_session() as session:
            value = session.execute(text("SELECT CURRENT_TIMESTAMP")).scalar()
        if value is not None:
            if isinstance(value, str):
                # SQLite returns CURRENT_TIMESTAMP as a string — parse it so
                # callers always receive a datetime (test_db_now_naive_parses_sqlite_string).
                try:
                    value = datetime.fromisoformat(value)
                except ValueError:
                    return value
            if getattr(value, "tzinfo", None) is not None:
                # psycopg2 returns timestamptz in the session timezone; dropping
                # the offset keeps the same wall-clock that was stored in the
                # naive updated_at column, so the difference is the true age.
                value = value.replace(tzinfo=None)
            return value
    except Exception:
        pass
    return datetime.utcnow()


def _remember_failed_peer(transfer: dict) -> None:
    """Tell the download pipeline to avoid this peer for the failed file.

    A transfer that errored (``Completed, Errored``) or that reports success
    while its file is unfindable will otherwise be re-searched on every retry
    and land on the same peer repeatedly. The pipeline keeps a short TTL
    block so retries prefer a different peer when one exists.
    """
    try:
        from services.downloads.download_pipeline_service import _block_peer
        _block_peer(transfer.get("username"), transfer.get("filename"))
    except Exception:
        pass


def _monitored_downloads_dir() -> str:
    """The downloads directory the completion service scans, for diagnostics."""
    try:
        from services.downloads.download_scan_service import resolve_downloads_dir
        # Must match the walk in ``check_completed_downloads`` (downloads root,
        # not the ``Music`` subfolder preference) so diagnostics reflect the
        # directory that is actually scanned.
        return resolve_downloads_dir(prefer_music_subfolder=False)
    except Exception:
        return "?"


# Grace window before a successful slskd transfer is declared "local file not
# found": slskd can report completion while the file is still landing on disk
# (volume sync, delayed move).  Polls × seconds ≈ 30s worst case.
_FILE_ARRIVAL_POLLS = 3
_FILE_ARRIVAL_POLL_SECONDS = 10


def _wait_for_transfer_file(found_filename: str, local_file_path: str) -> Optional[str]:
    """Poll up to ~30s for a just-completed transfer to appear on disk.

    Checks the monitored downloads directory (basename match) plus the
    slskd-reported ``localFilePath``.  Returns the path that appeared, or
    None when the file never landed.
    """
    import time as _time

    monitored = _monitored_downloads_dir()
    candidates = []
    base = os.path.basename(found_filename or "")
    if monitored and monitored != "?" and base:
        candidates.append(os.path.join(monitored, base))
    if local_file_path:
        candidates.append(local_file_path)

    if not candidates:
        return None

    for _ in range(_FILE_ARRIVAL_POLLS):
        for path in candidates:
            try:
                if path and os.path.isfile(path):
                    return path
            except Exception:
                pass
        _time.sleep(_FILE_ARRIVAL_POLL_SECONDS)
    return None


def _is_stale_queue_item(
    item: dict,
    stale_minutes: int = 10,
    now: datetime | None = None,
) -> bool:
    """True when a queue row's ``updated_at`` is older than *stale_minutes*.

    *now* should be the DB clock (``_db_now_naive()``) computed once per
    reconciliation cycle; when omitted it is fetched lazily. Comparing against
    the DB clock keeps the check correct regardless of the app/DB timezone.
    """
    updated_at = item.get("updated_at")
    if not updated_at:
        return False
    try:
        updated_text = str(updated_at).replace("Z", "+00:00")
        updated_dt = datetime.fromisoformat(updated_text)
        if updated_dt.tzinfo is not None:
            updated_dt = updated_dt.astimezone(timezone.utc).replace(tzinfo=None)
        if now is None:
            now = _db_now_naive()
        return (now - updated_dt).total_seconds() >= (stale_minutes * 60)
    except Exception:
        return False


# =============================================================================
# FILE MATCHING
# =============================================================================

def _build_download_index(fs_files: list[dict[str, Any]]) -> dict[str, Any]:
    """Index the walked downloads tree ONCE per reconciliation cycle.

    The previous per-item matching loop re-walked the entire tree for every
    ``downloading`` item (N items × M files, each with a metadata read), which
    hammered file storage every ~30s cycle. The index turns the hot paths
    (exact filename, fuzzy basename-token) into dict lookups.

    Returns:
        {
            "by_rel_norm": {normalised rel_path -> file},
            "by_basename": {lowercased basename -> [file, ...]},
            "by_token":    {meaningful basename token -> [file, ...]},
        }
    """
    by_rel_norm: dict[str, dict[str, Any]] = {}
    by_basename: dict[str, list[dict[str, Any]]] = {}
    by_token: dict[str, list[dict[str, Any]]] = {}

    try:
        from services.queue.queue_scoring import _tokenize_meaningful
    except Exception:
        _tokenize_meaningful = None

    for f in fs_files:
        rel = f["rel_path"].replace("\\", "/")
        norm = _normalize_transfer_key(rel)
        if norm:
            by_rel_norm.setdefault(norm, f)
        base = os.path.basename(rel)
        by_basename.setdefault(base.lower(), []).append(f)
        if _tokenize_meaningful is not None:
            for token in set(_tokenize_meaningful(base)):
                by_token.setdefault(token, []).append(f)

    return {
        "by_rel_norm": by_rel_norm,
        "by_basename": by_basename,
        "by_token": by_token,
    }


def _fuzzy_candidates(
    fs_index: dict[str, Any],
    item: dict,
    fs_files: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return likely fuzzy-match candidates for one queue item, cheapest first.

    Pre-filters the walked tree using meaningful basename tokens from the
    queue item's title and artist. ``_score_soulseek_candidate`` already
    rejects basenames sharing <50% of title tokens, so any candidate that
    could score above the bar shares at least one title token — restricting
    the scan to those files is safe. Artist tokens are included because
    downloaded files virtually always carry the artist name, and cover the
    metadata-only-match case where the filename itself is unhelpful.

    Falls back to the full file list only when the item has no meaningful
    title or artist tokens (so metadata-only matches are still found).
    """
    try:
        from services.queue.queue_scoring import (
            _tokenize_meaningful,
            normalize_core_title,
        )
        from helpers.normalization_service import normalize_artist
    except Exception:
        return fs_files

    tokens: set[str] = set(_tokenize_meaningful(normalize_core_title(item.get("title") or "")))
    tokens |= set(_tokenize_meaningful(normalize_artist(item.get("artist") or "") or ""))

    token_index = fs_index.get("by_token") or {}
    seen: dict[str, dict[str, Any]] = {}
    for token in tokens:
        for f in token_index.get(token, []):
            seen.setdefault(f["full_path"], f)

    if seen:
        return list(seen.values())
    return fs_files


def _file_matches_queue_item(
    file_path: str,
    queue_item: dict,
    relative_name: str | None = None,
) -> tuple[bool, str]:
    """Return ``(is_match, source)`` for a downloaded file against a queue item.

    ``source`` is ``"metadata"`` when the file's embedded tags match,
    otherwise ``"filename"`` (both artist and title present in the path).
    """
    from services.queue.queue_metadata_matcher import _metadata_matches_queue_item
    from services.downloads.match_engine import filename_matches_queue_item

    # 1. Strongest: embedded metadata agrees with the queue item.
    meta_state = None
    try:
        meta_state = _metadata_matches_queue_item(file_path, queue_item)
    except Exception as exc:
        logger.debug("[COMPLETE] metadata check failed for %s: %s", file_path, exc)

    if meta_state is True:
        return True, "metadata"
    if meta_state is False:
        return False, "metadata"

    # 2. Metadata unavailable/ambiguous — fall back to filename/path matching.
    #    This requires both artist AND title to appear in the path (or a high
    #    combined score) so false positives (wrong version, wrong album) are
    #    not imported.
    try:
        is_match = filename_matches_queue_item(file_path, queue_item)
    except Exception as exc:
        logger.debug("[COMPLETE] filename match failed for %s: %s", file_path, exc)
        is_match = False
    return bool(is_match), "filename"


def _delete_mismatched_download(file_path: str, queue_id: int, reason: str) -> None:
    """Delete a downloaded file that does not match its queue item."""
    try:
        if os.path.isfile(file_path):
            os.remove(file_path)
            logger.warning("[COMPLETE] Queue %s: deleted mismatched download %s (%s)", queue_id, file_path, reason)
    except Exception as exc:
        logger.warning("[COMPLETE] Queue %s: could not delete mismatched file %s: %s", queue_id, file_path, exc)


def _extract_duration_seconds(file_path: str) -> Optional[int]:
    """Return the file duration in seconds, or None when unreadable."""
    try:
        from helpers.metadata_reader import read_mp3_metadata
        meta = read_mp3_metadata(file_path) or {}
        dur = meta.get("duration_ms")
        if not dur:
            return None
        return int(dur) // 1000 if int(dur) > 1000 else int(dur)
    except Exception:
        return None


# =============================================================================
# MOVE / IMPORT
# =============================================================================

def _apply_stored_metadata(item: dict, file_path: str) -> None:
    """Best-effort write of the queue item's (MusicBrainz-matched) metadata.

    Mirrors the old_system finalizer so files copied into /music carry the
    corrected title/artist/album/year and MusicBrainz IDs rather than whatever
    tags the download source embedded. Only non-empty fields are written so a
    partial queue row never wipes existing tags. Never raises — tag failures
    are non-fatal and must not block the move.
    """
    meta: dict[str, Any] = {
        "title": item.get("title"),
        "artist": item.get("artist"),
        "album": item.get("album"),
        "album_artist": item.get("album_artist") or item.get("artist"),
        "year": item.get("year"),
        "track_number": item.get("track_number"),
        "disc_number": item.get("disc_number"),
    }
    meta = {k: v for k, v in meta.items() if v not in (None, "")}
    recording_mbid = item.get("recording_mbid")
    if recording_mbid:
        meta["recording_mbid"] = recording_mbid
    release_mbid = item.get("release_mbid") or item.get("release_id")
    if release_mbid:
        meta["release_mbid"] = release_mbid
    if not meta:
        return

    try:
        from services.metadata.tag_file_service import update_file_metadata
        update_file_metadata(file_path, meta)
    except Exception as exc:
        logger.debug("[COMPLETE] Queue %s: could not apply stored metadata to %s: %s", item.get("id"), file_path, exc)


def _move_and_import(item: dict, abs_path: str, match_source: str) -> dict[str, Any]:
    """Move *abs_path* into the music library and promote the item to imported.

    Guards against double-moves with an atomic ``downloading -> moving`` claim.
    Any failure after the claim resets the row back to ``downloading`` so the
    next cycle retries it instead of leaving it stuck in ``moving`` forever.
    """
    queue_id = item.get("id")

    # Atomically claim the move so a concurrent caller (UI button, another
    # worker cycle) cannot move the same file twice.
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    UPDATE download_queue
                    SET status = 'moving', updated_at = CURRENT_TIMESTAMP
                    WHERE id = :qid AND status = 'downloading'
                """),
                {"qid": queue_id},
            )
            if result.rowcount == 0:
                logger.info("[COMPLETE] Queue %s: move already claimed elsewhere — skipping", queue_id)
                return {"success": False, "error": "already_claimed"}
    except Exception as exc:
        logger.error("[COMPLETE] Queue %s: could not claim move: %s", queue_id, exc)
        return {"success": False, "error": str(exc)}

    def _reset_to_downloading(reason: str) -> None:
        try:
            from db.repositories.queue import update_queue_item
            update_queue_item(queue_id, status="downloading")
        except Exception:
            pass
        logger.warning("[COMPLETE] Queue %s: reset to downloading (%s)", queue_id, reason)

    try:
        from services.downloads.download_organize_helpers import move_track_to_library
        from services.downloads.download_verification_service import (
            verify_file_in_music,
            mark_queue_item_moved,
        )
        from db.repositories.queue import update_queue_item

        # Apply the stored MusicBrainz metadata to the file before moving so
        # the copy in /music arrives with the corrected name and information.
        _apply_stored_metadata(item, abs_path)

        track = {
            "file_path": abs_path,
            "artist": item.get("artist"),
            "title": item.get("title"),
            "track_number": item.get("track_number"),
            "disc_number": item.get("disc_number"),
        }
        release_metadata = {
            "album_artist": item.get("album_artist") or item.get("artist"),
            "album": item.get("album"),
            "year": item.get("year"),
        }

        move_result = move_track_to_library(track, release_metadata, _MUSIC_ROOT)
        if not move_result.get("success"):
            logger.warning("[COMPLETE] Queue %s: move failed (%s) — resetting to downloading", queue_id, move_result.get("error"))
            _reset_to_downloading(f"move failed: {move_result.get('error')}")
            return move_result

        target_path = move_result.get("target_path")
        if not target_path:
            _reset_to_downloading("move returned no target path")
            return {"success": False, "error": "missing target path"}

        # Re-apply the stored metadata to the FINAL file: FLAC→MP3 conversion
        # rewrites the container, and ffmpeg's ``-map_metadata 0`` may drop or
        # rename custom frames (e.g. MUSICBRAINZ ids). Writing the tags again
        # on the target guarantees the library copy carries the queue's
        # MusicBrainz information (artist, track name, track number, MBIDs).
        if os.path.isfile(target_path):
            _apply_stored_metadata(item, target_path)

        # Persist the duration if the row lacks it (added without MusicBrainz data).
        if not item.get("duration"):
            # Prefer the final file — the original source may have been
            # converted/deleted by FLAC→MP3 conversion.
            duration = _extract_duration_seconds(target_path) or _extract_duration_seconds(abs_path)
            if duration:
                try:
                    update_queue_item(queue_id, duration=duration)
                except Exception:
                    pass

        verify_result = verify_file_in_music(queue_id, target_path)
        file_exists = bool(target_path) and os.path.isfile(target_path)

        if verify_result.get("success") or file_exists:
            try:
                mark_queue_item_moved(queue_id, target_path)
                update_queue_item(
                    queue_id,
                    status="imported",
                    music_file_path=target_path,
                    file_path=target_path,
                    found_filename=os.path.basename(target_path),
                    copied_individually=1,
                    copied_individually_at=datetime.now().isoformat(),
                )
            except Exception as exc:
                logger.error("[COMPLETE] Queue %s: status update failed (%s) — resetting to downloading", queue_id, exc)
                _reset_to_downloading(f"status update failed: {exc}")
                return {"success": False, "error": str(exc)}
            logger.info(
                "[AUTO_MOVE] Queue %s: verified and imported to %s (source=%s)",
                queue_id, target_path, match_source,
            )
            return {"success": True, "target_path": target_path}

        # Verification failed and the file is not present at the target — the move
        # may have been partial. Requeue so a fresh download can be attempted.
        logger.error("[COMPLETE] Queue %s: verification failed (%s)", queue_id, verify_result.get("error"))
        try:
            from db.repositories.queue import mark_failed
            mark_failed(queue_id, f"Move verification failed: {verify_result.get('error')}")
        except Exception:
            pass
        return {"success": False, "error": verify_result.get("error")}

    except Exception as exc:
        # An unexpected error after the claim must not leave the row stuck in
        # 'moving' — drop it back to 'downloading' so the next cycle retries.
        logger.error("[COMPLETE] Queue %s: unhandled error during move — resetting to downloading: %s", queue_id, exc, exc_info=True)
        _reset_to_downloading(f"unhandled error: {exc}")
        return {"success": False, "error": str(exc)}


def _sync_download_progress(active: list[dict], downloading: list[dict]) -> None:
    """Best-effort sync of slskd transfer progress onto 'downloading' queue rows.

    Keeps the progress column (surfaced by the queue UI progress bars) current
    without a dedicated sync loop.
    """
    if not active or not downloading:
        return

    # Index active transfers by remote filename and basename.
    transfer_by_key: dict[str, dict] = {}
    for t in active:
        filename = _normalize_transfer_key(t.get("filename") or "")
        if filename:
            transfer_by_key[filename] = t
            transfer_by_key[os.path.basename(filename)] = t

    for item in downloading:
        found_fn = (item.get("found_filename") or "").strip()
        found_norm = _normalize_transfer_key(found_fn)
        if not found_norm:
            continue
        transfer = (
            transfer_by_key.get(found_norm)
            or transfer_by_key.get(os.path.basename(found_norm))
        )
        if not transfer:
            continue
        try:
            from db.repositories.queue import update_queue_item
            update_queue_item(
                item.get("id"),
                progress=transfer.get("progress"),
                speed=transfer.get("averageSpeed"),
            )
        except Exception:
            pass


# =============================================================================
# TRANSFER STATE RECONCILIATION
# =============================================================================

def _reconcile_stale_moving(stale_minutes: int = 10) -> dict[str, int]:
    """Recover queue items left in ``moving`` by a crashed/restarted worker.

    ``_move_and_import`` atomically claims ``downloading -> moving`` before the
    file is transferred and only promotes to ``imported`` after the file is
    verified in /music. A worker killed mid-move therefore leaves the row stuck
    in ``moving`` with no automatic recovery (the old system required a manual
    "reset moving" click). Reconcile stale ``moving`` rows:

    - target file present in /music          -> promote to ``imported``
    - source file still in /downloads        -> reset to ``downloading`` (retry move)
    - neither present                        -> reset to ``downloading`` so the
                                               normal download reconciliation marks
                                               it failed instead of leaving it stuck
    """
    stats = {"imported": 0, "reset": 0, "skipped": 0}

    try:
        from services.downloads.download_organize_helpers import _build_target_path
        from db.repositories.queue import update_queue_item
    except Exception:
        return stats

    # Staleness is evaluated against the DB's own clock (CURRENT_TIMESTAMP)
    # rather than a Python-computed UTC cutoff: ``updated_at`` is a naive
    # TIMESTAMP written from CURRENT_TIMESTAMP, so comparing to the same clock
    # in SQL is immune to app/DB timezone mismatch (the reason an item could
    # remain stuck in 'moving' indefinitely).
    try:
        with db_session() as session:
            rows = session.execute(text("""
                SELECT * FROM download_queue
                WHERE status = 'moving'
                  AND updated_at < CURRENT_TIMESTAMP - make_interval(mins => :stale_minutes)
            """), {"stale_minutes": stale_minutes}).fetchall() or []
            items = [dict(r._mapping) for r in rows]
    except Exception as exc:
        logger.error("[COMPLETE] Could not fetch stale 'moving' items: %s", exc)
        return stats

    for item in items:
        queue_id = item.get("id")
        try:
            # 1. The file may already have reached /music before the crash.
            target = item.get("music_file_path")
            if not (target and os.path.isfile(str(target))):
                try:
                    target = _build_target_path(
                        _MUSIC_ROOT,
                        item.get("album_artist") or item.get("artist"),
                        item.get("year"),
                        item.get("album"),
                        item.get("artist"),
                        item.get("title"),
                        item.get("track_number"),
                        item.get("file_path") or "track.mp3",
                        disc_number=item.get("disc_number"),
                    )
                except Exception:
                    target = None

            # FLAC→MP3 conversion rewrites the extension — a crash between the
            # conversion and the status update leaves the .mp3 in the library.
            if not (target and os.path.isfile(str(target))) and target:
                try:
                    mp3_variant = f"{os.path.splitext(str(target))[0]}.mp3"
                    if os.path.isfile(mp3_variant):
                        target = mp3_variant
                except Exception:
                    pass

            if target and os.path.isfile(str(target)):
                update_queue_item(
                    queue_id,
                    status="imported",
                    music_file_path=str(target),
                    file_path=str(target),
                    found_filename=os.path.basename(str(target)),
                    copied_individually=1,
                    copied_individually_at=datetime.now().isoformat(),
                )
                stats["imported"] += 1
                logger.info(
                    "[COMPLETE] Queue %s: recovered stale 'moving' item — file found in library at %s",
                    queue_id, target,
                )
                try:
                    _log_queue_event("imported", f"Recovered stale 'moving' item — file found in library: {os.path.basename(str(target))}", queue_id)
                except Exception:
                    pass
            else:
                # File not yet in /music: put the item back into the normal
                # 'downloading' flow so matching/moving is retried (or the
                # download reconciliation marks it failed if it is truly gone).
                update_queue_item(queue_id, status="downloading")
                stats["reset"] += 1
                logger.info(
                    "[COMPLETE] Queue %s: recovered stale 'moving' item — reset to downloading for re-match",
                    queue_id,
                )
                try:
                    _log_queue_event("downloading", "Recovered stale 'moving' item — reset to downloading for re-match", queue_id)
                except Exception:
                    pass
        except Exception as exc:
            logger.warning("[COMPLETE] Queue %s: stale 'moving' recovery failed: %s", queue_id, exc)
            stats["skipped"] += 1

    return stats


def _reconcile_transfer_state(
    item: dict,
    slskd,
    active: list[dict] | None = None,
    now: datetime | None = None,
) -> bool:
    """Reconcile a 'downloading' item against live slskd transfers.

    Returns True when the item was moved to a terminal state (failed) and
    should be skipped for file matching this cycle.
    """
    queue_id = item.get("id")
    found_fn = (item.get("found_filename") or "").strip()

    if active is None:
        try:
            active = slskd.get_active_downloads()
        except Exception as exc:
            logger.debug("[COMPLETE] Could not fetch active transfers: %s", exc)
            active = []

    if not active:
        # slskd has no record of the transfer at all.
        if _is_stale_queue_item(item, stale_minutes=10, now=now):
            logger.warning("[COMPLETE] Queue %s: missing from slskd transfers and stale — scheduling retry", queue_id)
            from db.repositories.queue import mark_failed
            mark_failed(queue_id, "Transfer missing from slskd API while marked downloading")
            _log_queue_event("failed", "Missing from slskd transfers and stale — scheduling retry", queue_id)
            return True
        return False

    # Index transfers by remote filename and basename.
    transfer = None
    found_norm = _normalize_transfer_key(found_fn)
    for t in active:
        filename = _normalize_transfer_key(t.get("filename") or "")
        if found_norm and filename and (
            filename == found_norm or os.path.basename(filename) == os.path.basename(found_norm)
        ):
            transfer = t
            break

    if transfer is None:
        if _is_stale_queue_item(item, stale_minutes=10, now=now):
            logger.warning("[COMPLETE] Queue %s: transfer not found and item stale — scheduling retry", queue_id)
            from db.repositories.queue import mark_failed
            mark_failed(queue_id, "Transfer missing from slskd API while marked downloading")
            _log_queue_event("failed", "Transfer not found and item stale — scheduling retry", queue_id)
            return True
        return False

    state = slskd.state_text(transfer.get("state"))
    failed_states = slskd.FAILED_STATES

    if state in failed_states:
        logger.warning("[COMPLETE] Queue %s: slskd reports failed state %r — scheduling retry", queue_id, state)
        _remember_failed_peer(transfer)
        from db.repositories.queue import mark_failed
        mark_failed(queue_id, f"slskd transfer failed: {state}")
        _log_queue_event("failed", f"slskd transfer failed: {state} — scheduling retry", queue_id)
        return True

    if slskd.is_success_state(state):
        # slskd reports success but the file may still be landing on disk
        # (container volume sync, delayed move, filesystem flush).  Wait up
        # to ~30s before declaring the transfer missing so transient path
        # delays never fail an otherwise-good import.
        local = str(transfer.get("localFilePath") or "")
        landed = _wait_for_transfer_file(found_fn, local)
        if landed:
            logger.info(
                "[COMPLETE] Queue %s: transfer file appeared within the grace window — %s",
                queue_id, landed,
            )
            return False  # not terminal — normal file matching takes over

        # slskd reports success but no file was found — the file was likely
        # deleted before the queue processor could match it, or (more common)
        # it was saved somewhere outside the monitored downloads folder (the
        # slskd container's download path vs Popularr's DOWNLOADS_DIR).
        # Log the exact paths so a path mismatch is self-diagnosing instead
        # of an endless silent fail→retry loop.
        logger.warning(
            "[COMPLETE] Queue %s: slskd succeeded but no file found — removing stale transfer and retrying. "
            "slskd localFilePath=%r; queue found_filename=%r; monitored downloads dir=%r. "
            "If the file exists at the localFilePath but is not in the monitored dir, "
            "align DOWNLOADS_DIR / downloads.monitor_folder with slskd's download directory.",
            queue_id,
            local or "(empty)",
            found_fn,
            _monitored_downloads_dir(),
        )
        _remember_failed_peer(transfer)
        try:
            transfer_id = str(transfer.get("id") or "")
            username = str(transfer.get("username") or "")
            if transfer_id and username:
                slskd.cancel_download(username, transfer_id, remove=True)
        except Exception as exc:
            logger.debug("[COMPLETE] Queue %s: could not remove stale transfer: %s", queue_id, exc)
        from db.repositories.queue import mark_failed
        mark_failed(queue_id, "slskd transfer succeeded but local file not found")
        _log_queue_event("failed", "slskd transfer succeeded but local file not found — retrying", queue_id)
        return True

    if state == slskd.STATE_QUEUED_REMOTELY:
        if _is_stale_queue_item(item, stale_minutes=_SLSKD_REMOTELY_QUEUED_TIMEOUT_MINUTES, now=now):
            logger.warning("[COMPLETE] Queue %s: remotely queued too long — cancelling and retrying", queue_id)
            try:
                transfer_id = str(transfer.get("id") or "")
                username = str(transfer.get("username") or "")
                if transfer_id and username:
                    slskd.cancel_download(username, transfer_id, remove=True)
            except Exception:
                pass
            _remember_failed_peer(transfer)
            from db.repositories.queue import mark_failed
            mark_failed(queue_id, "Remotely queued too long")
            _log_queue_event("failed", "Remotely queued too long — cancelling and retrying", queue_id)
            return True
        return False

    if state in slskd.ACTIVE_STATES:
        if _is_stale_queue_item(item, stale_minutes=_SLSKD_ACTIVE_STATE_TIMEOUT_MINUTES, now=now):
            logger.warning("[COMPLETE] Queue %s: download timed out (state=%r) — cancelling and retrying", queue_id, state)
            try:
                transfer_id = str(transfer.get("id") or "")
                username = str(transfer.get("username") or "")
                if transfer_id and username:
                    slskd.cancel_download(username, transfer_id, remove=True)
            except Exception:
                pass
            _remember_failed_peer(transfer)
            from db.repositories.queue import mark_failed
            mark_failed(queue_id, f"slskd download timed out ({state})")
            _log_queue_event("failed", f"slskd download timed out ({state}) — cancelling and retrying", queue_id)
            return True

    return False


# =============================================================================
# MAIN ENTRYPOINT
# =============================================================================

def check_completed_downloads() -> dict[str, Any]:
    """Reconcile 'downloading' queue items against completed downloads.

    Returns a summary dict compatible with the queue_orchestrator maintenance
    hook contract (``{"success": True, ...}``).
    """
    stats: dict[str, Any] = {
        "success": True,
        "downloading_items": 0,
        "imported": 0,
        "failed": 0,
        "skipped": 0,
        "errors": 0,
    }

    # Recover items a previous worker left stuck in 'moving' (crash/restart
    # between the move claim and the final status update). Without this they
    # stay 'moving' forever and never get reconciled.
    try:
        moving_stats = _reconcile_stale_moving()
        stats["imported"] += moving_stats.get("imported", 0)
        stats["moving_reset"] = moving_stats.get("reset", 0)
        stats["moving_skipped"] = moving_stats.get("skipped", 0)
    except Exception as exc:
        logger.warning("[COMPLETE] Stale 'moving' reconciliation failed: %s", exc)

    # One DB-clock snapshot per cycle so every staleness decision below uses
    # the same reference and the check stays correct across app/DB timezones.
    db_now = _db_now_naive()

    try:
        from db.repositories.queue import get_queue_status_counts
        downloading_count = get_queue_status_counts().get("downloading", 0)
        if not downloading_count:
            return stats
        stats["downloading_items"] = downloading_count
    except Exception:
        pass

    try:
        from api_clients.slskd_http import get_slskd_client
        from services.downloads.slskd_service import SlskdService
        from db.repositories.queue import update_queue_item, mark_failed
        from services.downloads.download_scan_service import (
            resolve_downloads_dir,
        )

        slskd_client = get_slskd_client()
        slskd = SlskdService(http_client=slskd_client) if slskd_client is not None else None

        # Walk the downloads ROOT (not the ``Music`` subfolder preference)
        # so files landing anywhere under the configured downloads folder are
        # found — mirrors ``discover_audio_files``, which is the scan that
        # surfaces these files on the monitor/downloads-folder view. Without
        # this, a ``Music`` subfolder under DOWNLOADS_DIR makes the completion
        # walk scan only ``Music`` while slskd downloads sit in the root, so
        # the items are never matched and end up marked failed even though the
        # files are present on disk.
        downloads_dir = resolve_downloads_dir(prefer_music_subfolder=False)

        from helpers.logging_config import log_unified
        log_unified(
            f"[QUEUE] Checking {downloading_count} completed download(s) — downloads dir: {downloads_dir}"
        )

        # 1. Completed transfers from slskd: remote filename -> localFilePath.
        slskd_completed: dict[str, str] = {}
        if slskd is not None:
            try:
                for transfer in slskd.get_completed_transfers():
                    local = transfer.get("localFilePath") or ""
                    remote = transfer.get("filename") or ""
                    if local and os.path.isfile(local):
                        remote_norm = _normalize_transfer_key(remote)
                        if remote_norm:
                            slskd_completed[remote_norm] = local
                            slskd_completed[os.path.basename(remote_norm)] = local
                        slskd_completed[os.path.basename(local).lower()] = local
                logger.debug("[COMPLETE] slskd completed transfers: %s", len(slskd_completed))
            except Exception as exc:
                logger.debug("[COMPLETE] Could not query slskd completed transfers: %s", exc)

        # 2. Filesystem walk (fallback / supplement). We walk the primary
        # downloads directory directly rather than relying on
        # ``discover_audio_files`` (which prefers a ``torrents`` subfolder) so
        # Soulseek downloads landing in the downloads root are always found.
        fs_files: list[Any] = []
        if os.path.isdir(downloads_dir):
            try:
                for root, _, files in os.walk(downloads_dir):
                    for filename in sorted(files):
                        ext = os.path.splitext(filename)[1].lower()
                        if ext not in (".mp3", ".flac", ".m4a", ".ogg", ".wav", ".aac", ".wma", ".opus"):
                            continue
                        full_path = os.path.join(root, filename)
                        fs_files.append({
                            "full_path": full_path,
                            "rel_path": os.path.relpath(full_path, downloads_dir),
                        })
                logger.debug("[COMPLETE] Filesystem walk: %s audio files in %s", len(fs_files), downloads_dir)
            except Exception as exc:
                logger.debug("[COMPLETE] Filesystem walk failed: %s", exc)

        # Index the walk ONCE so per-item matching is dict lookups instead of
        # an O(items × files) re-scan with a metadata read per candidate.
        fs_index = _build_download_index(fs_files) if fs_files else {
            "by_rel_norm": {},
            "by_basename": {},
            "by_token": {},
        }

        # 3. Files already claimed by non-downloading items (avoid reassignment).
        claimed_files: set[str] = set()
        try:
            with db_session() as session:
                rows = session.execute(text("""
                    SELECT found_filename FROM download_queue
                    WHERE found_filename IS NOT NULL
                      AND found_filename <> ''
                      AND status NOT IN ('downloading', 'queued', 'failed', 'searching')
                """)).fetchall() or []
            for row in rows:
                fn = (row[0] or "") if not hasattr(row, "_mapping") else (row._mapping.get("found_filename") or "")
                if fn:
                    norm = _normalize_transfer_key(fn)
                    if norm:
                        claimed_files.add(norm)
                        claimed_files.add(os.path.basename(norm))
        except Exception as exc:
            logger.debug("[COMPLETE] Could not pre-load claimed files: %s", exc)

        # 4. All items stuck in 'downloading'.
        downloading: list[dict] = []
        try:
            with db_session() as session:
                rows = session.execute(text("SELECT * FROM download_queue WHERE status = 'downloading'")).fetchall() or []
                downloading = [dict(r._mapping) for r in rows]
        except Exception as exc:
            logger.error("[COMPLETE] Could not fetch downloading items: %s", exc)
            stats["errors"] += 1
            return stats

        # 4a. Keep the progress column current for in-flight transfers and reuse
        # the same active-transfers snapshot for transfer-state reconciliation
        # so the slskd API is not polled once per unmatched item per cycle.
        active_transfers: list[dict] = []
        if slskd is not None and downloading:
            try:
                active_transfers = slskd.get_active_downloads()
            except Exception as exc:
                logger.debug("[COMPLETE] Could not fetch active transfers: %s", exc)
            if active_transfers:
                _sync_download_progress(active_transfers, downloading)

        for item in downloading:
            try:
                queue_id = item.get("id")

                # Reconciliation: file already in music library but status
                # never flipped to imported (crash between verify and update).
                existing_music = item.get("music_file_path")
                if existing_music and os.path.isfile(existing_music):
                    logger.info("[COMPLETE] Queue %s: already in music library — promoting to imported", queue_id)
                    update_queue_item(
                        queue_id,
                        status="imported",
                        copied_individually=1,
                        copied_individually_at=datetime.now().isoformat(),
                    )
                    stats["imported"] += 1
                    log_unified(f"[QUEUE] {item.get('artist') or ''} - {item.get('title') or ''} → already in library, promoted to imported")
                    _log_queue_event("imported", f"{item.get('artist') or ''} - {item.get('title') or ''} → already in library, promoted to imported", queue_id)
                    continue

                match_found: str | None = None
                match_source = ""
                abs_path: str | None = None

                found_fn = (item.get("found_filename") or "").strip()

                # 1. Exact match via slskd localFilePath (most reliable).
                if found_fn:
                    found_norm = _normalize_transfer_key(found_fn)
                    if found_norm:
                        abs_path = (
                            slskd_completed.get(found_norm)
                            or slskd_completed.get(os.path.basename(found_norm))
                        )
                    if abs_path and os.path.isfile(abs_path):
                        is_match, match_source = _file_matches_queue_item(abs_path, item)
                        if is_match:
                            match_found = os.path.relpath(abs_path, downloads_dir)
                        else:
                            logger.info("[COMPLETE] Queue %s: rejecting slskd-completed file (metadata mismatch): %s", queue_id, abs_path)
                            _delete_mismatched_download(abs_path, queue_id, f"metadata mismatch ({match_source})")
                            mark_failed(queue_id, "Downloaded file did not match queue item; deleted and rescheduled")
                            stats["failed"] += 1
                            log_unified(f"[QUEUE] {item.get('artist') or ''} - {item.get('title') or ''} → failed: downloaded file did not match queue item (deleted + rescheduled)")
                            _log_queue_event("failed", f"{item.get('artist') or ''} - {item.get('title') or ''} → failed: downloaded file did not match queue item (deleted + rescheduled)", queue_id)
                            continue

                # 2. Exact filename match against filesystem files (indexed).
                if match_found is None and found_fn:
                    found_norm = _normalize_transfer_key(found_fn)
                    found_basename = os.path.basename(found_norm) if found_norm else None
                    exact_candidates: list[dict[str, Any]] = []
                    if found_norm:
                        hit = fs_index["by_rel_norm"].get(found_norm)
                        if hit:
                            exact_candidates.append(hit)
                    if found_basename:
                        exact_candidates.extend(fs_index["by_basename"].get(found_basename.lower(), []))

                    seen_paths: set[str] = set()
                    for f in exact_candidates:
                        candidate = f["full_path"]
                        if candidate in seen_paths:
                            continue
                        seen_paths.add(candidate)
                        rel = f["rel_path"].replace("\\", "/")
                        is_match, match_source = _file_matches_queue_item(candidate, item, rel)
                        if not is_match:
                            _delete_mismatched_download(candidate, queue_id, f"metadata mismatch for exact filename match ({match_source})")
                            mark_failed(queue_id, "Downloaded file did not match queue item; deleted and rescheduled")
                            stats["failed"] += 1
                            match_found = "__rejected__"
                            break
                        match_found = f["rel_path"]
                        abs_path = candidate
                        break

                # 3. Fuzzy match against filesystem files (token-indexed).
                #    Skipped when step 2 already rejected + failed this item.
                if match_found is None:
                    for f in _fuzzy_candidates(fs_index, item, fs_files):
                        rel = f["rel_path"].replace("\\", "/")
                        fn_key = _normalize_transfer_key(rel)
                        if fn_key in claimed_files or os.path.basename(fn_key) in claimed_files:
                            continue
                        candidate = f["full_path"]
                        # Metadata check first: True -> accept, False -> reject.
                        try:
                            from services.queue.queue_metadata_matcher import _metadata_matches_queue_item
                            meta_state = _metadata_matches_queue_item(candidate, item)
                        except Exception:
                            meta_state = None

                        if meta_state is False:
                            continue
                        if meta_state is None:
                            # Fall back to filename scoring with a strict bar to
                            # avoid false positives.
                            try:
                                from services.queue.queue_scoring import _score_soulseek_candidate
                                score = _score_soulseek_candidate(rel, item, candidate_duration=_extract_duration_seconds(candidate))
                            except Exception:
                                score = 0.0
                            if score < _SLSKD_MIN_ACCEPT_SCORE:
                                continue

                        match_found = rel
                        abs_path = candidate
                        match_source = "metadata" if meta_state is True else "filename"
                        claimed_files.add(fn_key)
                        claimed_files.add(os.path.basename(fn_key))
                        logger.debug("[COMPLETE] Queue %s: fuzzy match found: %s (score-based)", queue_id, rel)
                        break

                if match_found == "__rejected__":
                    match_found = None

                # 4. No file found — reconcile against live slskd transfers so
                # stale 'downloading' rows do not remain stuck forever.
                if match_found is None or abs_path is None:
                    if slskd is not None:
                        was_failed = _reconcile_transfer_state(item, slskd, active=active_transfers, now=db_now)
                        if was_failed:
                            stats["failed"] += 1
                            continue
                    if _is_stale_queue_item(item, stale_minutes=_STALE_DOWNLOADING_MINUTES, now=db_now):
                        logger.warning("[COMPLETE] Queue %s: no file found and stale in downloading — scheduling retry", queue_id)
                        mark_failed(queue_id, "No file found while marked downloading")
                        stats["failed"] += 1
                        log_unified(f"[QUEUE] {item.get('artist') or ''} - {item.get('title') or ''} → failed: no file found while marked downloading (stale)")
                        _log_queue_event("failed", f"{item.get('artist') or ''} - {item.get('title') or ''} → failed: no file found while marked downloading (stale)", queue_id)
                        continue
                    stats["skipped"] += 1
                    continue

                # ------------------------------------------------------------------
                # Pre-copy duration validation (only when the queue item carries
                # an expected duration) to avoid importing the wrong version.
                # ------------------------------------------------------------------
                expected_dur = item.get("duration")
                if expected_dur:
                    expected_dur = queue_duration_seconds(expected_dur)
                if expected_dur:
                    actual_dur = _extract_duration_seconds(abs_path)
                    if actual_dur and abs(expected_dur - actual_dur) > 20:
                        logger.warning(
                            "[COMPLETE] Queue %s: duration mismatch — expected %ss, file is %ss; deleting and retrying",
                            queue_id, expected_dur, actual_dur,
                        )
                        _delete_mismatched_download(abs_path, queue_id, f"duration mismatch: expected {expected_dur}s, got {actual_dur}s")
                        mark_failed(queue_id, f"Pre-copy duration mismatch: expected {expected_dur}s, got {actual_dur}s")
                        stats["failed"] += 1
                        log_unified(f"[QUEUE] {item.get('artist') or ''} - {item.get('title') or ''} → failed: duration mismatch (expected {expected_dur}s, got {actual_dur}s)")
                        _log_queue_event("failed", f"{item.get('artist') or ''} - {item.get('title') or ''} → failed: duration mismatch (expected {expected_dur}s, got {actual_dur}s)", queue_id)
                        continue

                # Persist found_filename/file_path now that a file is confirmed.
                try:
                    update_queue_item(queue_id, found_filename=match_found, file_path=abs_path)
                except Exception:
                    pass

                move_result = _move_and_import(item, abs_path, match_source)
                if move_result.get("success"):
                    stats["imported"] += 1
                    log_unified(f"[QUEUE] {item.get('artist') or ''} - {item.get('title') or ''} → imported to library (match={match_source})")
                    _log_queue_event("imported", f"{item.get('artist') or ''} - {item.get('title') or ''} → imported to library (match={match_source})", queue_id)
                else:
                    stats["errors"] += 1
                    log_unified(f"[QUEUE] {item.get('artist') or ''} - {item.get('title') or ''} → import failed: {move_result.get('error') or 'unknown error'}")
                    _log_queue_event("failed", f"{item.get('artist') or ''} - {item.get('title') or ''} → import failed: {move_result.get('error') or 'unknown error'}", queue_id)

            except Exception as exc:
                logger.error("[COMPLETE] Unhandled error processing queue %s: %s", item.get("id"), exc, exc_info=True)
                stats["errors"] += 1

        return stats

    except Exception as exc:
        logger.error("[COMPLETE] check_completed_downloads failed: %s", exc, exc_info=True)
        return {"success": False, "error": str(exc), **stats}
