"""
Repository helpers for queue database operations.

✅ ALL database access is centralized here.
✅ Services must call this repository — no direct SQL elsewhere.
"""

from __future__ import annotations

import logging
import json
import os
from typing import Any, Dict, List, Optional
from datetime import datetime
from sqlalchemy import text

from db.engine import db_session
from db.utils import row_get

from helpers.config_helpers import get_config
from services.downloads.download_scan_service import resolve_downloads_dir
from helpers.normalization_service import normalize_match_text

from services.queue.queue_constraints import (
    COMPLETED_QUEUE_STATUSES,
)

logger = logging.getLogger(__name__)

# Columns callers are allowed to write via ``update_queue_item()``. Whitelisted
# so API payloads can never inject arbitrary column names into the SET clause.
UPDATE_ALLOWED_COLUMNS = frozenset({
    # Core identity / metadata
    "artist", "title", "album", "album_artist", "source", "priority",
    "track_number", "disc_number", "year", "duration",
    # MusicBrainz linkage
    "release_id", "release_source", "release_mbid", "recording_mbid",
    "release_year", "cover_art_url",
    # Grouping
    "import_group", "import_type",
    # File / path tracking
    "file_path", "matched_file_path", "music_file_path", "found_filename",
    "progress", "speed",
    # Retry / backoff
    "retry_count", "max_retries", "retry_delay_minutes", "next_retry_at",
    "failure_reason",
    # Scheduling
    "release_date",
    # State
    "status",
    # Library linkage (manual match)
    "collection_track_id", "in_collection",
    # Copy tracking (legacy)
    "copied_individually", "copied_individually_at",
})


# =============================================================================
# CORE GETTERS
# =============================================================================

def _row_to_dict(
    row,
    cursor,
) -> dict[str, Any]:
    if hasattr(row, "keys"):
        return dict(row)
    return dict(zip([c[0] for c in cursor.description], row))


def get_queue_item(queue_id: int) -> Optional[Dict[str, Any]]:
    try:
        with db_session() as session:
            result = session.execute(
                text("SELECT * FROM download_queue WHERE id = :id"),
                {"id": queue_id},
            )
            row = result.fetchone()
            return dict(row._mapping) if row else None
    except Exception as e:
        logger.error(f"Failed to fetch queue item {queue_id}: {e}")
        return None


