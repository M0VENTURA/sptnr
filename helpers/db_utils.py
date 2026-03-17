import sqlite3
import os
import logging
import time

DB_PATH = os.environ.get("DB_PATH", "/database/sptnr.db")
_pg_last_failure_monotonic = 0.0
_PG_FAILURE_BACKOFF_SECONDS = float(os.environ.get("PG_FAILURE_BACKOFF_SECONDS", "30"))


def is_postgres_configured() -> bool:
    """Return True when PostgreSQL connection settings are configured."""
    pg_dsn = (os.environ.get("DATABASE_URL") or os.environ.get("PG_DSN") or "").strip()
    pg_host = (os.environ.get("PG_HOST") or "").strip()
    pg_user = (os.environ.get("PG_USER") or "").strip()
    pg_database = (os.environ.get("PG_DATABASE") or "").strip()
    return bool(pg_dsn or (pg_host and pg_user and pg_database))

def _is_postgres_connection(conn):
    """Detect if connection is PostgreSQL."""
    try:
        import psycopg2
        return isinstance(conn, psycopg2.extensions.connection)
    except (ImportError, AttributeError):
        return False


def _row_first_value(row, default=None):
    """Return the first value from sqlite tuple/Row or psycopg2 RealDictRow."""
    if row is None:
        return default
    if isinstance(row, dict):
        for value in row.values():
            return value
        return default
    try:
        return row[0]
    except (KeyError, IndexError, TypeError):
        return default


def _table_exists(cursor, table_name, is_pg):
    """Check whether a table exists using the current backend's system catalog."""
    if is_pg:
        cursor.execute(
            "SELECT COUNT(*) AS count FROM information_schema.tables "
            "WHERE table_name = %s AND table_schema = current_schema()",
            (table_name,)
        )
        return (_row_first_value(cursor.fetchone(), 0) or 0) > 0

    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
    return bool(cursor.fetchone())


def _get_table_columns(cursor, table_name, is_pg):
    """Return a set of column names for a table."""
    if is_pg:
        cursor.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = %s AND table_schema = current_schema()",
            (table_name,)
        )
        return {str(_row_first_value(row, "")) for row in cursor.fetchall() if _row_first_value(row, "")}

    cursor.execute(f"PRAGMA table_info({table_name})")
    columns = set()
    for row in cursor.fetchall():
        if isinstance(row, dict):
            column_name = row.get("name")
        else:
            column_name = row[1] if len(row) > 1 else None
        if column_name:
            columns.add(column_name)
    return columns

