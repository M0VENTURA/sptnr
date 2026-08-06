"""
Metadata conflict detection and resolution service (Shadow Table Pattern).

Provides a "gatekeeper" that prevents background scans from silently
overwriting curated local metadata.  When an external provider (MusicBrainz,
Last.fm, etc.) reports a value that differs from the local canonical value
for a protected field, the conflict is recorded in the ``metadata_conflicts``
shadow table instead of being applied automatically.

Key Functions:
    - ``detect_and_record_conflicts()`` — Compare incoming provider data
      against local values and record any protected-field mismatches.
    - ``fetch_pending_conflicts()`` — Return unresolved conflicts for the UI.
    - ``resolve_conflict()`` — Apply a user's decision and mark resolved.
    - ``resolve_conflicts_batch()`` — Bulk-resolve all conflicts for a track.
    - ``get_conflict_stats()`` — Aggregate counts for the corrections page.

Architecture:
    The ``metadata_conflicts`` table is the single source of truth for
    pending human review.  The corrections page queries it alongside the
    existing album-level inconsistency data from ``correction_service.py``.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import text

from db.engine import db_session

logger = logging.getLogger(__name__)


def _conflicts_table_available() -> bool:
    """True when the ``metadata_conflicts`` table exists.

    Older databases predate the table; the corrections page must render
    (empty conflict sections) instead of 500ing on every query.
    """
    try:
        from db.schema_helpers import table_exists
        with db_session() as session:
            return bool(table_exists(session, "metadata_conflicts"))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Protected fields — never auto-overwrite by an external provider
# ---------------------------------------------------------------------------

PROTECTED_FIELDS: frozenset[str] = frozenset({
    "genre",
    "genres",
    "manual_genres",
    "year",
    "release_year",
    "album",
    "album_artist",
    "artist",
    "title",
    "track_number",
    "disc_number",
    "mood",
    "mbid",
    "musicbrainz_album_mbid",
    "musicbrainz_artistid",
    "musicbrainz_albumartistid",
    "musicbrainz_releasegroupid",
})

# Fields that are safe to auto-update without human review
SAFE_FIELDS: frozenset[str] = frozenset({
    "last_scanned",
    "spotify_score",
    "lastfm_score",
    "listenbrainz_score",
    "age_score",
    "final_score",
    "stars",
    "popularity",
    "popularity_frozen",
    "is_single",
    "single_confidence",
    "duration",
    "file_path",
})


# ---------------------------------------------------------------------------
# Gatekeeper
# ---------------------------------------------------------------------------

def detect_and_record_conflicts(
    track_id: str,
    provider: str,
    local_data: dict[str, Any],
    remote_data: dict[str, Any],
    *,
    artist_name: str | None = None,
    album_name: str | None = None,
    track_title: str | None = None,
) -> dict[str, Any]:
    """Compare incoming provider data against local values and record conflicts.

    This is the central "gatekeeper" function.  Call it from any scan or
    import pipeline *before* writing remote data to the canonical tracks table.

    Args:
        track_id: The canonical track ID.
        provider: Source identifier (``"musicbrainz"``, ``"lastfm"``, etc.).
        local_data: Current values from the ``tracks`` table.
        remote_data: Incoming values from the external provider.
        artist_name, album_name, track_title: Optional display context for the UI.

    Returns:
        Dict with keys ``conflicts_recorded`` (int), ``safe_updates`` (list),
        and ``blocked_fields`` (list).
    """
    conflicts: list[dict[str, Any]] = []
    safe_updates: list[str] = []
    blocked_fields: list[str] = []

    for field, remote_value in remote_data.items():
        if remote_value is None:
            continue

        local_value = local_data.get(field)

        # If values match, nothing to do
        if str(local_value or "") == str(remote_value or ""):
            continue

        # If local is empty and remote has data, it's safe to fill in
        if not local_value or str(local_value).strip() == "":
            safe_updates.append(field)
            continue

        # If field is protected, record a conflict
        if field in PROTECTED_FIELDS:
            conflicts.append({
                "track_id": track_id,
                "provider": provider,
                "field_name": field,
                "local_value": str(local_value),
                "remote_value": str(remote_value),
                "artist_name": artist_name or "",
                "album_name": album_name or "",
                "track_title": track_title or "",
            })
            blocked_fields.append(field)
        elif field not in SAFE_FIELDS:
            # Unknown field — also protect by default
            conflicts.append({
                "track_id": track_id,
                "provider": provider,
                "field_name": field,
                "local_value": str(local_value),
                "remote_value": str(remote_value),
                "artist_name": artist_name or "",
                "album_name": album_name or "",
                "track_title": track_title or "",
            })
            blocked_fields.append(field)
        else:
            # Safe field — allow auto-update
            safe_updates.append(field)

    recorded = 0
    if conflicts:
        recorded = _insert_conflicts_batch(conflicts)

    return {
        "conflicts_recorded": recorded,
        "safe_updates": safe_updates,
        "blocked_fields": blocked_fields,
    }


def _insert_conflicts_batch(conflicts: list[dict[str, Any]]) -> int:
    """Upsert conflicts into the shadow table.

    Uses ``ON CONFLICT DO NOTHING`` so the first-recorded conflict is
    preserved and repeat scans don't spam duplicate rows.
    """
    if not conflicts:
        return 0

    count = 0
    with db_session() as session:
        for c in conflicts:
            try:
                session.execute(
                    text("""
                        INSERT INTO metadata_conflicts
                            (track_id, provider, field_name,
                             local_value, remote_value,
                             artist_name, album_name, track_title,
                             status, created_at)
                        VALUES
                            (:track_id, :provider, :field_name,
                             :local_value, :remote_value,
                             :artist_name, :album_name, :track_title,
                             'pending', CURRENT_TIMESTAMP)
                        ON CONFLICT (track_id, provider, field_name)
                        DO UPDATE SET
                            remote_value = EXCLUDED.remote_value,
                            local_value = EXCLUDED.local_value
                        WHERE metadata_conflicts.status = 'pending'
                    """),
                    {
                        "track_id": c["track_id"],
                        "provider": c["provider"],
                        "field_name": c["field_name"],
                        "local_value": c["local_value"],
                        "remote_value": c["remote_value"],
                        "artist_name": c.get("artist_name", ""),
                        "album_name": c.get("album_name", ""),
                        "track_title": c.get("track_title", ""),
                    },
                )
                count += 1
            except Exception as exc:
                logger.warning(
                    "Failed to record conflict for %s/%s: %s",
                    c.get("track_id"), c.get("field_name"), exc,
                )
    return count


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------

def fetch_pending_conflicts(
    *,
    limit: int = 100,
    offset: int = 0,
    provider: str | None = None,
    artist: str | None = None,
) -> list[dict[str, Any]]:
    """Return unresolved conflicts, newest first."""
    if not _conflicts_table_available():
        return []
    clauses: list[str] = ["status = 'pending'"]
    params: dict[str, Any] = {"limit": limit, "offset": offset}

    if provider:
        clauses.append("provider = :provider")
        params["provider"] = provider
    if artist:
        clauses.append("artist_name ILIKE :artist")
        params["artist"] = f"%{artist}%"

    where = " AND ".join(clauses)

    with db_session() as session:
        result = session.execute(
            text(f"""
                SELECT id, track_id, provider, field_name,
                       local_value, remote_value,
                       artist_name, album_name, track_title,
                       status, created_at
                FROM metadata_conflicts
                WHERE {where}
                ORDER BY created_at DESC
                LIMIT :limit OFFSET :offset
            """),
            params,
        )
        return [dict(row._mapping) for row in result.fetchall() or []]


def count_pending_conflicts(
    provider: str | None = None,
    artist: str | None = None,
) -> int:
    """Return the number of unresolved conflicts."""
    if not _conflicts_table_available():
        return 0
    clauses: list[str] = ["status = 'pending'"]
    params: dict[str, Any] = {}

    if provider:
        clauses.append("provider = :provider")
        params["provider"] = provider
    if artist:
        clauses.append("artist_name ILIKE :artist")
        params["artist"] = f"%{artist}%"

    where = " AND ".join(clauses)

    with db_session() as session:
        result = session.execute(
            text(f"SELECT COUNT(*) FROM metadata_conflicts WHERE {where}"),
            params,
        )
        row = result.fetchone()
        return int(row[0]) if row else 0


def get_conflict_stats() -> dict[str, Any]:
    """Return aggregate stats about pending conflicts for the corrections page."""
    if not _conflicts_table_available():
        return {"total_pending": 0, "by_provider": [], "by_field": []}
    with db_session() as session:
        total = session.execute(
            text("SELECT COUNT(*) FROM metadata_conflicts WHERE status = 'pending'")
        ).scalar() or 0

        by_provider = session.execute(
            text("""
                SELECT provider, COUNT(*)
                FROM metadata_conflicts
                WHERE status = 'pending'
                GROUP BY provider
                ORDER BY COUNT(*) DESC
            """)
        ).fetchall() or []

        by_field = session.execute(
            text("""
                SELECT field_name, COUNT(*)
                FROM metadata_conflicts
                WHERE status = 'pending'
                GROUP BY field_name
                ORDER BY COUNT(*) DESC
            """)
        ).fetchall() or []

    return {
        "total_pending": int(total),
        "by_provider": [dict(row._mapping) for row in by_provider],
        "by_field": [dict(row._mapping) for row in by_field],
    }


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

def resolve_conflict(
    conflict_id: int,
    *,
    accepted_value: str | None = None,
    resolved_by: str = "webui",
) -> dict[str, Any]:
    """Mark a single conflict as resolved and optionally apply the accepted value.

    If ``accepted_value`` is provided, the canonical ``tracks`` table is
    updated within the same transaction (atomic commit).
    """
    with db_session() as session:
        # Fetch the conflict row
        result = session.execute(
            text("SELECT * FROM metadata_conflicts WHERE id = :id"),
            {"id": conflict_id},
        )
        row = result.fetchone()
        if not row:
            return {"success": False, "error": "Conflict not found"}

        conflict = dict(row._mapping)

        if accepted_value is not None:
            # Apply the accepted value to the tracks table
            field = conflict["field_name"]
            session.execute(
                text(f"UPDATE tracks SET {field} = :value WHERE id = :track_id"),
                {"value": accepted_value, "track_id": conflict["track_id"]},
            )

        # Mark resolved
        session.execute(
            text("""
                UPDATE metadata_conflicts
                SET status = 'resolved',
                    resolved_at = CURRENT_TIMESTAMP,
                    resolved_by = :resolved_by
                WHERE id = :id
            """),
            {"id": conflict_id, "resolved_by": resolved_by},
        )

    return {
        "success": True,
        "conflict_id": conflict_id,
        "track_id": conflict["track_id"],
        "field": conflict["field_name"],
        "applied_value": accepted_value,
    }


def resolve_conflicts_batch(
    track_id: str,
    resolutions: dict[str, str],
    *,
    resolved_by: str = "webui",
) -> dict[str, Any]:
    """Resolve all pending conflicts for a track by applying user-chosen values.

    All updates happen in a single database transaction.

    Args:
        track_id: The canonical track ID.
        resolutions: Mapping of ``{field_name: accepted_value}``.
        resolved_by: Source identifier for the audit trail.

    Returns:
        Dict with ``success``, ``resolved_count``, and ``updated_fields``.
    """
    updated_fields: list[str] = []
    resolved_count = 0

    with db_session() as session:
        for field, accepted_value in resolutions.items():
            if not field:
                continue

            # Update the canonical track
            session.execute(
                text(f"UPDATE tracks SET {field} = :value WHERE id = :track_id"),
                {"value": accepted_value, "track_id": track_id},
            )

            # Mark matching conflicts as resolved
            result = session.execute(
                text("""
                    UPDATE metadata_conflicts
                    SET status = 'resolved',
                        resolved_at = CURRENT_TIMESTAMP,
                        resolved_by = :resolved_by
                    WHERE track_id = :track_id
                      AND field_name = :field
                      AND status = 'pending'
                    RETURNING id
                """),
                {
                    "track_id": track_id,
                    "field": field,
                    "resolved_by": resolved_by,
                },
            )
            resolved_count += result.rowcount or 0
            updated_fields.append(field)

    return {
        "success": True,
        "resolved_count": resolved_count,
        "updated_fields": updated_fields,
    }


def ignore_conflict(conflict_id: int, *, resolved_by: str = "webui") -> dict[str, Any]:
    """Mark a conflict as ignored (keep local value, don't apply remote)."""
    with db_session() as session:
        session.execute(
            text("""
                UPDATE metadata_conflicts
                SET status = 'ignored',
                    resolved_at = CURRENT_TIMESTAMP,
                    resolved_by = :resolved_by
                WHERE id = :id
            """),
            {"id": conflict_id, "resolved_by": resolved_by},
        )
    return {"success": True, "conflict_id": conflict_id, "status": "ignored"}
