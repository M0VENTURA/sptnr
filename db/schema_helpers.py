"""Schema inspection and schema-aware value helpers."""

from __future__ import annotations

from typing import Any, Iterable

from sqlalchemy import Connection, text
from sqlalchemy.orm import Session


def table_exists(conn_or_session: Session | Connection, table_name: str) -> bool:
    """Check whether a table exists as the app would resolve it.

    Uses ``to_regclass(:name)`` which resolves the bare name through the
    connection's ``search_path`` — the EXACT same resolution ``FROM tracks``
    uses.  The previous ``table_schema = current_schema()`` check only looked
    at the connection's default schema, so when ``tracks`` lived in a schema
    earlier on ``search_path`` (e.g. ``popularr`` before ``public``) the
    helper reported the table missing, the bootstrap skipped its column-ensure
    (and the search self-heal skipped its ALTER), and queries kept failing
    with ``column album_artist does not exist``.
    """
    result = conn_or_session.execute(
        text("SELECT to_regclass(:name)"),
        {"name": table_name},
    )
    row = result.fetchone()
    return bool(row and row[0])


def get_table_columns(conn_or_session: Session | Connection, table_name: str) -> set[str]:
    """Return column names for a table, resolved via ``search_path``.

    Mirrors :func:`table_exists`: resolves the bare name through
    ``to_regclass`` so the columns are read from whatever schema the queries
    actually use.  Reads ``pg_attribute`` on the relation resolved by
    ``to_regclass`` (not ``current_schema()``).

    The filter compares ``a.attrelid`` (the relation OID) directly against
    ``to_regclass(:name)`` rather than comparing ``nspname || '.' || relname``
    to ``to_regclass(:name)::text``: ``regclass::text`` output OMITS the
    schema qualifier when the object is visible through the current
    ``search_path`` (e.g. ``to_regclass('tracks')::text`` → ``tracks`` on the
    default ``"$user", public`` path), so the old text comparison never
    matched and this helper silently returned an EMPTY column set on any
    Postgres database with a default search path — making /api/search return
    the graceful "run a Navidrome import" empty result while the artists /
    albums / tracks browse pages (which query ``FROM tracks`` directly) kept
    working.
    """
    rows = conn_or_session.execute(
        text("""
            SELECT a.attname
            FROM pg_attribute a
            WHERE a.attrelid = to_regclass(:name)
              AND a.attnum > 0
              AND NOT a.attisdropped
            ORDER BY a.attnum
        """),
        {"name": table_name},
    ).fetchall()
    return {str(r[0]).strip() for r in rows if r[0]}


def get_postgres_column_types(
    conn_or_session: Session | Connection, 
    table_name: str, 
    column_names: Iterable[str]
) -> dict[str, str]:
    """Return PostgreSQL data types for requested columns."""
    column_names_list = list(column_names or [])
    if not column_names_list:
        return {}

    result = conn_or_session.execute(
        text("""
            SELECT a.attname, format_type(a.atttypid, a.atttypmod)
            FROM pg_attribute a
            WHERE a.attrelid = to_regclass(:name)
              AND a.attnum > 0
              AND NOT a.attisdropped
              AND a.attname = ANY(:columns)
        """),
        {"name": table_name, "columns": column_names_list},
    )
    return {str(row[0]): str(row[1]) for row in result.fetchall()}


def normalize_track_flag_payload(
    conn_or_session: Session | Connection, 
    flag_values: dict[str, Any]
) -> dict[str, Any]:
    """Coerce track flag values based on actual DB column types.

    PostgreSQL BOOLEAN columns receive True/False. Other columns receive 0/1.
    """
    if not flag_values:
        return {}
        
    column_types = get_postgres_column_types(conn_or_session, "tracks", flag_values.keys())
    normalized: dict[str, Any] = {}
    
    for column_name, raw_value in flag_values.items():
        bool_value = bool(raw_value)
        if column_types.get(column_name) == "boolean":
            normalized[column_name] = bool_value
        else:
            normalized[column_name] = int(bool_value)
            
    return normalized
