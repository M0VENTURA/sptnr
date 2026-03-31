import os
import logging
import time

DB_PATH = os.environ.get("DB_PATH", "/database/sptnr.db")
_pg_last_failure_monotonic = 0.0
_PG_FAILURE_BACKOFF_SECONDS = float(os.environ.get("PG_FAILURE_BACKOFF_SECONDS", "30"))

# Default session timeout for idle-in-transaction connections (milliseconds).
# Connections that sit idle inside an open transaction longer than this are
# automatically aborted by PostgreSQL, preventing the
# "unexpected EOF on client connection with an open transaction" log spam and
# the slow-checkpoint / VACUUM-blocking effects that follow.
# Override via PG_IDLE_IN_TRANSACTION_TIMEOUT_MS env-var (0 = disabled).
_PG_IDLE_IN_TRANSACTION_TIMEOUT_MS = int(
    os.environ.get("PG_IDLE_IN_TRANSACTION_TIMEOUT_MS", "60000")  # 60 seconds (was 5 min)
)


class _AutoRollbackPGConnection:
    """Wraps a psycopg2 connection to guarantee a ROLLBACK is sent before the
    underlying TCP connection is closed.

    Without this wrapper, psycopg2 closes the socket without sending a
    ROLLBACK, which causes PostgreSQL to log
    "unexpected EOF on client connection with an open transaction" for every
    read-only (or partially-used) connection.  Over time this also prevents
    VACUUM from reclaiming dead tuples and inflates WAL / checkpoint volume.

    All attribute access is transparently forwarded to the real connection so
    no call sites need to change.  ``_is_postgres_connection()`` recognises
    the wrapper via the ``_conn`` attribute.
    """

    __slots__ = ("_conn", "_closed")

    def __init__(self, conn):
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "_closed", False)

    # ------------------------------------------------------------------
    # Core override: rollback before close (idempotent — safe to call twice)
    # ------------------------------------------------------------------
    def close(self):
        if object.__getattribute__(self, "_closed"):
            return
        object.__setattr__(self, "_closed", True)
        conn = object.__getattribute__(self, "_conn")
        try:
            conn.rollback()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Context-manager: matches psycopg2 native behaviour
    # (commit on success, rollback on error; does NOT close)
    # ------------------------------------------------------------------
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        conn = object.__getattribute__(self, "_conn")
        if exc_type is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        else:
            try:
                conn.commit()
            except Exception:
                pass
        return False

    # ------------------------------------------------------------------
    # Transparent delegation
    # ------------------------------------------------------------------
    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_conn"), name)

    def __setattr__(self, name, value):
        if name == "_conn":
            object.__setattr__(self, name, value)
        else:
            setattr(object.__getattribute__(self, "_conn"), name, value)


def is_transient_pg_startup_error(error) -> bool:
    """Return True for transient PostgreSQL startup/recovery availability errors."""
    message = str(error).lower()
    markers = (
        "the database system is starting up",
        "the database system is in recovery mode",
        "cannot connect now",
        "terminating connection due to administrator command",
        "recent connection failures are in backoff",
    )
    return any(marker in message for marker in markers)


def is_postgres_configured() -> bool:
    """Return True when PostgreSQL connection settings are present in the environment."""
    pg_dsn = (os.environ.get("DATABASE_URL") or os.environ.get("PG_DSN") or "").strip()
    pg_host = (os.environ.get("PG_HOST") or "").strip()
    pg_user = (os.environ.get("PG_USER") or "").strip()
    pg_database = (os.environ.get("PG_DATABASE") or "").strip()
    return bool(pg_dsn or (pg_host and pg_user and pg_database))


def _is_postgres_connection(conn) -> bool:
    """Return True when *conn* is (or wraps) a psycopg2 connection."""
    try:
        import psycopg2
        # Unwrap _AutoRollbackPGConnection transparently.
        underlying = getattr(conn, "_conn", conn)
        return isinstance(underlying, psycopg2.extensions.connection)
    except (ImportError, AttributeError):
        return False


def _row_first_value(row, default=None):
    """Return the first value from a psycopg2 RealDictRow (or plain dict/tuple)."""
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