def get_db_connection():
    """
    Get database connection.
    Supports both PostgreSQL (if configured) and SQLite fallback.
    """
    # Try PostgreSQL if configured via DSN or individual PG_* vars (same as app.py get_db())
    pg_dsn = os.environ.get("DATABASE_URL") or os.environ.get("PG_DSN")
    pg_host = os.environ.get("PG_HOST", "")
    pg_user = os.environ.get("PG_USER", "")
    pg_database = os.environ.get("PG_DATABASE", "sptnr")

    if is_postgres_configured():
        global _pg_last_failure_monotonic
        now = time.monotonic()
        if _pg_last_failure_monotonic > 0:
            elapsed = now - _pg_last_failure_monotonic
            if elapsed < _PG_FAILURE_BACKOFF_SECONDS:
                remaining = int(_PG_FAILURE_BACKOFF_SECONDS - elapsed)
                raise RuntimeError(
                    "PostgreSQL is configured but recent connection failures are in backoff "
                    f"for another ~{remaining}s"
                )
        try:
            import psycopg2
            import psycopg2.extras
            if pg_dsn:
                conn = psycopg2.connect(
                    pg_dsn,
                    cursor_factory=psycopg2.extras.RealDictCursor,
                    connect_timeout=10,
                )
                logging.debug(f"Connected to PostgreSQL: {pg_dsn.split('@')[1] if '@' in pg_dsn else 'configured'}")
            else:
                conn = psycopg2.connect(
                    host=pg_host,
                    port=int(os.environ.get("PG_PORT", "5432")),
                    user=pg_user,
                    password=os.environ.get("PG_PASSWORD", ""),
                    dbname=pg_database,
                    cursor_factory=psycopg2.extras.RealDictCursor,
                    connect_timeout=10,
                )
                logging.debug(f"Connected to PostgreSQL: {pg_host}/{pg_database}")
            _pg_last_failure_monotonic = 0.0
            return conn
        except ImportError as e:
            raise RuntimeError(
                "PostgreSQL is configured but psycopg2 is not installed. "
                "Install psycopg2-binary to continue."
            ) from e
        except Exception as e:
            _pg_last_failure_monotonic = time.monotonic()
            raise RuntimeError(
                f"PostgreSQL is configured but connection failed: {e}"
            ) from e
    
    # Fall back to SQLite
    # Ensure database directory exists
    db_dir = os.path.dirname(DB_PATH)
    if db_dir and not os.path.exists(db_dir):
        try:
            os.makedirs(db_dir, exist_ok=True)
        except (PermissionError, OSError) as e:
            # If we can't create the directory, the sqlite3.connect will fail with a more specific error
            # Log the warning but let the connection attempt proceed
            logging.warning(f"Could not create database directory {db_dir}: {e}")
    
    conn = sqlite3.connect(DB_PATH, timeout=120.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def ensure_album_artist_column():
    """Ensure the album_artist column exists in the tracks table AND populate it with artist data.
    
    This is called on app startup to automatically migrate the database
    if needed, without requiring manual intervention.
    """
    import logging
    import sys
    
    try:
        db_path = os.environ.get("DB_PATH", "/database/sptnr.db")
        logging.debug(f"Checking album_artist migration for database: {db_path}")
        
        conn = get_db_connection()
        cursor = conn.cursor()
        is_pg = _is_postgres_connection(conn)
        
        # Check if tracks table exists
        if not _table_exists(cursor, "tracks", is_pg):
            # Table doesn't exist yet, nothing to migrate
            logging.warning("Tracks table does not exist yet, skipping album_artist migration")
            conn.close()
            return False
        
        # Check if album_artist column exists
        columns = _get_table_columns(cursor, "tracks", is_pg)
        
        if 'album_artist' not in columns:
            # Add the album_artist column
            logging.info("Creating album_artist column...")
            try:
                cursor.execute("ALTER TABLE tracks ADD COLUMN album_artist TEXT")
                conn.commit()
                logging.info("✓ Successfully added album_artist column to tracks table")
            except sqlite3.OperationalError as e:
                if "duplicate column name" not in str(e).lower():
                    logging.error(f"✗ Failed to add album_artist column: {e}")
                    conn.close()
                    raise
        
        # Populate album_artist with artist data where it's NULL.
        # For PostgreSQL, guard with an advisory lock so only one worker
        # performs the migration at a time and use small SKIP LOCKED batches
        # to avoid long-running row lock chains.
        logging.debug("Populating album_artist column from artist data...")
        try:
            if is_pg:
                lock_key = 915317411  # Stable app-specific advisory lock key
                cursor.execute("SELECT pg_try_advisory_lock(%s) AS acquired", (lock_key,))
                lock_row = cursor.fetchone()
                lock_acquired = bool(lock_row.get("acquired")) if isinstance(lock_row, dict) else bool(lock_row[0])

                if not lock_acquired:
                    logging.debug("Another worker is already running album_artist migration; skipping this run")
                    conn.close()
                    return True

                total_rows_updated = 0
                batch_size = 500
                try:
                    while True:
                        cursor.execute(
                            """
                            WITH to_update AS (
                                SELECT id
                                FROM tracks
                                WHERE album_artist IS NULL
                                ORDER BY id
                                FOR UPDATE SKIP LOCKED
                                LIMIT %s
                            )
                            UPDATE tracks t
                            SET album_artist = t.artist
                            FROM to_update u
                            WHERE t.id = u.id
                            """,
                            (batch_size,)
                        )
                        batch_updated = cursor.rowcount or 0
                        conn.commit()
                        total_rows_updated += batch_updated

                        if batch_updated == 0:
                            break

                    if total_rows_updated > 0:
                        logging.info(f"✓ Populated album_artist for {total_rows_updated} rows")
                    else:
                        logging.debug("✓ Populated album_artist for 0 rows")
                finally:
                    try:
                        cursor.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))
                        conn.commit()
                    except Exception:
                        pass
            else:
                cursor.execute("UPDATE tracks SET album_artist = artist WHERE album_artist IS NULL")
                rows_updated = cursor.rowcount
                conn.commit()
                if rows_updated > 0:
                    logging.info(f"✓ Populated album_artist for {rows_updated} rows")
                else:
                    logging.debug("✓ Populated album_artist for 0 rows")
        except Exception as e:
            logging.error(f"✗ Failed to populate album_artist column: {e}")
            conn.close()
            raise
        
        logging.debug("✓ album_artist migration complete")
        conn.close()
        return True
        
    except RuntimeError as e:
        logging.warning(f"⚠ Skipping album_artist migration: {e}")
        return False
    except Exception as e:
        logging.error(f"✗ Error ensuring album_artist column exists: {e}", exc_info=True)
        # Don't fail app startup, but log the error
        return False


