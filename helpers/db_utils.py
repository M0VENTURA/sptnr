import sqlite3
import os
import logging

DB_PATH = os.environ.get("DB_PATH", "/database/sptnr.db")

def _is_postgres_connection(conn):
    """Detect if connection is PostgreSQL."""
    try:
        import psycopg2
        return isinstance(conn, psycopg2.extensions.connection)
    except (ImportError, AttributeError):
        return False

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

    if pg_dsn or (pg_host and pg_user):
        try:
            import psycopg2
            if pg_dsn:
                conn = psycopg2.connect(pg_dsn)
                logging.debug(f"Connected to PostgreSQL: {pg_dsn.split('@')[1] if '@' in pg_dsn else 'configured'}")
            else:
                conn = psycopg2.connect(
                    host=pg_host,
                    port=int(os.environ.get("PG_PORT", "5432")),
                    user=pg_user,
                    password=os.environ.get("PG_PASSWORD", ""),
                    dbname=pg_database,
                )
                logging.debug(f"Connected to PostgreSQL: {pg_host}/{pg_database}")
            return conn
        except ImportError:
            logging.warning("PostgreSQL configured but psycopg2 not installed, falling back to SQLite")
        except Exception as e:
            logging.warning(f"Failed to connect to PostgreSQL: {e}, falling back to SQLite")
    
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
        logging.info(f"Checking album_artist migration for database: {db_path}")
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Check if tracks table exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tracks'")
        if not cursor.fetchone():
            # Table doesn't exist yet, nothing to migrate
            logging.warning("Tracks table does not exist yet, skipping album_artist migration")
            conn.close()
            return False
        
        # Check if album_artist column exists
        cursor.execute("PRAGMA table_info(tracks)")
        columns = {row[1] for row in cursor.fetchall()}
        
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
        
        # Populate album_artist with artist data where it's NULL
        logging.info("Populating album_artist column from artist data...")
        try:
            cursor.execute("UPDATE tracks SET album_artist = artist WHERE album_artist IS NULL")
            rows_updated = cursor.rowcount
            conn.commit()
            logging.info(f"✓ Populated album_artist for {rows_updated} rows")
        except Exception as e:
            logging.error(f"✗ Failed to populate album_artist column: {e}")
            conn.close()
            raise
        
        logging.info("✓ album_artist migration complete")
        conn.close()
        return True
        
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

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tracks'")
        if not cursor.fetchone():
            logging.warning("Tracks table does not exist yet, skipping MBID column migration")
            conn.close()
            return False

        cursor.execute("PRAGMA table_info(tracks)")
        columns = {row[1] for row in cursor.fetchall()}

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
                cursor.execute("PRAGMA table_info(tracks)")
                columns_after = {row[1] for row in cursor.fetchall()}
                if "musicbrainz_album_mbid" not in columns_after:
                    logging.info("New column doesn't exist; adding it instead")
                    cursor.execute("ALTER TABLE tracks ADD COLUMN musicbrainz_album_mbid TEXT")
                    conn.commit()
                    logging.info("✓ Added musicbrainz_album_mbid column")
        elif has_legacy and has_new:
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
        
        # Check if tracks table exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tracks'")
        if not cursor.fetchone():
            return {"exists": False, "message": "Tracks table does not exist"}
        
        # Check if album_artist column exists
        cursor.execute("PRAGMA table_info(tracks)")
        columns = {row[1] for row in cursor.fetchall()}
        
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
        return int(row[0]) if row else 0
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
        if is_pg:
            cursor.execute(
                "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'tracks' AND table_schema = current_schema()"
            )
        else:
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tracks'")
        row = cursor.fetchone()
        table_exists = (row[0] if row else 0) if is_pg else bool(row)
        if not table_exists:
            logging.warning("Tracks table does not exist yet, skipping writer column migration")
            conn.close()
            return False
        
        # Check if writer column exists (database-agnostic)
        if is_pg:
            cursor.execute(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_name = 'tracks' AND column_name = 'writer' AND table_schema = current_schema()"
            )
            writer_exists = (cursor.fetchone()[0] or 0) > 0
        else:
            cursor.execute("PRAGMA table_info(tracks)")
            columns = {row[1] for row in cursor.fetchall()}
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
        if is_pg:
            cursor.execute(
                "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'tracks' AND table_schema = current_schema()"
            )
        else:
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tracks'")
        row = cursor.fetchone()
        table_exists = (row[0] if row else 0) if is_pg else bool(row)
        if not table_exists:
            logging.warning("Tracks table does not exist yet, skipping cover columns migration")
            conn.close()
            return False

        # Determine existing columns
        if is_pg:
            cursor.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'tracks' AND table_schema = current_schema()"
            )
            existing = {row[0] for row in cursor.fetchall()}
        else:
            cursor.execute("PRAGMA table_info(tracks)")
            existing = {row[1] for row in cursor.fetchall()}

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

    except Exception as e:
        logging.error(f"✗ Error ensuring cover columns exist: {e}", exc_info=True)
        return False