def _table_exists(cursor, table_name):
    """Check whether a table exists in the current PostgreSQL schema."""
    cursor.execute(
        "SELECT COUNT(*) AS count FROM information_schema.tables "
        "WHERE table_name = %s AND table_schema = current_schema()",
        (table_name,)
    )
    return (_row_first_value(cursor.fetchone(), 0) or 0) > 0


def _get_table_columns(cursor, table_name):
    """Return a set of column names for a PostgreSQL table."""
    cursor.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = %s AND table_schema = current_schema()",
        (table_name,)
    )
    return {str(_row_first_value(row, "")) for row in cursor.fetchall() if _row_first_value(row, "")}

def get_db_connection() -> "_AutoRollbackPGConnection":
    """Return a PostgreSQL connection wrapped in :class:`_AutoRollbackPGConnection`.

    The wrapper ensures that any open transaction is rolled back before the
    connection is closed, preventing PostgreSQL from logging
    "unexpected EOF on client connection with an open transaction".

    Raises ``RuntimeError`` when PostgreSQL is not configured or unreachable.
    """
    if not is_postgres_configured():
        raise RuntimeError(
            "PostgreSQL is not configured.  Set PG_HOST, PG_USER, and PG_DATABASE "
            "(or DATABASE_URL / PG_DSN) in the environment."
        )

    global _pg_last_failure_monotonic
    now = time.monotonic()
    if _pg_last_failure_monotonic > 0:
        elapsed = now - _pg_last_failure_monotonic
        if elapsed < _PG_FAILURE_BACKOFF_SECONDS:
            remaining = int(_PG_FAILURE_BACKOFF_SECONDS - elapsed)
            raise RuntimeError(
                "PostgreSQL recent connection failures are in backoff "
                f"for another ~{remaining}s"
            )

    try:
        import psycopg2
        import psycopg2.extras

        pg_dsn = os.environ.get("DATABASE_URL") or os.environ.get("PG_DSN")
        pg_host = os.environ.get("PG_HOST", "")
        pg_user = os.environ.get("PG_USER", "")
        pg_database = os.environ.get("PG_DATABASE", "sptnr")

        options_parts = []
        if _PG_IDLE_IN_TRANSACTION_TIMEOUT_MS > 0:
            options_parts.append(
                f"-c idle_in_transaction_session_timeout={_PG_IDLE_IN_TRANSACTION_TIMEOUT_MS}"
            )
        options = " ".join(options_parts) or None

        if pg_dsn:
            raw = psycopg2.connect(
                pg_dsn,
                cursor_factory=psycopg2.extras.RealDictCursor,
                connect_timeout=10,
                **({"options": options} if options else {}),
            )
            logging.debug(
                "Connected to PostgreSQL: %s",
                pg_dsn.split("@")[1] if "@" in pg_dsn else "configured",
            )
        else:
            raw = psycopg2.connect(
                host=pg_host,
                port=int(os.environ.get("PG_PORT", "5432")),
                user=pg_user,
                password=os.environ.get("PG_PASSWORD", ""),
                dbname=pg_database,
                cursor_factory=psycopg2.extras.RealDictCursor,
                connect_timeout=10,
                **({"options": options} if options else {}),
            )
            logging.debug("Connected to PostgreSQL: %s/%s", pg_host, pg_database)

        _pg_last_failure_monotonic = 0.0
        return _AutoRollbackPGConnection(raw)

    except ImportError as exc:
        raise RuntimeError(
            "psycopg2 is not installed.  Run: pip install psycopg2-binary"
        ) from exc
    except Exception as exc:
        _pg_last_failure_monotonic = time.monotonic()
        raise RuntimeError(f"PostgreSQL connection failed: {exc}") from exc


def ensure_album_artist_column():
    """Ensure the album_artist column exists in the tracks table AND populate it.

    Called on app startup.  Uses a PostgreSQL advisory lock so only one worker
    performs the migration at a time, and small SKIP LOCKED batches to avoid
    long row-lock chains.
    """
    import logging
    import sys

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        if not _table_exists(cursor, "tracks"):
            logging.warning("Tracks table does not exist yet, skipping album_artist migration")
            conn.close()
            return False

        columns = _get_table_columns(cursor, "tracks")

        if "album_artist" not in columns:
            logging.info("Creating album_artist column...")
            try:
                cursor.execute("ALTER TABLE tracks ADD COLUMN album_artist TEXT")
                conn.commit()
                logging.info("✓ Successfully added album_artist column to tracks table")
            except Exception as e:
                if "duplicate column name" not in str(e).lower() and "already exists" not in str(e).lower():
                    logging.error(f"✗ Failed to add album_artist column: {e}")
                    conn.close()
                    raise

        logging.debug("Populating album_artist column from artist data...")
        try:
            lock_key = 915317411
            cursor.execute("SELECT pg_try_advisory_lock(%s) AS acquired", (lock_key,))
            lock_row = cursor.fetchone()
            lock_acquired = bool(lock_row.get("acquired")) if isinstance(lock_row, dict) else bool(lock_row[0])

            if not lock_acquired:
                logging.debug("Another worker is already running album_artist migration; skipping")
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
        return False