def get_completed_group_queue_items(import_group: str) -> List[Dict[str, Any]]:
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT id, file_path, artist, album, title, track_number, disc_number, album_artist, year
                    FROM download_queue
                    WHERE import_group = :group AND status = 'completed'
                    ORDER BY id
                """),
                {"group": import_group},
            )
            rows = result.fetchall()
            return [dict(r._mapping) for r in rows]
    except Exception as exc:
        logger.error(f"[get_completed_group_queue_items] {exc}")
        return []


def item_has_status(queue_id: int, expected_status: str) -> bool:
    try:
        with db_session() as session:
            result = session.execute(
                text("SELECT status FROM download_queue WHERE id = :id"),
                {"id": queue_id},
            )
            row = result.fetchone()
            return str(row[0]).lower() == str(expected_status).lower() if row else False
    except Exception as e:
        logger.error(f"Failed to check status for queue item {queue_id}: {e}")
        return False


def get_queue_file_path(queue_id: int) -> Optional[str]:
    try:
        with db_session() as session:
            result = session.execute(
                text("SELECT file_path FROM download_queue WHERE id=:id"),
                {"id": queue_id},
            )
            row = result.fetchone()
            return row[0] if row else None
    except Exception as e:
        logger.error(f"[get_queue_file_path] {e}")
        return None


# =============================================================================
# CORE MUTATIONS
# =============================================================================

def insert_queue_item(
    artist: str,
    title: str,
    album: str | None = None,
    source: str = "soulseek",
    priority: int = 5,
    **kwargs,
) -> dict[str, Any]:
    """Insert a new item into the download queue.

    Performs duplicate detection before inserting: if an active item with the
    same artist + title + source already exists, returns that item with
    ``already_queued=True`` instead of creating a duplicate.

    Args:
        artist: Artist name.
        title: Track title.
        album: Optional album name.
        source: Download source (default ``"soulseek"``).
        priority: Queue priority (1-10, default 5).
        **kwargs: Additional fields (track_number, disc_number, album_artist,
                  year, release_id, release_mbid, recording_mbid, duration, etc.).

    Returns:
        The inserted row as a dict, or the existing row with ``already_queued`` set.
    """
    # Ghost-row guard: rows without artist AND title are invisible to users
    # (they render as "Unknown - Unknown") and are never searchable — reject
    # them at the shared insert path.
    if not (artist or "").strip() and not (title or "").strip():
        return {"success": False, "error": "Artist and title are required"}

    # ── Duplicate detection ────────────────────────────────────────────────
    with db_session() as session:
        existing = session.execute(
            text("""
                SELECT * FROM download_queue
                WHERE LOWER(artist) = LOWER(:artist)
                  AND LOWER(title) = LOWER(:title)
                  AND source = :source
                  AND status IN ('queued', 'searching', 'downloading', 'completed', 'unmatched', 'imported', 'in_collection')
                ORDER BY created_at ASC
                LIMIT 1
            """),
            {"artist": artist, "title": title, "source": source},
        ).fetchone()

        if existing is not None:
            row = dict(existing._mapping)
            row["already_queued"] = True
            logger.info(
                "Duplicate skipped: %s - %s already in queue (ID %s)",
                artist, title, row.get("id"),
            )
            return row

        # ── Insert ──
        result = session.execute(
            text("""
                INSERT INTO download_queue
                    (artist, title, album, source, priority, track_number, disc_number,
                     album_artist, year, release_id, release_mbid, recording_mbid,
                     duration, import_group, import_type, status, created_at, updated_at)
                VALUES (:artist, :title, :album, :source, :priority, :track_number, :disc_number,
                        :album_artist, :year, :release_id, :release_mbid, :recording_mbid,
                        :duration, :import_group, :import_type, 'queued', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                RETURNING id
            """),
            {
                "artist": artist, "title": title, "album": album, "source": source,
                "priority": priority,
                "track_number": kwargs.get("track_number"),
                "disc_number": kwargs.get("disc_number"),
                "album_artist": kwargs.get("album_artist"),
                "year": kwargs.get("year"),
                "release_id": kwargs.get("release_id"),
                "release_mbid": kwargs.get("release_mbid"),
                "recording_mbid": kwargs.get("recording_mbid"),
                "duration": kwargs.get("duration"),
                "import_group": kwargs.get("import_group"),
                "import_type": kwargs.get("import_type") or "song",
            },
        )
        new_id = result.scalar()

    return get_queue_item(new_id) or {}


def update_queue_item(queue_id: int, **kwargs) -> Optional[Dict[str, Any]]:
    if not kwargs:
        return get_queue_item(queue_id)

    # Whitelist columns so API payloads can't inject arbitrary column names
    # into the SET clause (see UPDATE_ALLOWED_COLUMNS above).
    updates = {k: v for k, v in kwargs.items() if k in UPDATE_ALLOWED_COLUMNS}
    if not updates:
        logger.warning("[update_queue_item] no whitelisted columns for queue %s", queue_id)
        return get_queue_item(queue_id)

    set_clauses = []
    params = {}

    for key, value in updates.items():
        set_clauses.append(f"{key} = :{key}")
        params[key] = json.dumps(value) if isinstance(value, (dict, list)) else value

    # ``copied_individually`` is BOOLEAN in the schema but legacy completion
    # flows pass 1/0 (int) — coerce centrally so the UPDATE never trips
    # psycopg2 DatatypeMismatch.
    if "copied_individually" in params:
        params["copied_individually"] = bool(params["copied_individually"])

    params["id"] = queue_id

    query = text(f"""
        UPDATE download_queue
        SET {', '.join(set_clauses)}, updated_at = CURRENT_TIMESTAMP
        WHERE id = :id
        RETURNING *
    """)

    try:
        with db_session() as session:
            result = session.execute(query, params)
            row = result.fetchone()
            return dict(row._mapping) if row else None
    except Exception as e:
        logger.error(f"[update_queue_item] {e}")
        return None


def claim_queue_item(
    queue_id: int,
    status: str = "searching",
) -> Optional[Dict[str, Any]]:
    """Atomically claim a queued item for processing.

    The guarded UPDATE only wins if the item is still ``queued`` (or a due
    ``backed_off``/``pending_release`` scheduled item) and past any retry
    backoff, so concurrent workers can never double-claim an item.
    Used by the orchestrator's ``_claim_item`` chain (``process_next_batch``).
    """
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    UPDATE download_queue
                    SET status = :status, updated_at = CURRENT_TIMESTAMP
                    WHERE id = :qid
                      AND file_path IS NULL
                      AND status IN ('queued', 'backed_off', 'pending_release')
                      AND (next_retry_at IS NULL OR next_retry_at <= CURRENT_TIMESTAMP)
                    RETURNING *
                """),
                {"qid": queue_id, "status": status},
            )
            row = result.fetchone()
            return dict(row._mapping) if row else None
    except Exception as e:
        logger.error(f"[claim_queue_item] {e}")
        return None


