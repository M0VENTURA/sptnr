#!/usr/bin/env python3
"""
Download File Verification System

Verifies that files successfully moved from /downloads to /music remain accessible.
- Verifies immediately after move
- Periodically checks old moved files (30+ minutes)
- Requeues files that go missing from music library
"""

import os
import psycopg2
import psycopg2.extras
import logging
import time
from datetime import datetime, timedelta
from contextlib import closing

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [Download Verification] %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("/config/download_verification.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)
_last_pg_startup_log_monotonic = 0.0


def _is_postgres_connection(conn):
    """Check if connection is PostgreSQL."""
    try:
        return hasattr(conn, 'get_dsn_parameters')
    except:
        return False


def _is_postgres_configured():
    """Return True when PostgreSQL connection settings are present in the environment."""
    pg_dsn = (os.environ.get("DATABASE_URL") or os.environ.get("PG_DSN") or "").strip()
    pg_host = (os.environ.get("PG_HOST") or "").strip()
    pg_user = (os.environ.get("PG_USER") or "").strip()
    pg_database = (os.environ.get("PG_DATABASE") or "").strip()
    return bool(pg_dsn or (pg_host and pg_user and pg_database))


def _is_transient_pg_startup_error(error) -> bool:
    message = str(error).lower()
    markers = (
        "the database system is starting up",
        "the database system is in recovery mode",
        "cannot connect now",
    )
    return any(marker in message for marker in markers)


def _log_pg_startup_once(message: str, interval_seconds: int = 30):
    global _last_pg_startup_log_monotonic
    now = time.monotonic()
    if (now - _last_pg_startup_log_monotonic) >= interval_seconds:
        logger.info(message)
        _last_pg_startup_log_monotonic = now
    else:
        logger.debug(message)


def _get_db_connection():
    """Get database connection via helpers abstraction.

    Uses helpers.db_utils.get_db_connection to avoid a circular import: this
    module is imported by app.py at module level and its ensure_* functions are
    called before app.py has finished initialising, so 'from app import get_db'
    would fail with 'partially initialised module' errors.
    """
    from helpers.db_utils import get_db_connection as _helper_get_db
    return _helper_get_db()


def _cursor(conn):
    """Return a dict-capable cursor for PG and a standard cursor for SQLite."""
    if _is_postgres_connection(conn):
        return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    return conn.cursor()


def _placeholder(conn):
    return "%s" if _is_postgres_connection(conn) else "?"


def _row_get(row, key, default=None):
    if row is None:
        return default
    if hasattr(row, "get"):
        return row.get(key, default)
    if hasattr(row, "keys"):
        try:
            return row[key]
        except Exception:
            return default
    return default


def _ensure_columns_in_table(columns_to_add):
    """
    Internal helper: add missing columns to download_queue.

    Args:
        columns_to_add: list of (column_name, column_type) tuples

    Returns:
        True on success, False on failure.
    """
    if not _is_postgres_configured():
        logger.debug("PostgreSQL not configured; skipping download_queue column migration")
        return False

    try:
        conn = _get_db_connection()
        # Use an explicit plain tuple cursor regardless of the connection's default
        # cursor_factory. helpers.db_utils.get_db_connection opens PG connections
        # with cursor_factory=RealDictCursor as the default, which would make
        # row[0] raise KeyError instead of returning the first column.
        import psycopg2.extensions as _pg_ext
        cursor = conn.cursor(_pg_ext.cursor) if _is_postgres_connection(conn) else conn.cursor()

        # Check if download_queue table exists
        cursor.execute(
            "SELECT EXISTS(SELECT FROM information_schema.tables WHERE table_name = 'download_queue')"
        )

        exists_row = cursor.fetchone()
        if not exists_row or not exists_row[0]:
            logger.warning("download_queue table does not exist yet")
            conn.close()
            return False

        # Determine existing columns
        cursor.execute(
            """SELECT column_name FROM information_schema.columns
               WHERE table_name = 'download_queue'"""
        )
        existing = [row[0] for row in cursor.fetchall()]

        for col_name, col_type in columns_to_add:
            if col_name not in existing:
                try:
                    cursor.execute(f"ALTER TABLE download_queue ADD COLUMN {col_name} {col_type};")
                    conn.commit()
                    logger.info(f"✓ Added column '{col_name}' to download_queue")
                except Exception as col_err:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    if "already exists" not in str(col_err).lower():
                        logger.warning(f"Could not add column {col_name}: {col_err}")

        conn.close()
        return True

    except psycopg2.Error as e:
        if _is_transient_pg_startup_error(e):
            _log_pg_startup_once(
                f"Skipping download_queue column migration while PostgreSQL starts: {e}"
            )
        else:
            logger.warning(f"Skipping download_queue column migration: PostgreSQL unavailable - {e}")
        return False
    except Exception as e:
        logger.error(f"Error ensuring download_queue columns: {e}", exc_info=True)
        return False


def ensure_verification_columns():
    """
    Ensure the verification columns exist in download_queue table.

    Adds:
        - moved_at TIMESTAMP: When file was moved to /music
        - verified_in_music_at TIMESTAMP: When move was verified successful
        - music_file_path TEXT: Final path in /music after verification
    """
    return _ensure_columns_in_table([
        ("moved_at", "TIMESTAMP"),
        ("verified_in_music_at", "TIMESTAMP"),
        ("music_file_path", "TEXT"),
    ])


def ensure_queue_mbid_columns():
    """
    Ensure MusicBrainz MBID and extended metadata columns exist in download_queue.

    These columns are required by download_monitor_enhancements.py and the
    /api/queue/<id>/apply-mbid-match endpoint in app.py.

    Adds:
        - release_mbid TEXT: MusicBrainz release ID
        - recording_mbid TEXT: MusicBrainz recording ID
        - release_year INTEGER: Parsed release year
        - duration INTEGER: Track duration in seconds
        - matched_file_path TEXT: File path if already matched
        - in_collection INTEGER: Whether track exists in local library
        - collection_track_id TEXT: ID of matching track in collection
        - collection_matched_at TEXT: Timestamp of collection match
        - copied_individually INTEGER: Whether file already moved individually
        - copied_individually_at TEXT: Timestamp of individual copy event
    """
    return _ensure_columns_in_table([
        ("release_mbid", "TEXT"),
        ("recording_mbid", "TEXT"),
        ("release_year", "INTEGER"),
        ("duration", "INTEGER"),
        ("matched_file_path", "TEXT"),
        ("in_collection", "INTEGER DEFAULT 0"),
        ("collection_track_id", "TEXT"),
        ("collection_matched_at", "TEXT"),
        ("copied_individually", "INTEGER DEFAULT 0"),
        ("copied_individually_at", "TEXT"),
    ])


def verify_file_in_music(queue_id, target_path):
    """
    Verify that a file exists at its target path in /music.
    
    Args:
        queue_id: Download queue item ID
        target_path: Expected path of file in /music
    
    Returns:
        dict: {
            'success': bool,
            'exists': bool,
            'verified_at': timestamp if success
        }
    """
    try:
        if not target_path:
            return {
                'success': False,
                'exists': False,
                'verified_at': None,
                'error': 'No target_path provided'
            }

        file_exists = os.path.isfile(target_path)

        if not file_exists:
            logger.warning(
                f"Queue {queue_id}: File verification FAILED - not found at {target_path}"
            )
            return {
                'success': False,
                'exists': False,
                'verified_at': None,
                'error': f'File not found at {target_path}'
            }

        # Verify file is readable
        if not os.access(target_path, os.R_OK):
            logger.warning(
                f"Queue {queue_id}: File exists but is not readable: {target_path}"
            )
            return {
                'success': False,
                'exists': True,
                'verified_at': None,
                'error': 'File exists but is not readable'
            }

        # Verify file has content
        file_size = os.path.getsize(target_path)
        if file_size == 0:
            logger.warning(f"Queue {queue_id}: File is empty: {target_path}")
            return {
                'success': False,
                'exists': True,
                'verified_at': None,
                'error': 'File size is 0 bytes'
            }

        verified_at = datetime.now().isoformat()
        logger.info(
            f"Queue {queue_id}: File verification SUCCESS - {target_path} ({file_size} bytes)"
        )

        # Update queue item with verification timestamp
        conn = _get_db_connection()
        cursor = _cursor(conn)
        placeholder = _placeholder(conn)

        update_sql = f"""
            UPDATE download_queue
            SET verified_in_music_at = {placeholder},
                music_file_path = {placeholder}
            WHERE id = {placeholder}
        """

        try:
            cursor.execute(update_sql, (verified_at, target_path, queue_id))
            conn.commit()
        finally:
            conn.close()

        return {
            'success': True,
            'exists': True,
            'verified_at': verified_at
        }

    except Exception as e:
        logger.error(f"Error verifying file for queue {queue_id}: {e}", exc_info=True)
        return {
            'success': False,
            'exists': False,
            'verified_at': None,
            'error': str(e)
        }


def mark_queue_item_moved(queue_id, target_path):
    """
    Mark a queue item as moved and set moved_at timestamp.
    
    Args:
        queue_id: Download queue item ID
        target_path: Path where file was moved to
    """
    try:
        conn = _get_db_connection()
        cursor = _cursor(conn)
        placeholder = _placeholder(conn)

        moved_at = datetime.now().isoformat()

        update_sql = f"""
            UPDATE download_queue
            SET moved_at = {placeholder},
                music_file_path = {placeholder}
            WHERE id = {placeholder}
        """

        cursor.execute(update_sql, (moved_at, target_path, queue_id))
        conn.commit()
        conn.close()

        logger.debug(f"Queue {queue_id}: Marked as moved at {moved_at}")

    except Exception as e:
        logger.error(f"Error marking queue item {queue_id} as moved: {e}")


def requeue_missing_file(queue_id):
    """
    Mark a file that went missing from /music back to 'completed' status.
    This allows it to be reprocessed or moved again.
    
    Args:
        queue_id: Download queue item ID
    
    Returns:
        bool: True if successful
    """
    try:
        conn = _get_db_connection()
        cursor = _cursor(conn)
        placeholder = _placeholder(conn)

        # Get the queue item first
        select_sql = f"SELECT * FROM download_queue WHERE id = {placeholder}"
        cursor.execute(select_sql, (queue_id,))
        item = cursor.fetchone()

        if not item:
            logger.warning(f"Queue item {queue_id} not found for requeuing")
            conn.close()
            return False

        # Mark as completed so it can be retried
        update_sql = f"""
            UPDATE download_queue
            SET status = 'completed',
                verified_in_music_at = NULL,
                moved_at = NULL
            WHERE id = {placeholder}
        """

        cursor.execute(update_sql, (queue_id,))
        conn.commit()
        conn.close()

        logger.warning(
            f"Queue {queue_id}: Requeued - file disappeared from /music, "
            f"reverting to 'completed' status for retry"
        )
        return True

    except Exception as e:
        logger.error(f"Error requeuing file {queue_id}: {e}", exc_info=True)
        return False


def _reset_matched_item_to_queued(queue_id):
    """
    Reset a 'matched' queue item back to 'queued' when its source file has
    been deleted from disk.  This allows the track to be re-downloaded rather
    than remaining permanently stuck in the 'matched' state with a dead
    file reference.

    Args:
        queue_id: Download queue item ID

    Returns:
        bool: True if successful
    """
    try:
        conn = _get_db_connection()
        cursor = _cursor(conn)
        placeholder = _placeholder(conn)

        cursor.execute(
            f"""
            UPDATE download_queue
            SET status = 'queued',
                file_path = NULL,
                found_filename = NULL,
                failure_reason = 'Matched file no longer exists on disk; re-queued for download'
            WHERE id = {placeholder} AND status = 'matched'
            """,
            (queue_id,),
        )
        conn.commit()
        conn.close()

        logger.warning(
            f"Queue {queue_id}: Matched file missing from disk — reset to 'queued' for re-download"
        )
        return True

    except Exception as e:
        logger.error(f"Error resetting matched item {queue_id}: {e}", exc_info=True)
        return False


def check_missing_moved_files(minutes_old=30):
    """
    Find files that were moved to /music but have since disappeared.
    Requeue them for retry.

    Also detects 'matched' queue items whose source file no longer exists on
    disk and resets them to 'queued' so they can be re-downloaded.

    This runs periodically to catch filesystem issues or external deletions.
    
    Args:
        minutes_old: Check files moved at least this many minutes ago (default 30)
    
    Returns:
        dict: {
            'checked': int,
            'found_missing': int,
            'requeued': int
        }
    """
    try:
        conn = _get_db_connection()
        cursor = conn.cursor()
        is_pg = _is_postgres_connection(conn)
        placeholder = "%s" if is_pg else "?"

        cutoff_time = (datetime.now() - timedelta(minutes=minutes_old)).isoformat()

        # Get all files that were moved and verified more than X minutes ago
        select_sql = f"""
            SELECT id, music_file_path, artist, album, title
            FROM download_queue
            WHERE status = 'imported'
            AND moved_at IS NOT NULL
            AND verified_in_music_at IS NOT NULL
            AND moved_at < {placeholder}
            ORDER BY moved_at ASC
        """

        cursor.execute(select_sql, (cutoff_time,))
        old_files = cursor.fetchall()

        # Also fetch 'matched' items that have a file_path set so we can verify
        # the source file still exists.  These are items where the user confirmed
        # a match but the file was subsequently deleted before being moved.
        cursor.execute(
            """
            SELECT id, file_path, artist, album, title
            FROM download_queue
            WHERE status = 'matched'
              AND TRIM(COALESCE(file_path, '')) != ''
            """
        )
        matched_with_path = cursor.fetchall() or []

        conn.close()

        found_missing = 0
        requeued = 0
        total_checked = 0

        if not old_files and not matched_with_path:
            return {
                'checked': 0,
                'found_missing': 0,
                'requeued': 0,
                'message': (
                    f'No files to verify (no imported files older than {minutes_old} min '
                    f'and no matched items with paths)'
                ),
            }

        if old_files:
            logger.info(
                f"Checking {len(old_files)} files moved more than {minutes_old} minutes ago..."
            )

        for item in old_files:
            queue_id = _row_get(item, 'id')
            music_file_path = _row_get(item, 'music_file_path')
            artist = _row_get(item, 'artist', 'Unknown')
            title = _row_get(item, 'title', 'Unknown')

            if not music_file_path:
                continue

            total_checked += 1
            # Check if file still exists
            if not os.path.isfile(music_file_path):
                logger.warning(
                    f"Queue {queue_id}: File missing from /music - "
                    f"{artist} - {title}"
                )
                found_missing += 1

                if requeue_missing_file(queue_id):
                    requeued += 1

        # Check matched items whose source file is gone
        for item in matched_with_path:
            queue_id = _row_get(item, 'id')
            file_path = _row_get(item, 'file_path', '')
            artist = _row_get(item, 'artist', 'Unknown')
            title = _row_get(item, 'title', 'Unknown')

            if not file_path:
                continue

            total_checked += 1
            if not os.path.isfile(file_path):
                logger.warning(
                    f"Queue {queue_id}: Matched source file missing — "
                    f"{artist} - {title} ({file_path})"
                )
                found_missing += 1

                if _reset_matched_item_to_queued(queue_id):
                    requeued += 1

        return {
            'checked': total_checked,
            'found_missing': found_missing,
            'requeued': requeued,
            'message': f'Checked {total_checked}, found {found_missing} missing, requeued {requeued}'
        }

    except Exception as e:
        logger.error(f"Error checking missing moved files: {e}", exc_info=True)
        return {
            'checked': 0,
            'found_missing': 0,
            'requeued': 0,
            'error': str(e)
        }


if __name__ == "__main__":
    # Test that columns exist
    ensure_verification_columns()
    
    # Check for missing files
    result = check_missing_moved_files()
    print(f"Result: {result}")
