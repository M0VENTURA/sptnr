"""Schema inspection and schema-aware value helpers."""

from __future__ import annotations
from typing import Any, Iterable

from sqlalchemy import text


def table_exists(session: Any, table_name: str) -> bool:
    """Check whether a table exists in the current PostgreSQL schema."""
    result = session.execute(
        text("""
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = current_schema()
                  AND table_name = :name
            )
        """),
        {"name": table_name},
    )
    row = result.fetchone()
    return bool(row[0]) if row else False


def get_table_columns(session: Any, table_name: str) -> set[str]:
    """Return column names for a table in the current PostgreSQL schema."""
    result = session.execute(
        text("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = :name
        """),
        {"name": table_name},
    )
    rows = result.fetchall() or []
    return {str(row[0]).strip() for row in rows if row[0]}


def get_postgres_column_types(conn_or_session: Any, table_name: str, column_names: Iterable[str]) -> dict[str, str]:
    """Return PostgreSQL data types for requested columns."""
    column_names = list(column_names or [])
    if not column_names:
        return {}
    is_session = hasattr(conn_or_session, 'execute') and hasattr(conn_or_session, 'close')
    
    if is_session:
        session = conn_or_session
        result = session.execute(
            text("""
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = :name
                  AND column_name = ANY(:columns)
            """),
            {"name": table_name, "columns": column_names},
        )
        return {str(row[0]): str(row[1]) for row in result.fetchall()}
    else:
        # Legacy cursor path
        cursor = conn_or_session.cursor()
        try:
            cursor.execute(
                """
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = %s
                  AND column_name = ANY(%s)
                """,
                (table_name, column_names),
            )
            return {str(row[0]): str(row[1]) for row in cursor.fetchall()}
        finally:
            cursor.close()


def normalize_track_flag_payload(conn: Any, flag_values: dict[str, Any]) -> dict[str, Any]:
    """Coerce track flag values based on actual DB column types.

    PostgreSQL BOOLEAN columns receive True/False. Other columns receive 0/1.
    """
    if not flag_values:
        return {}
    column_types = get_postgres_column_types(conn, "tracks", flag_values.keys())
    normalized: dict[str, Any] = {}
    for column_name, raw_value in flag_values.items():
        bool_value = bool(raw_value)
        normalized[column_name] = bool_value if column_types.get(column_name) == "boolean" else int(bool_value)
    return normalized