def delete_queue_item(queue_id: int) -> bool:
    try:
        with db_session() as session:
            result = session.execute(
                text("DELETE FROM download_queue WHERE id=:id"),
                {"id": queue_id},
            )
            return result.rowcount > 0
    except Exception as e:
        logger.error(f"[delete_queue_item] {e}")
        return False


def get_queue_item_by_path(path: str) -> Optional[Dict[str, Any]]:
    """Find the most recently updated queue item owning ``path``.

    Matches any of the path columns (``file_path``, ``found_filename``,
    ``music_file_path``).  Used by the ``process-one`` endpoint.
    """
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT * FROM download_queue
                    WHERE file_path = :p
                       OR found_filename = :p
                       OR music_file_path = :p
                    ORDER BY updated_at DESC
                    LIMIT 1
                """),
                {"p": path},
            )
            row = result.fetchone()
            return dict(row._mapping) if row else None
    except Exception as e:
        logger.error(f"[get_queue_item_by_path] {e}")
        return None


# =============================================================================
# GROUP / MATCHING HELPERS
# =============================================================================

def get_items_by_group(group_id: str) -> List[Dict[str, Any]]:
    try:
        with db_session() as session:
            result = session.execute(
                text("SELECT * FROM download_queue WHERE import_group = :group"),
                {"group": group_id},
            )
            return [dict(r._mapping) for r in result.fetchall()]
    except Exception as e:
        logger.error(f"[get_items_by_group] {e}")
        return []


def apply_release_mbid(queue_ids: list, mbid: str, artist: str, album: str) -> int:
    if not queue_ids:
        return 0

    try:
        with db_session() as session:
            placeholders = ",".join([f":id_{i}" for i in range(len(queue_ids))])
            params = {"mbid": mbid, "artist": artist, "album": album}
            params.update({f"id_{i}": qid for i, qid in enumerate(queue_ids)})
            result = session.execute(
                text(f"""
                    UPDATE download_queue
                    SET release_mbid = :mbid,
                        album_artist = :artist,
                        album = :album
                    WHERE id IN ({placeholders})
                """),
                params,
            )
            return result.rowcount
    except Exception as e:
        logger.error(f"[apply_release_mbid] {e}")
        return 0


# =============================================================================
# STATUS / PROCESSING
# =============================================================================


def get_queue_status_counts() -> Dict[str, int]:
    try:
        with db_session() as session:
            result = session.execute(
                text("SELECT status, COUNT(*) FROM download_queue GROUP BY status")
            )
            return {str(r[0]): int(r[1]) for r in result.fetchall()}
    except Exception as e:
        logger.error(f"[get_queue_status_counts] {e}")
        return {}


def get_active_queue(limit: int = 200) -> List[Dict[str, Any]]:
    """Get non-terminal queue items (active statuses + failed).

    Active statuses come from the canonical ``ACTIVE_QUEUE_STATUSES`` set so
    the list always matches the status counts.  Failed items are included
    because the queue page splits them out of the active list client-side
    (mirrors the legacy API payload).

    Strict queue vs. local-disk boundary: rows whose ``source`` is
    ``local``/``discovered`` represent ambient disk folders picked up by the
    watcher/discovery flow — they are PASSIVE (the Matched Folders section)
    and are always excluded here, regardless of status, so local disk folders
    never bleed into the active search/download queue.
    """
    from services.queue.queue_constraints import ACTIVE_QUEUE_STATUSES, FAILED_STATUSES
    # Inline the statuses rather than binding a list parameter — psycopg2
    # cannot adapt a Python list for ``ANY(:statuses)`` and the resulting
    # exception was silently swallowed, returning an empty list.
    status_sql = ", ".join(f"'{s}'" for s in sorted(ACTIVE_QUEUE_STATUSES | FAILED_STATUSES))
    try:
        with db_session() as session:
            result = session.execute(
                text(f"""
                    SELECT *
                    FROM download_queue
                    WHERE status IN ({status_sql})
                      AND LOWER(COALESCE(source, '')) NOT IN ('local', 'discovered')
                    ORDER BY created_at ASC,
                             -- Album tracks are queued in one batch (same
                             -- created_at) — keep them in track-number order
                             -- so the queue list under an album folder shows
                             -- the album's tracks in order instead of the
                             -- arbitrary insert sequence.
                             CASE WHEN NULLIF(TRIM(COALESCE(track_number, '')), '') ~ '^\\d+$'
                                  THEN TRIM(track_number)::integer ELSE 9999 END,
                             id ASC
                    LIMIT :limit
                """),
                {"limit": limit},
            )
            return [dict(r._mapping) for r in result.fetchall()]
    except Exception as e:
        logger.error(f"[get_active_queue] {e}")
        return []


def get_processing_snapshot() -> Dict[str, int]:
    counts = get_queue_status_counts()
    return {
        "queued": counts.get("queued", 0),
        "failed": counts.get("failed", 0),
        "completed": counts.get("completed", 0),
        "total": sum(counts.values())
    }


def get_ready_for_processing(limit: int = 100) -> List[Dict]:
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT *
                    FROM download_queue
                    WHERE file_path IS NULL
                      AND (status = 'queued'
                           OR (status IN ('backed_off', 'pending_release')
                               AND next_retry_at <= CURRENT_TIMESTAMP))
                      AND (next_retry_at IS NULL OR next_retry_at <= CURRENT_TIMESTAMP)
                    ORDER BY priority DESC, created_at ASC
                    LIMIT :limit
                """),
                {"limit": limit},
            )
            return [dict(r._mapping) for r in result.fetchall()]
    except Exception as e:
        logger.error(f"[get_ready_for_processing] {e}")
        return []


