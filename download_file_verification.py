#!/usr/bin/env python3
"""
Download File Verification System

Verifies that files successfully moved from /downloads to /music remain accessible.
- Verifies immediately after move
- Periodically checks old moved files (30+ minutes)
- Requeues files that go missing from music library
"""

import os
import sqlite3
import logging
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

DB_PATH = os.environ.get("DB_PATH", "/database/sptnr.db")


def _is_postgres_connection(conn):
    """Check if connection is PostgreSQL."""
    try:
        conn.execute("SELECT version();")
        return True
    except:
        return False


def _get_db_connection():
    """Get database connection with proper row factory."""
    try:
        if "postgresql" in os.environ.get("DATABASE_URL", "").lower():
            import psycopg2
            from psycopg2.extras import RealDictCursor
            conn = psycopg2.connect(os.environ.get("DATABASE_URL"))
            conn.row_factory = RealDictCursor
            return conn
        else:
            conn = sqlite3.connect(DB_PATH, timeout=120.0)
            conn.row_factory = sqlite3.Row
            return conn
    except Exception as e:
        logger.error(f"Failed to connect to database: {e}")
        raise


def ensure_verification_columns():
    """
    Ensure the verification columns exist in download_queue table.
    
    Adds:
        - moved_at TIMESTAMP: When file was moved to /music
        - verified_in_music_at TIMESTAMP: When move was verified successful
        - music_file_path TEXT: Final path in /music after verification
    """
    try:
        conn = _get_db_connection()
        cursor = conn.cursor()
        is_pg = _is_postgres_connection(conn)

        # Check if download_queue table exists
        if is_pg:
            cursor.execute(
                "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = 'download_queue'"
            )
        else:
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='download_queue'")

        if not cursor.fetchone():
            logger.warning("download_queue table does not exist yet")
            conn.close()
            return False

        # Determine existing columns
        if is_pg:
            cursor.execute(
                """SELECT column_name FROM information_schema.columns 
                   WHERE table_name = 'download_queue'"""
            )
            existing = [row[0] for row in cursor.fetchall()]
        else:
            cursor.execute("PRAGMA table_info(download_queue)")
            existing = [row[1] for row in cursor.fetchall()]

        columns_to_add = [
            ("moved_at", "TIMESTAMP"),
            ("verified_in_music_at", "TIMESTAMP"),
            ("music_file_path", "TEXT"),
        ]

        added_any = False
        for col_name, col_type in columns_to_add:
            if col_name not in existing:
                try:
                    if is_pg:
                        cursor.execute(f"ALTER TABLE download_queue ADD COLUMN {col_name} {col_type};")
                    else:
                        cursor.execute(f"ALTER TABLE download_queue ADD COLUMN {col_name} {col_type};")
                    conn.commit()
                    logger.info(f"✓ Added column '{col_name}' to download_queue")
                    added_any = True
                except Exception as col_err:
                    if "already exists" not in str(col_err).lower():
                        logger.warning(f"Could not add column {col_name}: {col_err}")

        conn.close()
        return True

    except Exception as e:
        logger.error(f"Error ensuring verification columns: {e}", exc_info=True)
        return False


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
        cursor = conn.cursor()
        is_pg = _is_postgres_connection(conn)
        placeholder = "%s" if is_pg else "?"

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
        cursor = conn.cursor()
        is_pg = _is_postgres_connection(conn)
        placeholder = "%s" if is_pg else "?"

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
        cursor = conn.cursor()
        is_pg = _is_postgres_connection(conn)
        placeholder = "%s" if is_pg else "?"

        # Get the queue item first
        select_sql = "SELECT * FROM download_queue WHERE id = ?"
        cursor.execute(select_sql, (queue_id,))
        item = cursor.fetchone()

        if not item:
            logger.warning(f"Queue item {queue_id} not found for requeuing")
            conn.close()
            return False

        if isinstance(item, dict):
            original_file_path = item.get('file_path')
        else:
            original_file_path = item['file_path'] if hasattr(item, 'keys') else item[11]

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


def check_missing_moved_files(minutes_old=30):
    """
    Find files that were moved to /music but have since disappeared.
    Requeue them for retry.
    
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
        old_files = [dict(row) if isinstance(row, dict) else row for row in cursor.fetchall()]
        conn.close()

        if not old_files:
            return {
                'checked': 0,
                'found_missing': 0,
                'requeued': 0,
                'message': f'No files moved more than {minutes_old} minutes ago'
            }

        logger.info(
            f"Checking {len(old_files)} files moved more than {minutes_old} minutes ago..."
        )

        found_missing = 0
        requeued = 0

        for item in old_files:
            if isinstance(item, dict):
                queue_id = item.get('id')
                music_file_path = item.get('music_file_path')
                artist = item.get('artist', 'Unknown')
                title = item.get('title', 'Unknown')
            else:
                queue_id = item[0]
                music_file_path = item[2]
                artist = item[3]
                title = item[5]

            if not music_file_path:
                continue

            # Check if file still exists
            if not os.path.isfile(music_file_path):
                logger.warning(
                    f"Queue {queue_id}: File missing from /music - "
                    f"{artist} - {title}"
                )
                found_missing += 1

                if requeue_missing_file(queue_id):
                    requeued += 1

        return {
            'checked': len(old_files),
            'found_missing': found_missing,
            'requeued': requeued,
            'message': f'Checked {len(old_files)}, found {found_missing} missing, requeued {requeued}'
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
