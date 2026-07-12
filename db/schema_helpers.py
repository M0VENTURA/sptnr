"""Schema inspection and schema-aware value helpers."""

from __future__ import annotations
from typing import Any, Iterable

from db.utils import row_get


def table_exists(cursor: Any, table_name: str) -> bool:
    """Check whether a table exists in the current PostgreSQL schema."""
    cursor.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = current_schema()
              AND table_name = %s
        ) AS exists
        """,
        (table_name,),
    )
    row = cursor.fetchone()
    return bool(row_get(row, "exists", 0, False))


def get_table_columns(cursor: Any, table_name: str) -> set[str]:
    """Return column names for a table in the current PostgreSQL schema."""
    cursor.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = %s
        """,
        (table_name,),
    )
    rows = cursor.fetchall() or []
    return {str(row_get(row, "column_name", 0, "")).strip() for row in rows if row_get(row, "column_name", 0)}


def get_postgres_column_types(conn: Any, table_name: str, column_names: Iterable[str]) -> dict[str, str]:
    """Return PostgreSQL data types for requested columns."""
    column_names = list(column_names or [])
    if not column_names:
        return {}
    cursor = conn.cursor()
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
        result: dict[str, str] = {}
        for row in cursor.fetchall() or []:
            column_name = row_get(row, "column_name", 0)
            data_type = row_get(row, "data_type", 1)
            if column_name:
                result[str(column_name)] = str(data_type or "").lower()
        return result
    finally:
        try:
            cursor.close()
        except Exception:
            pass


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