def requeue_due_failed_items(limit: int = 50) -> List[Dict[str, Any]]:
    """Requeue failed items whose retry window has arrived.

    Eligible items: ``status = 'failed'`` with (``next_retry_at`` unset or
    due).  Each requeued item gets ``retry_count + 1`` and
    ``next_retry_at = now + retry_delay_minutes`` so repeated failures back
    off (legacy retry-scheduler parity).

    No item is ever left permanently stuck in ``failed``: unlike earlier
    behaviour that stopped at ``max_retries``, every failed item keeps
    flowing back into the queue once its retry window arrives.  Items not
    yet due remain ``failed`` (i.e. "pending") until ``next_retry_at``, then
    are requeued automatically.  The configured ``queue.retry_delay_minutes``
    (default 30) governs how long an item sits pending between attempts.
    """
    requeued: List[Dict[str, Any]] = []
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT *
                    FROM download_queue
                    WHERE status = 'failed'
                      AND file_path IS NULL
                      AND (next_retry_at IS NULL OR next_retry_at <= CURRENT_TIMESTAMP)
                    ORDER BY updated_at ASC
                    LIMIT :limit
                """),
                {"limit": limit},
            )
            rows = [dict(r._mapping) for r in result.fetchall()]
            _delay_default = _queue_retry_defaults()[0]
            for row in rows:
                qid = row.get("id")
                delay_minutes = max(1, int(row.get("retry_delay_minutes") or _delay_default))
                session.execute(
                    text("""
                        UPDATE download_queue
                        SET status = 'queued',
                            retry_count = retry_count + 1,
                            failure_reason = NULL,
                            next_retry_at = CURRENT_TIMESTAMP + (:delay * INTERVAL '1 minute'),
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = :qid
                    """),
                    {"qid": qid, "delay": delay_minutes},
                )
                requeued.append(row)
        return requeued
    except Exception as exc:
        logger.error("[requeue_due_failed_items] %s", exc)
        return []


def get_failed_queue(limit: int = 100) -> List[Dict[str, Any]]:
    """Return failed items, newest first, for API / requeue endpoints."""
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT *
                    FROM download_queue
                    WHERE status = 'failed'
                    ORDER BY updated_at DESC
                    LIMIT :limit
                """),
                {"limit": limit},
            )
            return [dict(r._mapping) for r in result.fetchall()]
    except Exception as exc:
        logger.error("[get_failed_queue] %s", exc)
        return []


