"""Popularity persistence repository."""

from __future__ import annotations
import logging
import time
from typing import Dict

from sqlalchemy import text

from db.engine import db_session

logger = logging.getLogger(__name__)

_TRACKS_COLUMN_CACHE: set[str] | None = None
_TRACKS_COLUMN_TYPES_CACHE: Dict[str, str] | None = None

PG_INT_TYPES = {"smallint", "integer", "bigint"}
PG_FLOAT_TYPES = {"real", "double precision", "numeric", "decimal"}
PG_BOOL_TYPES = {"boolean"}

DB_LOCK_MAX_RETRIES = 5
DB_LOCK_BASE_DELAY_SECONDS = 0.25


def get_tracks_table_columns(session=None) -> set[str]:
    global _TRACKS_COLUMN_CACHE
    if _TRACKS_COLUMN_CACHE:
        return _TRACKS_COLUMN_CACHE

    own_session = session is None
    if own_session:
        from db.engine import db_session as _db_session
        with _db_session() as s:
            return _do_get_tracks_table_columns(s)
    return _do_get_tracks_table_columns(session)


def _do_get_tracks_table_columns(session) -> set[str]:
    global _TRACKS_COLUMN_CACHE
    dialect = (session.get_bind().dialect.name if hasattr(session, "get_bind") else "").lower()
    if dialect == "sqlite":
        result = session.execute(text("PRAGMA table_info(tracks)"))
        _TRACKS_COLUMN_CACHE = {
            str(row[1]) for row in result.fetchall() or []
        }
        return _TRACKS_COLUMN_CACHE
    result = session.execute(
        text("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'tracks'
        """)
    )
    _TRACKS_COLUMN_CACHE = {
        str(row[0])
        for row in result.fetchall() or []
    }
    return _TRACKS_COLUMN_CACHE


def get_tracks_table_column_types(session=None) -> Dict[str, str]:
    global _TRACKS_COLUMN_TYPES_CACHE
    if _TRACKS_COLUMN_TYPES_CACHE:
        return _TRACKS_COLUMN_TYPES_CACHE

    own_session = session is None
    if own_session:
        from db.engine import db_session as _db_session
        with _db_session() as s:
            return _do_get_tracks_table_column_types(s)
    return _do_get_tracks_table_column_types(session)


def _do_get_tracks_table_column_types(session) -> Dict[str, str]:
    global _TRACKS_COLUMN_TYPES_CACHE
    dialect = (session.get_bind().dialect.name if hasattr(session, "get_bind") else "").lower()
    if dialect == "sqlite":
        result = session.execute(text("PRAGMA table_info(tracks)"))
        _TRACKS_COLUMN_TYPES_CACHE = {
            str(row[1]): str(row[2])
            for row in result.fetchall() or []
        }
        return _TRACKS_COLUMN_TYPES_CACHE
    result = session.execute(
        text("""
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_name = 'tracks'
        """)
    )
    _TRACKS_COLUMN_TYPES_CACHE = {
        str(row[0]): str(row[1])
        for row in result.fetchall() or []
    }
    return _TRACKS_COLUMN_TYPES_CACHE


def coerce_track_value_for_pg_type(column: str, value, pg_type: str):
    if value is None:
        return None

    pg_type = (pg_type or "").lower()

    if pg_type in PG_BOOL_TYPES:
        return bool(value)
    if pg_type in PG_INT_TYPES:
        try:
            return int(value) if value != "" else None
        except (TypeError, ValueError):
            return None
    if pg_type in PG_FLOAT_TYPES:
        try:
            return float(value) if value != "" else None
        except (TypeError, ValueError):
            return None

    return value


def _is_transient_db_error(exc: Exception) -> bool:
    """Return True for connection-level DB errors worth retrying.

    PostgreSQL can drop pooled connections (restart, idle timeout, network
    blip).  ``pool_pre_ping`` only helps on fresh checkouts; a session already
    holding a stale connection fails at commit with ``OperationalError``.
    """
    try:
        from sqlalchemy.exc import OperationalError as SAOperationalError
        from sqlalchemy.exc import InterfaceError as SAInterfaceError
        if isinstance(exc, (SAOperationalError, SAInterfaceError)):
            return True
    except Exception:
        pass
    orig = getattr(exc, "orig", None)
    if orig is not None:
        cls_name = type(orig).__name__.lower()
        if any(k in cls_name for k in ("operationalerror", "interfaceerror", "connectionerror")):
            return True
    return False


def run_with_db_lock_retry(operation):
    """Run ``operation`` with retries for lock contention AND transient DB errors."""
    for attempt in range(DB_LOCK_MAX_RETRIES):
        try:
            return operation()
        except Exception as exc:
            transient = _is_transient_db_error(exc) or "lock" in str(exc).lower()
            if not transient or attempt >= DB_LOCK_MAX_RETRIES - 1:
                raise
            # Drop pooled connections so the next attempt uses fresh ones.
            try:
                from db.engine import get_engine
                get_engine().dispose()
            except Exception:
                pass
            time.sleep(DB_LOCK_BASE_DELAY_SECONDS * (attempt + 1))
            continue
    raise RuntimeError("retry loop exhausted")


def save_to_db(track_data: dict) -> bool:
    """Save or update a track in the database.

    Uses db_session for safe connection handling.

    When the payload carries ``_navidrome_sync=True``, popularity/scoring
    columns are excluded from the UPDATE clause so that an incremental
    metadata sync never overwrites scores computed by the popularity pipeline.

    Args:
        track_data: Dictionary of track data to save
        conn: Optional existing connection (deprecated, kept for compatibility)

    Returns:
        True if successfully saved, False otherwise
    """
    if not track_data:
        return False

    def operation():
        with db_session() as session:
            return _execute_save(session, track_data)

    return run_with_db_lock_retry(operation)


def upsert_tracks_bulk(track_payloads: list[dict]) -> bool:
    """Persist many track payloads in ONE session + commit.

    The per-track scan workers open a fresh ``db_session`` (one transaction,
    one commit) per track — tens of thousands of transactions for a full
    library scan.  A bulk upsert runs the whole batch (typically one album)
    through one session so the writes commit once per album instead of once
    per row.  Rows that fail validation are logged and skipped so a single
    malformed payload never aborts the album's remaining writes.

    Args:
        track_payloads: List of track payload dicts (each must include ``id``).

    Returns:
        True when every row saved, False if any row failed.
    """
    if not track_payloads:
        return False

    def operation():
        with db_session() as session:
            ok = True
            for payload in track_payloads:
                try:
                    _execute_save(session, payload)
                except Exception as exc:
                    ok = False
                    logger.debug(
                        "Bulk track upsert skipped for %s: %s",
                        payload.get("id"), exc,
                    )
            return ok

    return run_with_db_lock_retry(operation)


# ── Columns whose values are owned by the popularity pipeline and must
#    NEVER be overwritten by a Navidrome metadata sync.  The UPSERT UPDATE
#    clause skips these columns when ``_navidrome_sync`` is set.
_POPULARITY_PROTECTED_COLUMNS: frozenset[str] = frozenset({
    # Scoring
    "popularity_score", "score", "final_score",
    "spotify_score", "lastfm_score", "listenbrainz_score", "age_score",
    "combined_score",
    # Ratings
    "stars", "star_rating",
    # Single detection
    "is_single", "single_confidence", "single_sources",
    "single_detection_last_updated", "single_manual_override",
    # Genres / tags (owned by the popularity pipeline's enrichment pass)
    "spotify_genres", "lastfm_tags", "listenbrainz_genres",
    "discogs_genres", "musicbrainz_genres", "audiodb_genres", "essentia_genres",
    # Album type (owned by the album stage's enrichment pass — a Navidrome
    # sync reads albumtype/releasetype from file tags, which are usually
    # empty, and would otherwise wipe the type the scan just detected)
    "musicbrainz_albumtype", "spotify_album_type", "releasetype",
    # Popularity meta
    "spotify_popularity", "lastfm_ratio", "lastfm_track_playcount",
    "popularity_frozen",
})


def _execute_save(session, track_data: dict) -> bool:
    """Execute the actual save operation with schema validation.

    When ``_navidrome_sync`` is True in the payload, popularity/scoring
    columns are excluded from the UPDATE clause of the upsert so that
    Navidrome metadata syncs never clobber the math engine's results.
    """
    columns = get_tracks_table_columns(session)
    types = get_tracks_table_column_types(session)

    is_navidrome_sync = bool(track_data.get("_navidrome_sync"))

    data = {
        k: coerce_track_value_for_pg_type(k, v, types.get(k, ""))
        for k, v in track_data.items()
        if k in columns and k != "_navidrome_sync"
    }

    if "id" not in data:
        raise ValueError("track must include id")

    keys = list(data.keys())

    named_placeholders = ", ".join([f":{k}" for k in keys])

    # Build the UPDATE SET clause, skipping popularity-protected columns
    # when this is a Navidrome metadata sync.
    update_parts = []
    for k in keys:
        if k == "id":
            continue
        if is_navidrome_sync and k in _POPULARITY_PROTECTED_COLUMNS:
            continue
        update_parts.append(f"{k}=EXCLUDED.{k}")

    update_set = ", ".join(update_parts)

    query = text(f"""
        INSERT INTO tracks ({', '.join(keys)})
        VALUES ({named_placeholders})
        ON CONFLICT (id)
        DO UPDATE SET {update_set}
    """)

    params = {k: data[k] for k in keys}
    session.execute(query, params)

    return True