def ensure_musicbrainz_album_mbid_column():
    """Ensure tracks table uses `musicbrainz_album_mbid` instead of legacy `beets_album_mbid`.

    Migration behavior:
    - If only `beets_album_mbid` exists: rename it to `musicbrainz_album_mbid`.
    - If both exist: backfill missing values in the new column from the legacy column.
    - If neither exists: add `musicbrainz_album_mbid`.
    """
    import logging

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        is_pg = _is_postgres_connection(conn)

        if not _table_exists(cursor, "tracks", is_pg):
            logging.warning("Tracks table does not exist yet, skipping MBID column migration")
            conn.close()
            return False

        columns = _get_table_columns(cursor, "tracks", is_pg)

        has_legacy = "beets_album_mbid" in columns
        has_new = "musicbrainz_album_mbid" in columns

        if has_legacy and not has_new:
            logging.info("Renaming tracks.beets_album_mbid -> tracks.musicbrainz_album_mbid")
            try:
                cursor.execute(
                    "ALTER TABLE tracks RENAME COLUMN beets_album_mbid TO musicbrainz_album_mbid"
                )
                conn.commit()
                logging.info("✓ Renamed beets_album_mbid to musicbrainz_album_mbid")
            except Exception as rename_error:
                logging.warning(f"Rename failed (may already be done): {rename_error}")
                # Try to verify the new column exists, if not add it
                columns_after = _get_table_columns(cursor, "tracks", is_pg)
                if "musicbrainz_album_mbid" not in columns_after:
                    logging.info("New column doesn't exist; adding it instead")
                    cursor.execute("ALTER TABLE tracks ADD COLUMN musicbrainz_album_mbid TEXT")
                    conn.commit()
                    logging.info("✓ Added musicbrainz_album_mbid column")
        elif has_legacy and has_new:
            # Guard with an advisory lock so concurrent workers don't deadlock on the
            # full-table backfill UPDATE (same pattern as ensure_album_artist_column).
            if is_pg:
                mbid_lock_key = 915317412  # Stable app-specific key (album_artist uses 915317411)
                cursor.execute("SELECT pg_try_advisory_lock(%s) AS acquired", (mbid_lock_key,))
                lock_row = cursor.fetchone()
                lock_acquired = bool(lock_row.get("acquired")) if isinstance(lock_row, dict) else bool(lock_row[0])
                if not lock_acquired:
                    logging.info("Another worker is already running musicbrainz_album_mbid backfill; skipping")
                    conn.close()
                    return True
                try:
                    total_updated = 0
                    batch_size = 500
                    while True:
                        cursor.execute(
                            """
                            WITH to_update AS (
                                SELECT id
                                FROM tracks
                                WHERE (musicbrainz_album_mbid IS NULL OR musicbrainz_album_mbid = '')
                                  AND beets_album_mbid IS NOT NULL
                                  AND beets_album_mbid != ''
                                ORDER BY id
                                FOR UPDATE SKIP LOCKED
                                LIMIT %s
                            )
                            UPDATE tracks t
                            SET musicbrainz_album_mbid = t.beets_album_mbid
                            FROM to_update u
                            WHERE t.id = u.id
                            """,
                            (batch_size,)
                        )
                        batch_updated = cursor.rowcount or 0
                        conn.commit()
                        total_updated += batch_updated
                        if batch_updated == 0:
                            break
                    logging.info(f"✓ Backfilled musicbrainz_album_mbid from legacy beets_album_mbid ({total_updated} rows)")
                except Exception as backfill_error:
                    logging.warning(f"Backfill failed: {backfill_error}")
                finally:
                    try:
                        cursor.execute("SELECT pg_advisory_unlock(%s)", (mbid_lock_key,))
                        conn.commit()
                    except Exception:
                        pass
            else:
                try:
                    cursor.execute(
                        """
                        UPDATE tracks
                        SET musicbrainz_album_mbid = beets_album_mbid
                        WHERE (musicbrainz_album_mbid IS NULL OR musicbrainz_album_mbid = '')
                          AND beets_album_mbid IS NOT NULL
                          AND beets_album_mbid != ''
                        """
                    )
                    conn.commit()
                    logging.info("✓ Backfilled musicbrainz_album_mbid from legacy beets_album_mbid")
                except Exception as backfill_error:
                    logging.warning(f"Backfill failed: {backfill_error}")
        elif not has_new:
            logging.info("Adding missing musicbrainz_album_mbid column")
            try:
                cursor.execute("ALTER TABLE tracks ADD COLUMN musicbrainz_album_mbid TEXT")
                conn.commit()
                logging.info("✓ Added missing musicbrainz_album_mbid column")
            except Exception as add_error:
                logging.warning(f"Add column failed (may already exist): {add_error}")

        conn.close()
        return True
    except RuntimeError as e:
        logging.warning(f"⚠ Skipping musicbrainz_album_mbid migration: {e}")
        return False
    except Exception as e:
        logging.error(f"Error ensuring musicbrainz_album_mbid column exists: {e}", exc_info=True)
        return False