def requeue_queue_item(queue_id: int) -> Optional[Dict[str, Any]]:
    """Manually requeue a failed/removed/cancelled item, resetting retry state.

    Clears ``next_retry_at`` and ``retry_count`` so the item is picked up
    immediately by the next worker cycle (unlike a plain status bump, which
    would leave the backoff window in place).

    Items that already have a local ``file_path`` are NOT requeued for
    Soulseek — they return to ``unmatched`` so they only go through the
    matching/alignment flow, never a P2P re-download.
    """
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    UPDATE download_queue
                    SET status = CASE
                            WHEN file_path IS NOT NULL THEN 'unmatched'
                            ELSE 'queued'
                        END,
                        retry_count = 0,
                        failure_reason = NULL,
                        next_retry_at = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = :qid
                      AND status IN ('failed', 'removed', 'cancelled')
                    RETURNING *
                """),
                {"qid": queue_id},
            )
            row = result.fetchone()
            return dict(row._mapping) if row else None
    except Exception as exc:
        logger.error("[requeue_queue_item] %s", exc)
        return None
    
    # =============================================================================
# COMPLETED / POST-DOWNLOAD QUEUE
# =============================================================================

def get_completed_queue(limit: int = 50) -> List[Dict[str, Any]]:
    """
    Get completed downloads (and unmatched items) ready for post-processing.

    Includes:
    - completed
    - unmatched
    - possible_duplicate
    - moving
    """
    # Inline the statuses — psycopg2 cannot adapt a Python list for
    # ``ANY(:statuses)``; the exception was silently swallowed and the
    # completed list always rendered empty.
    status_sql = ", ".join(f"'{s}'" for s in sorted(COMPLETED_QUEUE_STATUSES))
    try:
        with db_session() as session:
            result = session.execute(
                text(f"""
                    SELECT *
                    FROM download_queue
                    WHERE status IN ({status_sql})
                    ORDER BY updated_at DESC
                    LIMIT :limit
                """),
                {"limit": limit},
            )

            return [dict(r._mapping) for r in result.fetchall()]

    except Exception as e:
        logger.error(f"[get_completed_queue] {e}")
        return []

# =============================================================================
# GROUP QUERIES
# =============================================================================

    
# queue.py
def get_completed_by_group(group_id: str) -> List[Dict[str, Any]]:
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT id, file_path, artist, album, title,
                           track_number, disc_number, album_artist, year
                    FROM download_queue
                    WHERE import_group = :group
                    AND status = 'completed'
                """),
                {"group": group_id},
            )

            return [dict(r._mapping) for r in result.fetchall()]

    except Exception as e:
        logger.error(f"[get_completed_by_group] {e}")
        return []


# =============================================================================
# STATUS HELPERS (STATE TRANSITIONS)
# =============================================================================

def schedule_queue_retry(
    queue_id: int,
    status: str,
    next_retry_at: str,
    reason: str = "",
) -> Optional[Dict[str, Any]]:
    """Park an item until a future retry time (backed_off / pending_release).

    Sets the scheduled status + ``next_retry_at`` and bumps ``retry_count``
    so the worker only picks it up once the timestamp is due.
    """
    try:
        with db_session() as session:
            session.execute(
                text("""
                    UPDATE download_queue
                    SET status = :status,
                        next_retry_at = :next_retry,
                        failure_reason = :reason,
                        retry_count = retry_count + 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = :qid
                """),
                {"qid": queue_id, "status": status, "next_retry": next_retry_at, "reason": reason},
            )
        return {"success": True, "id": queue_id, "status": status}
    except Exception as exc:
        logger.error("[schedule_queue_retry] %s", exc)
        return None


def _queue_retry_defaults() -> tuple[int, int]:
    """Return ``(failure_retry_delay_minutes, max_retries)`` from ``queue.*`` config."""
    try:
        from helpers.config_helpers import get_config
        q = (get_config() or {}).get("queue") or {}
        delay = max(1, int(q.get("failure_retry_delay_minutes", 30) or 30))
        max_retries = max(1, int(q.get("max_retries", 5) or 5))
        return delay, max_retries
    except Exception:
        return 30, 5


def mark_failed(queue_id: int, reason: str) -> Optional[Dict[str, Any]]:
    """Mark a queue item failed with a reason.

    Retryable failures return to ``queued`` with ``next_retry_at`` refreshed
    to now + retry delay — the worker only picks the item up once the retry
    window passes, so it reads as queued in the UI instead of sitting in a
    dead ``failed`` state.  Items that have exhausted ``max_retries`` stay
    ``failed`` for manual retry.
    """
    delay, max_retries = _queue_retry_defaults()
    try:
        with db_session() as session:
            session.execute(
                text("""
                    UPDATE download_queue
                    SET status = CASE
                            WHEN retry_count + 1 >= COALESCE(max_retries, :max_retries) THEN 'failed'
                            ELSE 'queued'
                        END,
                        failure_reason = :reason,
                        retry_count = retry_count + 1,
                        next_retry_at = CURRENT_TIMESTAMP
                            + (COALESCE(retry_delay_minutes, :delay) * INTERVAL '1 minute'),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = :qid
                """),
                {"qid": queue_id, "reason": reason, "delay": delay, "max_retries": max_retries},
            )
        return {"success": True, "id": queue_id}
    except Exception as exc:
        logger.error("[mark_failed] %s", exc)
        return None

