"""Database package for Popularr/SPTNR.

This package is the single home for database connection helpers, schema
bootstrap, table/schema inspection helpers, cleanup routines, and repository
query modules.
"""

from db.bootstrap import ensure_full_schema, init_database_and_schema, verify_all_tables_exist
from db.context import db_cursor
from db.utils import get_db_connection, is_postgres_connection

__all__ = [
    "ensure_full_schema",
    "init_database_and_schema",
    "verify_all_tables_exist",
    "db_cursor",
    "get_db_connection",
    "is_postgres_connection",
]