def ensure_musicbrainz_album_mbid_column():
    """Ensure tracks table uses ``musicbrainz_album_mbid`` instead of legacy ``beets_album_mbid``."""
    import logging

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        if not _table_exists(cursor, "tracks"):
            logging.warning("Tracks table does not exist yet, skipping MBID column migration")
            conn.close()
            return False

        columns = _get_table_columns(cursor, "tracks")
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
                columns_after = _get_table_columns(cursor, "tracks")
                if "musicbrainz_album_mbid" not in columns_after:
                    logging.info("New column doesn't exist; adding it instead")
                    cursor.execute("ALTER TABLE tracks ADD COLUMN musicbrainz_album_mbid TEXT")
                    conn.commit()
                    logging.info("✓ Added musicbrainz_album_mbid column")
        elif has_legacy and has_new:
            mbid_lock_key = 915317412
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
    """Verify that the album_artist column exists and is functional."""
    import logging

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        if not _table_exists(cursor, "tracks"):
            conn.close()
            return {"exists": False, "message": "Tracks table does not exist"}

        columns = _get_table_columns(cursor, "tracks")
        conn.close()

        if "album_artist" in columns:
            return {"exists": True, "message": "album_artist column exists and is functional"}
        return {"exists": False, "message": "album_artist column does NOT exist - migration failed or not run"}
    except Exception as e:
        return {"exists": False, "message": f"Error verifying column: {e}"}


