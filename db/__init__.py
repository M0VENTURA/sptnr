"""Database package for Popularr.

This package is the single home for database connection helpers, schema
bootstrap, table/schema inspection helpers, cleanup routines, repository
query modules, and the SQLAlchemy ORM layer.
"""

from db.bootstrap import ensure_full_schema, init_database_and_schema, verify_all_tables_exist
from db.context import db_cursor
from db.utils import get_db_connection, is_postgres_connection

# SQLAlchemy ORM — new code should prefer these
from db.engine import (
    Base,
    db_session,
    get_db,
    close_db,
    get_engine,
    get_session_factory,
    run_migrations_on_startup,
)

from db.models import (
    AlbumArt,
    Artist,
    ArtistStat,
    Bookmark,
    DownloadQueue,
    GenreUpdate,
    MissingRelease,
    MusicbrainzRelease,
    MusicbrainzReleaseTrack,
    ScanHistory,
    SlskdSearchLog,
    Track,
)

__all__ = [
    # Legacy helpers
    "ensure_full_schema",
    "init_database_and_schema",
    "verify_all_tables_exist",
    "db_cursor",
    "get_db_connection",
    "is_postgres_connection",
    # SQLAlchemy session
    "Base",
    "db_session",
    "get_db",
    "close_db",
    "get_engine",
    "get_session_factory",
    # ORM models
    "AlbumArt",
    "Artist",
    "ArtistStat",
    "Bookmark",
    "DownloadQueue",
    "GenreUpdate",
    "MissingRelease",
    "MusicbrainzRelease",
    "MusicbrainzReleaseTrack",
    "ScanHistory",
    "SlskdSearchLog",
    "Track",
]