def verify_album_artist_column():
    """Verify that the album_artist column exists and is functional.
    
    Returns:
        dict: Status information with 'exists' boolean and 'message' string
    """
    import logging
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        is_pg = _is_postgres_connection(conn)
        
        # Check if tracks table exists
        if not _table_exists(cursor, "tracks", is_pg):
            conn.close()
            return {"exists": False, "message": "Tracks table does not exist"}
        
        # Check if album_artist column exists
        columns = _get_table_columns(cursor, "tracks", is_pg)
        
        conn.close()
        
        if 'album_artist' in columns:
            return {"exists": True, "message": "album_artist column exists and is functional"}
        else:
            return {"exists": False, "message": "album_artist column does NOT exist - migration failed or not run"}
    except Exception as e:
        return {"exists": False, "message": f"Error verifying column: {e}"}


def get_current_track_rating(track_id: str) -> int:
    """
    Query the current rating for a track from the database.
    
    Args:
        track_id: Track ID to query
        
    Returns:
        Star rating (0-5), or 0 if not found
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        is_pg = _is_postgres_connection(conn)
        placeholder = "%s" if is_pg else "?"
        cursor.execute(f"SELECT stars FROM tracks WHERE id = {placeholder}", (track_id,))
        row = cursor.fetchone()
        conn.close()
        value = _row_first_value(row, 0)
        return int(value) if value is not None else 0
    except Exception as e:
        import logging
        logging.debug(f"Failed to get current rating for track {track_id}: {e}")
        return 0


def ensure_writer_column():
    """Ensure the writer column exists in the tracks table for storing lyricist/songwriter info.
    
    This is called on app startup to automatically add the writer column
    if it doesn't exist, allowing Navidrome lyricist data to be stored.
    Supports both SQLite and PostgreSQL.
    """
    import logging
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        is_pg = _is_postgres_connection(conn)
        
        # Check if tracks table exists (database-agnostic)
        table_exists = _table_exists(cursor, "tracks", is_pg)
        if not table_exists:
            logging.warning("Tracks table does not exist yet, skipping writer column migration")
            conn.close()
            return False
        
        # Check if writer column exists (database-agnostic)
        columns = _get_table_columns(cursor, "tracks", is_pg)
        writer_exists = 'writer' in columns
        
        if not writer_exists:
            # Add the writer column
            logging.info("Creating writer column for lyricist/songwriter data...")
            try:
                cursor.execute("ALTER TABLE tracks ADD COLUMN writer TEXT")
                conn.commit()
                logging.info("✓ Successfully added writer column to tracks table")
            except Exception as e:
                err_msg = str(e).lower()
                if "duplicate column" in err_msg or "already exists" in err_msg:
                    logging.info("✓ Writer column already exists")
                else:
                    logging.error(f"✗ Failed to add writer column: {e}")
                    conn.close()
                    raise
        else:
            logging.debug("✓ Writer column already exists in tracks table")
        
        conn.close()
        return True
        
    except RuntimeError as e:
        logging.warning(f"⚠ Skipping writer column migration: {e}")
        return False
    except Exception as e:
        logging.error(f"✗ Error ensuring writer column exists: {e}", exc_info=True)
        # Don't fail app startup, but log the error
        return False


def ensure_cover_columns():
    """Ensure the cover-related columns exist in the tracks table.

    Adds the following columns if missing:
        - is_cover     BOOLEAN DEFAULT 0   – marks track as a cover
        - is_cover_reason  TEXT            – human-readable detection reason
        - original_cover_artist  TEXT      – name of the original/earliest artist

    Supports both SQLite and PostgreSQL.
    """
    import logging

    columns_to_add = [
        ("is_cover", "BOOLEAN DEFAULT 0"),
        ("is_cover_reason", "TEXT"),
        ("original_cover_artist", "TEXT"),
    ]

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        is_pg = _is_postgres_connection(conn)

        # Check if tracks table exists
        table_exists = _table_exists(cursor, "tracks", is_pg)
        if not table_exists:
            logging.warning("Tracks table does not exist yet, skipping cover columns migration")
            conn.close()
            return False

        # Determine existing columns
        existing = _get_table_columns(cursor, "tracks", is_pg)

        for col_name, col_def in columns_to_add:
            if col_name not in existing:
                logging.info(f"Adding '{col_name}' column to tracks table...")
                try:
                    cursor.execute(f"ALTER TABLE tracks ADD COLUMN {col_name} {col_def}")
                    conn.commit()
                    logging.info(f"✓ Added '{col_name}' column to tracks table")
                except Exception as e:
                    err_msg = str(e).lower()
                    if "duplicate column" in err_msg or "already exists" in err_msg:
                        logging.info(f"✓ Column '{col_name}' already exists")
                    else:
                        logging.error(f"✗ Failed to add '{col_name}' column: {e}")
            else:
                logging.debug(f"✓ Column '{col_name}' already exists in tracks table")

        conn.close()
        return True

    except RuntimeError as e:
        logging.warning(f"⚠ Skipping cover columns migration: {e}")
        return False
    except Exception as e:
        logging.error(f"✗ Error ensuring cover columns exist: {e}", exc_info=True)
        return False


def ensure_track_release_year_column():
    """Ensure the optional release_year column exists in the tracks table."""
    import logging

    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        is_pg = _is_postgres_connection(conn)

        if not _table_exists(cursor, "tracks", is_pg):
            logging.warning("Tracks table does not exist yet, skipping release_year migration")
            conn.close()
            return False

        existing = _get_table_columns(cursor, "tracks", is_pg)
        if "release_year" not in existing:
            logging.info("Adding 'release_year' column to tracks table...")
            cursor.execute("ALTER TABLE tracks ADD COLUMN release_year INTEGER")
            conn.commit()
            logging.info("✓ Added 'release_year' column to tracks table")
        else:
            logging.debug("✓ Column 'release_year' already exists in tracks table")

        conn.close()
        return True
    except RuntimeError as e:
        logging.warning(f"⚠ Skipping release_year migration: {e}")
        return False
    except Exception as e:
        logging.error(f"✗ Error ensuring release_year column exists: {e}", exc_info=True)
        return False