def mark_imported(queue_id: int, target_path: str) -> Optional[Dict[str, Any]]:
    return update_queue_item(
        queue_id,
        status="imported",
        music_file_path=target_path,
        copied_individually=1,
        copied_individually_at=datetime.now().isoformat()
    )


def mark_processing(queue_id: int):
    return update_queue_item(queue_id, status="processing")



def mark_completed(queue_id: int):
    return update_queue_item(queue_id, status="completed")

# =============================================================================
# MATCH TARGETS / REMATCH HELPERS
# =============================================================================

def get_queue_match_targets(
    artist: str,
    album: str,
    selected_queue_id: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    Return queue items that can be used as manual rematch targets.
    """

    active_statuses = (
        'queued', 'searching', 'downloading',
        'matched', 'completed',
        'unmatched', 'queried',
        'discovered', 'pending_match',
        'possible_duplicate', 'duplicate'
    )

    try:
        with db_session() as session:

            status_placeholders = ", ".join([f":status_{i}" for i in range(len(active_statuses))])
            params = {f"status_{i}": s for i, s in enumerate(active_statuses)}
            params["artist"] = artist
            params["album"] = album
            params["selected_queue_id"] = selected_queue_id

            result = session.execute(
                text(f"""
                SELECT
                    id,
                    artist,
                    title,
                    album,
                    status,
                    track_number,
                    release_mbid,
                    found_filename,
                    created_at
                FROM download_queue
                WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)
                  AND LOWER(COALESCE(NULLIF(album, ''), '')) = LOWER(:album)
                  AND status IN ({status_placeholders})
                ORDER BY
                    CASE
                        WHEN :selected_queue_id IS NOT NULL AND id = :selected_queue_id THEN 0
                        ELSE 1
                    END,
                    CASE
                        WHEN NULLIF(TRIM(COALESCE(track_number, '')), '') ~ '^\\d+$'
                            THEN TRIM(track_number)::integer
                        ELSE 9999
                    END,
                    COALESCE(NULLIF(TRIM(track_number), ''), '9999'),
                    id
                LIMIT 250
                """),
                params,
            )

            return [
                {
                    "id": r[0],
                    "artist": r[1] or '',
                    "title": r[2] or '',
                    "album": r[3] or '',
                    "status": r[4] or '',
                    "track_number": r[5],
                    "release_mbid": r[6] or '',
                    "found_filename": r[7] or '',
                    "created_at": r[8],
                }
                for r in result.fetchall() or []
            ]

    except Exception as e:
        logger.error(f"[get_queue_match_targets] {e}")
        return []

def get_album_queue_tracks(
    artist: str,
    album: str,
) -> list[dict[str, Any]]:
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT id, title, file_path, status, track_number,
                           disc_number, artist, album_artist
                    FROM download_queue
                    WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)
                      AND LOWER(COALESCE(album, '')) = LOWER(:album)
                    ORDER BY
                        CASE WHEN NULLIF(TRIM(COALESCE(track_number, '')), '') ~ '^\d+$'
                            THEN TRIM(track_number)::integer ELSE 9999 END,
                        title
                """),
                {"artist": artist, "album": album},
            )
            return [dict(r._mapping) for r in result.fetchall()]
    except Exception as e:
        logger.error(f"[get_album_queue_tracks] {e}")
        return []




# =============================================================================
# BACKWARD-COMPATIBLE REDIRECTS
# (functions moved to db.repositories.queue_admin)
# =============================================================================
from db.repositories.queue_admin import (
    clear_queue, purge_all, get_imported, cleanup,
    cleanup_copied_sources, cleanup_orphaned,
    count_pending_by_release, verify_and_prune,
    apply_release_mbid,
    find_existing_discovered_file, find_duplicate_queue_item,
    insert_discovered_file, delete_duplicate_queue_entries,
    mark_in_collection, get_active_queue_signatures,
    get_queue_items_by_folder, slskd_eligibility_diagnostics,
    delete_folder, remove_group, reset_moving,
)