def get_current_track_rating(track_id: str) -> int:
    """Query the current star rating for a track from the database (0–5)."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT stars FROM tracks WHERE id = %s", (track_id,))
        row = cursor.fetchone()
        conn.close()
        value = _row_first_value(row, 0)
        return int(value) if value is not None else 0
    except Exception as e:
        import logging
        logging.debug(f"Failed to get current rating for track {track_id}: {e}")
        return 0


def ensure_writer_column():
    """Ensure the writer column exists in the tracks table for lyricist/songwriter data."""
    import logging

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        if not _table_exists(cursor, "tracks"):
            logging.warning("Tracks table does not exist yet, skipping writer column migration")
            conn.close()
            return False

        columns = _get_table_columns(cursor, "tracks")
        if "writer" not in columns:
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
        if is_transient_pg_startup_error(e):
            logging.info(f"Skipping writer column migration while PostgreSQL starts: {e}")
        else:
            logging.warning(f"⚠ Skipping writer column migration: {e}")
        return False
    except Exception as e:
        logging.error(f"✗ Error ensuring writer column exists: {e}", exc_info=True)
        return False


def ensure_cover_columns():
    """Ensure cover-detection columns exist in the tracks table.

    Adds: ``is_cover`` BIGINT, ``is_cover_reason`` TEXT, ``original_cover_artist`` TEXT.
    """
    import logging

    columns_to_add = [
        ("is_cover", "BIGINT DEFAULT 0"),
        ("is_cover_reason", "TEXT"),
        ("original_cover_artist", "TEXT"),
        ("cover_manual_override", "BOOLEAN DEFAULT FALSE"),
    ]

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        if not _table_exists(cursor, "tracks"):
            logging.warning("Tracks table does not exist yet, skipping cover columns migration")
            conn.close()
            return False

        existing = _get_table_columns(cursor, "tracks")

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
        if is_transient_pg_startup_error(e):
            logging.info(f"Skipping cover columns migration while PostgreSQL starts: {e}")
        else:
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

        if not _table_exists(cursor, "tracks"):
            logging.warning("Tracks table does not exist yet, skipping release_year migration")
            conn.close()
            return False

        try:
            conn.rollback()
        except Exception:
            pass

        if "release_year" not in _get_table_columns(cursor, "tracks"):
            logging.info("Adding 'release_year' column to tracks table...")
            try:
                cursor.execute("ALTER TABLE tracks ADD COLUMN IF NOT EXISTS release_year INTEGER")
                conn.commit()
            except Exception as e:
                conn.rollback()
                err_text = str(e).lower()
                if "duplicate column" in err_text or "already exists" in err_text:
                    logging.info("✓ Column 'release_year' already exists in tracks table")
                else:
                    raise

            refreshed = _get_table_columns(cursor, "tracks")
            if "release_year" in refreshed:
                logging.info("✓ Added 'release_year' column to tracks table")
            else:
                logging.warning("⚠ Column 'release_year' was not present after migration attempt")
        else:
            logging.debug("✓ Column 'release_year' already exists in tracks table")

        conn.close()
        return True
    except RuntimeError as e:
        if is_transient_pg_startup_error(e):
            logging.info(f"Skipping release_year migration while PostgreSQL starts: {e}")
        else:
            logging.warning(f"⚠ Skipping release_year migration: {e}")
        return False
    except Exception as e:
        logging.error(f"✗ Error ensuring release_year column exists: {e}", exc_info=True)
        return False


def ensure_mood_columns():
    """Ensure mood enrichment columns exist in the tracks table."""
    import logging

    columns_to_add = [
        ("mood", "TEXT"),
        ("mood_confidence", "DOUBLE PRECISION"),
        ("mood_source", "TEXT"),
        ("mood_last_updated", "TIMESTAMP"),
    ]

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        if not _table_exists(cursor, "tracks"):
            logging.warning("Tracks table does not exist yet, skipping mood columns migration")
            conn.close()
            return False

        existing = _get_table_columns(cursor, "tracks")
        for col_name, col_def in columns_to_add:
            if col_name in existing:
                continue
            try:
                cursor.execute(f"ALTER TABLE tracks ADD COLUMN IF NOT EXISTS {col_name} {col_def}")
                conn.commit()
                logging.info(f"✓ Added '{col_name}' column to tracks table")
            except Exception as e:
                conn.rollback()
                logging.error(f"✗ Failed to add '{col_name}' column: {e}")

        conn.close()
        return True
    except RuntimeError as e:
        if is_transient_pg_startup_error(e):
            logging.info(f"Skipping mood columns migration while PostgreSQL starts: {e}")
        else:
            logging.warning(f"⚠ Skipping mood columns migration: {e}")
        return False
    except Exception as e:
        logging.error(f"✗ Error ensuring mood columns exist: {e}", exc_info=True)
        return False


def ensure_essentia_feature_columns():
    """Ensure Essentia-derived audio feature columns exist in the tracks table."""
    import logging

    columns_to_add = [
        ("danceability", "DOUBLE PRECISION"),
        ("essentia_last_updated", "TIMESTAMP"),
    ]

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        if not _table_exists(cursor, "tracks"):
            logging.warning("Tracks table does not exist yet, skipping Essentia feature migration")
            conn.close()
            return False

        # Run column checks per-iteration so concurrent startups or prior
        # transactional errors do not poison subsequent ALTER statements.
        for col_name, col_def in columns_to_add:
            try:
                # Clear any inherited aborted transaction state before checking.
                try:
                    conn.rollback()
                except Exception:
                    pass

                existing = _get_table_columns(cursor, "tracks")
                if col_name in existing:
                    logging.info(f"✓ Column '{col_name}' already exists")
                    continue

                cursor.execute(f"ALTER TABLE tracks ADD COLUMN IF NOT EXISTS {col_name} {col_def}")
                conn.commit()

                # Re-check existence to emit an accurate message.
                refreshed = _get_table_columns(cursor, "tracks")
                if col_name in refreshed:
                    logging.info(f"✓ Added '{col_name}' column to tracks table")
                else:
                    logging.warning(f"⚠ Column '{col_name}' was not present after migration attempt")
            except Exception as e:
                conn.rollback()
                logging.error(f"✗ Failed to add '{col_name}' column: {e}")

        conn.close()
        return True
    except RuntimeError as e:
        if is_transient_pg_startup_error(e):
            logging.info(f"Skipping Essentia feature migration while PostgreSQL starts: {e}")
        else:
            logging.warning(f"⚠ Skipping Essentia feature migration: {e}")
        return False
    except Exception as e:
        logging.error(f"✗ Error ensuring Essentia feature columns exist: {e}", exc_info=True)
        return False



def ensure_navidrome_tag_columns():
    """Ensure all Navidrome-mapped tag columns exist in the tracks table."""
    import logging

    columns_to_add = [
        # Sort keys
        ("titlesort", "TEXT"),
        ("albumsort", "TEXT"),
        ("composersort", "TEXT"),
        ("lyricistsort", "TEXT"),
        ("artistssort", "TEXT"),
        ("albumartistssort", "TEXT"),
        # Multi-value artist fields
        ("artists", "TEXT"),
        ("albumartists", "TEXT"),
        # Credits
        ("conductor", "TEXT"),
        ("director", "TEXT"),
        ("djmixer", "TEXT"),
        ("engineer", "TEXT"),
        ("remixer", "TEXT"),
        ("lyricist", "TEXT"),
        # Release info
        ("recordlabel", "TEXT"),
        ("copyright", "TEXT"),
        ("releasedate", "TEXT"),
        # Content
        ("lyrics", "TEXT"),
        ("subtitle", "TEXT"),
        ("discsubtitle", "TEXT"),
        ("albumversion", "TEXT"),
        ("grouping", "TEXT"),
        ("movement", "TEXT"),
        ("movementname", "TEXT"),
        ("movementtotal", "TEXT"),
        # Technical/Legal
        ("key", "TEXT"),
        ("language", "TEXT"),
        ("license", "TEXT"),
        ("website", "TEXT"),
        ("encodedby", "TEXT"),
        ("encodersettings", "TEXT"),
        ("explicitstatus", "TEXT"),
        # ReplayGain (stored as TEXT to preserve units like "-6.54 dB" from ID3 frames)
        ("replaygain_track_gain", "TEXT"),
        ("replaygain_track_peak", "TEXT"),
        ("replaygain_album_gain", "TEXT"),
        ("replaygain_album_peak", "TEXT"),
        # R128
        ("r128_track_gain", "TEXT"),
        ("r128_album_gain", "TEXT"),
        # Fields that may already exist but ensure they're present
        ("performer", "TEXT"),
        ("label", "TEXT"),
        ("releasecountry", "TEXT"),
        ("releasestatus", "TEXT"),
        ("releasetype", "TEXT"),
        ("media", "TEXT"),
        ("barcode", "TEXT"),
        ("catalognumber", "TEXT"),
        ("asin", "TEXT"),
        ("originalyear", "TEXT"),
        ("originaldate", "TEXT"),
        ("tracktotal", "TEXT"),
        ("totaldiscs", "TEXT"),
        ("disctotal", "TEXT"),
        ("script", "TEXT"),
        # Sort fields with single-'s' names (distinct from the double-'s' variants above)
        ("artistsort", "TEXT"),
        ("albumartistsort", "TEXT"),
        # iTunes compilation flag
        ("compilation", "TEXT"),
        # Multi-value artist lists
        ("artists", "TEXT"),
    ]

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        if not _table_exists(cursor, "tracks"):
            logging.warning("Tracks table does not exist yet, skipping Navidrome tag columns migration")
            conn.close()
            return False

        for col_name, col_def in columns_to_add:
            try:
                try:
                    conn.rollback()
                except Exception:
                    pass

                existing = _get_table_columns(cursor, "tracks")
                if col_name in existing:
                    continue

                cursor.execute(f"ALTER TABLE tracks ADD COLUMN IF NOT EXISTS {col_name} {col_def}")
                conn.commit()
                logging.info(f"✓ Added '{col_name}' column to tracks table")
            except Exception as e:
                conn.rollback()
                logging.error(f"✗ Failed to add '{col_name}' column: {e}")

        conn.close()
        return True
    except RuntimeError as e:
        if is_transient_pg_startup_error(e):
            logging.info(f"Skipping Navidrome tag columns migration while PostgreSQL starts: {e}")
        else:
            logging.warning(f"⚠ Skipping Navidrome tag columns migration: {e}")
        return False
    except Exception as e:
        logging.error(f"✗ Error ensuring Navidrome tag columns exist: {e}", exc_info=True)
        return False
