#!/usr/bin/env python3
"""
Download Queue Manager
Manages download queue and file completion tracking for Soulseek downloads.
Monitors /downloads folder for completed files and matches them to queue items.
"""

import os
import sqlite3
import json
import logging
import time
import threading
import yaml
import requests
from difflib import SequenceMatcher
from datetime import datetime, timedelta
from pathlib import Path
from helpers.metadata_reader import read_mp3_metadata
from api_clients import session  # Use shared session with retry logic & connection pooling

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [Download Queue] %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("/config/download_queue.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "/database/sptnr.db")

# Global state for tracking scan progress (used by /api/downloads/scan-progress)
_scan_progress = {
    'scanning': False,
    'files_found': 0,
    'recent_files': [],  # List of last 50 files found
    'start_time': None,
    'current_path': '',
}

# Throttle repetitive empty-scan logs to avoid warning spam when downloads folder is idle.
_last_no_audio_log_at = 0.0
_NO_AUDIO_LOG_INTERVAL_SECONDS = 600

_queue_schema_checked = False
_queue_schema_lock = threading.Lock()

def get_scan_progress():
    """Get current scan progress state."""
    return _scan_progress.copy()

def update_scan_progress(files_found=None, recent_file=None, scanning=None, current_path=None):
    """Update scan progress state."""
    global _scan_progress
    if files_found is not None:
        _scan_progress['files_found'] = files_found
    if recent_file is not None:
        _scan_progress['recent_files'].append(recent_file)
        # Keep only last 50 files
        if len(_scan_progress['recent_files']) > 50:
            _scan_progress['recent_files'] = _scan_progress['recent_files'][-50:]
    if scanning is not None:
        _scan_progress['scanning'] = scanning
        if scanning:
            _scan_progress['start_time'] = datetime.now().isoformat()
    if current_path is not None:
        _scan_progress['current_path'] = current_path



def resolve_downloads_dir():
    """Resolve downloads directory from env/config with safe fallback."""
    def _prefer_music_subfolder(path: str) -> str:
        if not path:
            return path
        normalized = os.path.normpath(path)
        if os.path.basename(normalized).lower() == "downloads":
            # Normalize root downloads path to the Music subfolder consistently.
            return os.path.join(normalized, "Music")
        return path

    config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")
    try:
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            configured = (cfg.get("downloads") or {}).get("folder")
            if configured:
                return _prefer_music_subfolder(configured)
    except Exception as e:
        logger.warning(f"Could not read downloads folder from config: {e}")

    env_dir = os.environ.get("DOWNLOADS_DIR")
    if env_dir:
        return _prefer_music_subfolder(env_dir)

    return "/downloads/Music"


def get_downloads_dir():
    """Dynamically get downloads directory (re-evaluates on each call for config changes)."""
    return resolve_downloads_dir()

MUSIC_DIR = os.environ.get("MUSIC_ROOT", "/music")


def retry_on_db_lock(max_retries=5, initial_delay=0.1):
    """Decorator to retry database operations on locked database error"""
    def decorator(func):
        def wrapper(*args, **kwargs):
            delay = initial_delay
            last_error = None
            
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except sqlite3.OperationalError as e:
                    if 'database is locked' in str(e):
                        last_error = e
                        if attempt < max_retries - 1:
                            time.sleep(delay)
                            delay = min(delay * 2, 5.0)  # Exponential backoff, max 5 seconds
                            logger.warning(f"Database locked, retrying (attempt {attempt + 1}/{max_retries})...")
                            continue
                    raise
            
            if last_error:
                raise last_error
        return wrapper
    return decorator


def get_db():
    """Get database connection with proper timeout and locking"""
    conn = sqlite3.connect(DB_PATH, timeout=30.0)  # Increased timeout to 30 seconds
    conn.row_factory = sqlite3.Row
    # Enable WAL mode for better concurrent access
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except Exception as e:
        logger.warning(f"Could not enable WAL mode: {e}")
    # Ask SQLite to wait for lock release before raising OperationalError.
    try:
        conn.execute("PRAGMA busy_timeout=5000")
    except Exception as e:
        logger.warning(f"Could not set busy_timeout: {e}")
    return conn


def _ensure_download_queue_columns(conn, cursor, is_pg=False):
    """Ensure expected queue columns exist (run once per process)."""
    global _queue_schema_checked
    if _queue_schema_checked:
        return

    with _queue_schema_lock:
        if _queue_schema_checked:
            return

        if is_pg:
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'download_queue'
                  AND table_schema = current_schema()
            """)
            columns = [row['column_name'] for row in cursor.fetchall()]
        else:
            cursor.execute("PRAGMA table_info(download_queue);")
            columns = [row[1] for row in cursor.fetchall()]

        required_cols = {
            'search_query': "TEXT",
            'source': "TEXT DEFAULT 'soulseek'",
            'priority': "INTEGER DEFAULT 5",
            'import_group': "TEXT",
            'import_type': "TEXT DEFAULT 'song'",
            'track_number': "TEXT",
            'disc_number': "TEXT",
            'album_artist': "TEXT",
            'year': "TEXT",
            'release_id': "TEXT",
            'release_source': "TEXT",
            'copied_individually': "INTEGER DEFAULT 0",
            'copied_individually_at': "TEXT",
        }

        for col, col_type in required_cols.items():
            if col not in columns:
                logger.info(f"Adding missing column '{col}' to download_queue")
                try:
                    cursor.execute(f"ALTER TABLE download_queue ADD COLUMN {col} {col_type};")
                    conn.commit()
                except Exception as e:
                    logger.warning(f"Could not add {col} column: {e}")

        _queue_schema_checked = True


def execute_write_with_retry(cursor, conn, query, params=(), context="database write", max_retries=5, initial_delay=0.1):
    """Execute a write query with commit retry on SQLite lock contention."""
    delay = initial_delay
    for attempt in range(max_retries):
        try:
            cursor.execute(query, params)
            conn.commit()
            return True
        except sqlite3.OperationalError as e:
            if 'database is locked' in str(e).lower() and attempt < max_retries - 1:
                logger.warning(
                    f"Database locked during {context}, retrying in {delay:.2f}s "
                    f"(attempt {attempt + 1}/{max_retries})"
                )
                time.sleep(delay)
                delay = min(delay * 2, 5.0)
                continue
            raise
    return False


def trigger_navidrome_scan():
    """
    Trigger Navidrome library scan via Subsonic API.
    Does not wait for completion - scan runs in background on Navidrome server.
    
    Returns:
        bool: True if scan triggered successfully, False otherwise
    """
    try:
        import yaml
        import requests
        import hashlib
        
        # Load config to get Navidrome credentials
        config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")
        if not os.path.exists(config_path):
            logger.warning("Config file not found, cannot trigger Navidrome scan")
            return False
        
        with open(config_path, 'r') as f:
            config = yaml.safe_load(f) or {}
        
        navidrome_config = config.get('navidrome', {})
        base_url = navidrome_config.get('base_url', '')
        username = navidrome_config.get('username', '')
        password = navidrome_config.get('password', '')
        
        if not all([base_url, username, password]):
            logger.warning("Navidrome credentials not configured, skipping scan trigger")
            return False
        
        # Use Subsonic API startScan endpoint
        # Note: MD5 is required by Subsonic API spec for enc: prefix
        password_hash = hashlib.md5(password.encode()).hexdigest()
        
        url = f"{base_url}/rest/startScan"
        params = {
            "u": username,
            "p": f"enc:{password_hash}",
            "v": "1.16.1",
            "c": "sptnr",
            "f": "json"
        }
        
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        
        result = response.json()
        
        # Check for Subsonic API success response
        if result.get("subsonic-response", {}).get("status") == "ok":
            logger.info("✅ Navidrome library scan triggered successfully")
            return True
        else:
            error = result.get("subsonic-response", {}).get("error", {})
            logger.warning(f"Navidrome scan response error: {error}")
            return False
            
    except ImportError as e:
        logger.warning(f"Missing required library for Navidrome scan: {e}")
        return False
    except Exception as e:
        logger.warning(f"Could not trigger Navidrome scan: {e}")
        return False


def add_to_queue(artist, title, album=None, source='soulseek', priority=5, import_group=None, import_type='song',
                 track_number=None, album_artist=None, year=None, release_id=None, release_source=None):
    """
    Add a song to the download queue
    
    Args:
        artist: Artist name
        title: Song title
        album: Album name (optional)
        source: 'soulseek' or 'qbittorrent'
        priority: Priority level (1-10, lower = higher priority)
        import_group: Group ID for batch imports (optional, e.g., for albums/playlists)
        import_type: Type of import - 'song', 'album', or 'playlist' (defaults to 'song')
        track_number: Track number from MusicBrainz/Discogs (optional)
        album_artist: Album artist from MusicBrainz/Discogs (optional)
        year: Release year from MusicBrainz/Discogs (optional)
        release_id: MusicBrainz/Discogs release ID (optional)
        release_source: Source of metadata - 'musicbrainz' or 'discogs' (optional)
    
    Returns:
        Queue item dict or None if failed
    """
    try:
        # Use the instance-configured DB method.
        # If PostgreSQL is configured but cannot be resolved, fail fast instead of silently falling back to SQLite.
        is_pg = False
        pg_configured = bool(os.environ.get("PG_HOST") and os.environ.get("PG_USER") and os.environ.get("PG_DATABASE"))
        try:
            from app import get_db as app_get_db, _is_postgres_connection as app_is_postgres_connection
            conn = app_get_db()
            is_pg = bool(app_is_postgres_connection(conn))
        except Exception:
            if pg_configured:
                raise RuntimeError("PostgreSQL is configured for this instance, but queue manager could not acquire app DB connection")
            conn = get_db()
        cursor = conn.cursor()

        logger.debug(f"[add_to_queue] Using {'PostgreSQL' if is_pg else 'SQLite'} backend")
        
        # Validate inputs
        if not artist or not title:
            logger.error("Artist and title are required")
            conn.close()
            return None
        
        # Ensure required columns exist for both SQLite and PostgreSQL.
        _ensure_download_queue_columns(conn, cursor, is_pg=is_pg)
        
        # Search query for Soulseek: artist and title only (no album)
        search_query = f"{artist} - {title}"
        
        try:
            if is_pg:
                cursor.execute(
                    """
                    INSERT INTO download_queue 
                    (artist, title, album, search_query, source, status, priority, file_path, import_group, import_type, 
                     track_number, album_artist, year, release_id, release_source, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, 'queued', %s, NULL, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    RETURNING id
                    """,
                    (artist, title, album, search_query, source, priority, import_group, import_type,
                     track_number, album_artist, year, release_id, release_source),
                )
                inserted = cursor.fetchone()
                conn.commit()
                queue_id = inserted.get('id') if isinstance(inserted, dict) else inserted[0]
            else:
                from app import _is_postgres_connection as app_is_postgres_connection
                is_pg = bool(app_is_postgres_connection(conn))
                placeholder = "%s" if is_pg else "?"
                execute_write_with_retry(
                    cursor,
                    conn,
                    f"""
                    INSERT INTO download_queue 
                    (artist, title, album, search_query, source, status, priority, file_path, import_group, import_type, 
                     track_number, album_artist, year, release_id, release_source, created_at, updated_at)
                    VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, 'queued', {placeholder}, NULL, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """,
                    (artist, title, album, search_query, source, priority, import_group, import_type,
                     track_number, album_artist, year, release_id, release_source),
                    context="add_to_queue insert",
                    max_retries=8,
                    initial_delay=0.2,
                )

                queue_id = cursor.lastrowid
            
            logger.info(f"Added to queue: {search_query} (ID: {queue_id}, source: {source})")
            
            # Return the item
            if is_pg:
                cursor.execute("SELECT * FROM download_queue WHERE id = %s", (queue_id,))
            else:
                cursor.execute("SELECT * FROM download_queue WHERE id = ?", (queue_id,))
            item = cursor.fetchone()
            
            if item:
                return dict(item)
            else:
                logger.error(f"Failed to retrieve inserted item with ID: {queue_id}")
                return None
                
        finally:
            conn.close()
        
    except sqlite3.IntegrityError as e:
        logger.error(f"Database integrity error adding to queue: {e}")
        return None
    except sqlite3.DatabaseError as e:
        logger.error(f"Database error adding to queue: {e}")
        return None
    except Exception as e:
        logger.error(f"Error adding to queue: {type(e).__name__}: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None


def get_queue(status=None, source=None, limit=50):
    """
    Get queue items

    Args:
        status: Filter by status (queued, searching, downloading, completed, failed, imported, unmatched).
                If None, returns all active statuses (queued, searching, downloading, unmatched).
        source: Filter by source (soulseek, qbittorrent, discovered).
                If None (default), all sources are returned.
        limit: Max results

    Returns:
        List of queue items
    """
    try:
        conn = get_db()
        cursor = conn.cursor()

        # First ensure required columns exist
        cursor.execute("PRAGMA table_info(download_queue);")
        columns = [row[1] for row in cursor.fetchall()]

        # Add missing columns if needed
        missing_cols = {
            'source': "TEXT DEFAULT 'soulseek'",
            'priority': "INTEGER DEFAULT 5",
            'search_query': "TEXT",
            'import_group': "TEXT",
            'import_type': "TEXT DEFAULT 'song'"
        }

        for col, col_type in missing_cols.items():
            if col not in columns:
                logger.warning(f"'{col}' column missing from download_queue, attempting to add it")
                try:
                    cursor.execute(f"ALTER TABLE download_queue ADD COLUMN {col} {col_type};")
                    conn.commit()
                    columns.append(col)
                except Exception as e:
                    logger.warning(f"Could not add {col} column: {e}")

        from app import _is_postgres_connection as app_is_postgres_connection
        is_pg = bool(app_is_postgres_connection(conn))
        placeholder = "%s" if is_pg else "?"

        conditions = []
        params = []

        # Source filter (optional)
        if source and 'source' in columns:
            conditions.append(f"source = {placeholder}")
            params.append(source)

        # Status filter
        if status:
            conditions.append(f"status = {placeholder}")
            params.append(status)
        else:
            # Default: return all non-archived statuses
            if is_pg:
                conditions.append("status NOT IN ('imported', 'removed', 'cancelled')")
            else:
                conditions.append("status NOT IN ('imported', 'removed', 'cancelled')")

        query = "SELECT * FROM download_queue"
        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        # Only use priority in ORDER BY if column exists
        if 'priority' in columns:
            query += f" ORDER BY priority ASC, created_at DESC LIMIT {placeholder}"
        else:
            query += f" ORDER BY created_at DESC LIMIT {placeholder}"
        params.append(limit)

        cursor.execute(query, params)
        items = [dict(row) for row in cursor.fetchall()]
        conn.close()

        return items

    except Exception as e:
        logger.error(f"Error getting queue: {type(e).__name__}: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return []


def update_queue_item(queue_id, **kwargs):
    """
    Update a queue item with retry logic for database locks
    
    Args:
        queue_id: Queue item ID
        **kwargs: Fields to update (status, found_filename, file_path, failure_reason, etc.)
    
    Returns:
        Updated item dict or None
    """
    max_retries = 5
    retry_delay = 0.1
    last_error = None
    
    for attempt in range(max_retries):
        try:
            # Try app's get_db first (PostgreSQL-aware)
            is_pg = False
            try:
                from app import get_db as app_get_db, _is_postgres_connection as app_is_postgres_connection
                conn = app_get_db()
                is_pg = bool(app_is_postgres_connection(conn))
            except Exception:
                conn = get_db()
            
            cursor = conn.cursor()
            placeholder = "%s" if is_pg else "?"
            
            # Build update query
            updates = []
            params = []
            
            for key, value in kwargs.items():
                if key in ['status', 'source_id', 'found_filename', 'file_path', 'failure_reason', 
                           'retry_count', 'last_failure_time', 'imported_at', 'metadata', 'import_group', 'import_type',
                           'copied_individually', 'copied_individually_at']:
                    # Special handling for file_path to avoid UNIQUE constraint issues
                    if key == 'file_path' and value:
                        # Check if this file_path is already in use by another item
                        cursor.execute(f"SELECT COUNT(*) as cnt FROM download_queue WHERE file_path = {placeholder} AND id != {placeholder}", 
                                     (value, queue_id))
                        result = cursor.fetchone()
                        if result and result['cnt'] > 0:
                            logger.debug(f"File path {value} already in use by another queue item, skipping update")
                            continue
                    
                    updates.append(f"{key} = {placeholder}")
                    params.append(value)
            
            if not updates:
                logger.debug(f"No valid fields to update for queue item {queue_id}")
                conn.close()
                return None
            
            updates.append("updated_at = CURRENT_TIMESTAMP")
            params.append(queue_id)
            
            query = f"UPDATE download_queue SET {', '.join(updates)} WHERE id = {placeholder}"
            cursor.execute(query, params)
            conn.commit()
            
            if cursor.rowcount == 0:
                logger.debug(f"No rows updated for queue item {queue_id} - item may not exist or was already processed")
                conn.close()
                return None
            
            logger.debug(f"Updated {cursor.rowcount} row(s) for queue item {queue_id}: {list(kwargs.keys())}")
            
            # Return updated item
            cursor.execute(f"SELECT * FROM download_queue WHERE id = {placeholder}", (queue_id,))
            item = cursor.fetchone()
            conn.close()
            
            if item:
                logger.info(f"Successfully updated queue item {queue_id}: status={kwargs.get('status', 'N/A')}")
                return dict(item)
            else:
                logger.error(f"Failed to retrieve updated queue item {queue_id}")
                return None
        
        except sqlite3.OperationalError as e:
            if 'database is locked' in str(e):
                last_error = e
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 5.0)  # Exponential backoff
                    logger.warning(f"Database locked updating queue item {queue_id}, retrying (attempt {attempt + 1}/{max_retries})...")
                    continue
            logger.error(f"OperationalError updating queue item {queue_id}: {e}")
            return None
        except sqlite3.IntegrityError as e:
            logger.error(f"Database integrity error updating queue item {queue_id}: {e}")
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 5.0)
                logger.warning(f"Integrity error, retrying (attempt {attempt + 1}/{max_retries})...")
                continue
            return None
        except Exception as e:
            logger.error(f"Error updating queue item {queue_id}: {type(e).__name__}: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None
    
    if last_error:
        logger.error(f"Failed to update queue item {queue_id} after {max_retries} retries: {last_error}")
    return None


def mark_as_failed(queue_id, reason, retry_delay_minutes=30):
    """
    Mark queue item as failed and schedule retry with retry logic for database locks
    
    Args:
        queue_id: Queue item ID
        reason: Failure reason
        retry_delay_minutes: Minutes until next retry
    
    Returns:
        Updated item or None
    """
    max_retries = 5
    retry_delay = 0.1
    
    for attempt in range(max_retries):
        try:
            # Try app's get_db first (PostgreSQL-aware)
            is_pg = False
            try:
                from app import get_db as app_get_db, _is_postgres_connection as app_is_postgres_connection
                conn = app_get_db()
                is_pg = bool(app_is_postgres_connection(conn))
            except Exception:
                conn = get_db()
            
            cursor = conn.cursor()
            placeholder = "%s" if is_pg else "?"
            
            # Get current retry count
            cursor.execute(f"SELECT retry_count, max_retries FROM download_queue WHERE id = {placeholder}", (queue_id,))
            row = cursor.fetchone()
            
            if not row:
                conn.close()
                return None
            
            retry_count = (row['retry_count'] or 0) + 1
            next_retry = datetime.now() + timedelta(minutes=retry_delay_minutes)
            
            # Check if we've exceeded max retries
            if retry_count >= row['max_retries']:
                new_status = 'failed'
                logger.warning(f"Queue item {queue_id} exceeded max retries ({retry_count}/{row['max_retries']}): {reason}")
            else:
                new_status = 'queued'
                logger.info(f"Queue item {queue_id} scheduled for retry (attempt {retry_count}/{row['max_retries']}) at {next_retry}: {reason}")
            
            cursor.execute(f"""
                UPDATE download_queue 
                SET status = {placeholder}, retry_count = {placeholder}, failure_reason = {placeholder}, last_failure_time = CURRENT_TIMESTAMP, next_retry_at = {placeholder}, updated_at = CURRENT_TIMESTAMP
                WHERE id = {placeholder}
            """, (new_status, retry_count, reason, next_retry.isoformat(), queue_id))
            
            conn.commit()
            
            # Return updated item
            cursor.execute(f"SELECT * FROM download_queue WHERE id = {placeholder}", (queue_id,))
            item = cursor.fetchone()
            conn.close()
            
            return dict(item) if item else None
        
        except sqlite3.OperationalError as e:
            if 'database is locked' in str(e):
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 5.0)
                    logger.warning(f"Database locked marking failed, retrying (attempt {attempt + 1}/{max_retries})...")
                    continue
            logger.error(f"OperationalError marking failed: {e}")
            return None
        except Exception as e:
            logger.error(f"Error marking queue item as failed: {e}")
            return None
    
    logger.error(f"Failed to mark queue item {queue_id} as failed after {max_retries} retries")
    return None


def clear_queue(keep_completed=False):
    """
    Clear all items from the download queue.
    
    Args:
        keep_completed: If True, keep 'completed' and 'imported' items (only clear active/failed items)
        
    Returns:
        Dict with cleared_count and status
    """
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        if keep_completed:
            # Only clear non-completed items
            cursor.execute("DELETE FROM download_queue WHERE status NOT IN ('completed', 'imported')")
            logger.info("Cleared all active and failed queue items (kept completed/imported)")
        else:
            # Clear everything
            cursor.execute("DELETE FROM download_queue")
            logger.info("Cleared entire download queue")
        
        cleared_count = cursor.rowcount
        conn.commit()
        conn.close()
        
        return {
            "success": True,
            "cleared_count": cleared_count,
            "message": f"Cleared {cleared_count} queue item(s)"
        }
    except Exception as e:
        logger.error(f"Error clearing queue: {e}")
        return {
            "success": False,
            "message": f"Error clearing queue: {e}"
        }


def _sanitize_path_component(value):
    """Remove characters that are invalid in directory/file names."""
    if not value:
        return value
    invalid = '<>:"|?*\\'
    for ch in invalid:
        value = value.replace(ch, '_')
    return value.strip('. ')


def move_single_track_to_music_dir(queue_item_dict, music_dir=None):
    """
    Move a single completed track from /downloads into the /music library tree.

    Folder structure: <music_root>/<album_artist>/<year> - <album>/
    (year omitted when not available)

    Args:
        queue_item_dict: dict from download_queue row with at least file_path,
                         artist, album, album_artist, year, title.
        music_dir:       Optional override for MUSIC_ROOT (defaults to env var).

    Returns:
        dict with keys:
            success  (bool)
            target_path (str | None)
            error (str | None)
    """
    import shutil

    try:
        file_path = queue_item_dict.get('file_path')
        if not file_path:
            return {'success': False, 'target_path': None, 'error': 'No file_path in queue item'}
        if not os.path.exists(file_path):
            return {'success': False, 'target_path': None, 'error': f'File not found: {file_path}'}

        music_root = music_dir or MUSIC_DIR

        album_artist = _sanitize_path_component(
            queue_item_dict.get('album_artist') or queue_item_dict.get('artist') or 'Unknown Artist'
        )
        album = _sanitize_path_component(queue_item_dict.get('album') or 'Unknown Album')
        year = queue_item_dict.get('year')

        if year:
            dest_folder = os.path.join(music_root, album_artist, f"{year} - {album}")
        else:
            dest_folder = os.path.join(music_root, album_artist, album)

        os.makedirs(dest_folder, exist_ok=True)

        filename = os.path.basename(file_path)
        dest_path = os.path.join(dest_folder, filename)

        # Avoid overwriting
        if os.path.exists(dest_path):
            base, ext = os.path.splitext(filename)
            counter = 1
            while os.path.exists(os.path.join(dest_folder, f"{base}_{counter}{ext}")):
                counter += 1
            dest_path = os.path.join(dest_folder, f"{base}_{counter}{ext}")

        shutil.move(file_path, dest_path)
        logger.info(f"[MOVE] {filename} → {dest_path}")
        return {'success': True, 'target_path': dest_path, 'error': None}

    except Exception as e:
        logger.error(f"[MOVE] Failed to move file: {e}")
        return {'success': False, 'target_path': None, 'error': str(e)}


def _metadata_matches_queue_item(file_meta, queue_item, threshold=0.6):
    """
    Check if discovered file metadata is a good match for a pending queue item.

    Compares artist + title (required), with album as a bonus.
    Returns True when the similarity exceeds `threshold`.
    """
    def _sim(a, b):
        if not a or not b:
            return 0.0
        return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()

    artist_score = _sim(file_meta.get('artist'), queue_item.get('artist'))
    title_score = _sim(file_meta.get('title'), queue_item.get('title'))

    # Each individual field must clear a minimum similarity floor (0.5) before
    # the weighted average is tested against the overall threshold parameter.
    _FIELD_MIN = 0.5
    if artist_score < _FIELD_MIN or title_score < _FIELD_MIN:
        return False

    combined = (artist_score + title_score) / 2

    # Album similarity gives a small boost if available
    album_score = _sim(file_meta.get('album'), queue_item.get('album'))
    if album_score > 0:
        combined = (combined * 2 + album_score) / 3

    return combined >= threshold


def check_downloads_folder():
    """
    Monitor /downloads folder for completed files.
    Match files to queue items and update their status.
    Automatically moves matched files to /music and marks them as 'imported'.

    Returns:
        List of newly completed items
    """
    try:
        downloads_dir = get_downloads_dir()
        if not os.path.isdir(downloads_dir):
            logger.warning(f"Downloads folder not found: {downloads_dir}")
            return []
        
        completed_items = []
        conn = get_db()
        cursor = conn.cursor()
        
        # Get all active queue items (not yet completed or imported)
        cursor.execute("""
            SELECT * FROM download_queue 
            WHERE status IN ('queued', 'searching', 'downloading')
            ORDER BY created_at ASC
        """)
        queue_items = [dict(row) for row in cursor.fetchall()]
        
        # Recursively get all audio files in downloads folder and subdirectories
        downloads_files = []
        if os.path.isdir(downloads_dir):
            try:
                for root, dirs, files in os.walk(downloads_dir):
                    for f in files:
                        if f.endswith(('.mp3', '.flac', '.m4a', '.ogg', '.wav')):
                            # Store both filename and full path
                            downloads_files.append({
                                'filename': f,
                                'full_path': os.path.join(root, f),
                                'rel_path': os.path.relpath(os.path.join(root, f), downloads_dir)
                            })
            except Exception as e:
                logger.error(f"Error scanning downloads folder: {e}")
        
        logger.info(f"Found {len(downloads_files)} audio files in {downloads_dir}, checking {len(queue_items)} queue items")
        
        # Try to match files to queue items
        for queue_item in queue_items:
            match_found = None
            match_path = None
            
            # Try exact filename match first
            if queue_item['found_filename']:
                for file_info in downloads_files:
                    if file_info['filename'] == queue_item['found_filename'] or \
                       file_info['rel_path'] == queue_item['found_filename'] or \
                       file_info['full_path'] == queue_item['found_filename']:
                        match_found = file_info['filename']
                        match_path = file_info['full_path']
                        break
            
            # If not found by filename, try fuzzy matching based on artist/title
            if not match_found:
                for file_info in downloads_files:
                    if is_match(file_info['rel_path'], queue_item):
                        match_found = file_info['filename']
                        match_path = file_info['full_path']
                        logger.debug(f"Fuzzy matched '{queue_item['search_query']}' to '{file_info['rel_path']}'")
                        break
            
            if match_found and match_path:
                logger.info(f"Matched queue {queue_item['id']} ({queue_item['search_query']}) to file: {match_found}")

                # First mark as completed so the item has file_path set
                updated_item = update_queue_item(
                    queue_item['id'],
                    status='completed',
                    found_filename=match_found,
                    file_path=match_path,
                    imported_at=datetime.now().isoformat()
                )

                if updated_item:
                    # Immediately move the file to /music
                    item_for_move = dict(queue_item)
                    item_for_move['file_path'] = match_path
                    move_result = move_single_track_to_music_dir(item_for_move)
                    if move_result['success']:
                        # Update to 'imported' with the new /music path
                        update_queue_item(
                            queue_item['id'],
                            status='imported',
                            file_path=move_result['target_path'],
                            copied_individually=1,
                            copied_individually_at=datetime.now().isoformat()
                        )
                        logger.info(
                            f"[MOVE] Queue {queue_item['id']}: moved to {move_result['target_path']}"
                        )
                        completed_items.append({
                            'queue_id': queue_item['id'],
                            'filename': match_found,
                            'file_path': move_result['target_path'],
                            'artist': queue_item['artist'],
                            'title': queue_item['title'],
                            'album': queue_item['album'],
                            'moved': True
                        })
                    else:
                        logger.warning(
                            f"[MOVE] Queue {queue_item['id']}: could not move file "
                            f"({move_result.get('error')}), keeping as 'completed'"
                        )
                        completed_items.append({
                            'queue_id': queue_item['id'],
                            'filename': match_found,
                            'file_path': match_path,
                            'artist': queue_item['artist'],
                            'title': queue_item['title'],
                            'album': queue_item['album'],
                            'moved': False
                        })
                else:
                    logger.debug(f"Could not update queue item {queue_item['id']} - item may have been processed already")
            else:
                # Debug: show what we're looking for
                search_query = queue_item.get('search_query', f"{queue_item.get('artist', '')} {queue_item.get('title', '')}")
                logger.debug(f"No match found for queue item {queue_item['id']}: {search_query}")

        conn.close()

        if completed_items:
            logger.info(f"Found {len(completed_items)} completed downloads")

        return completed_items
        
    except Exception as e:
        logger.error(f"Error checking downloads folder: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return []


def is_match(filename, queue_item):
    """
    Check if a filename matches a queue item
    Uses fuzzy matching on artist, album, and title
    
    Args:
        filename: Filename or relative path to check
        queue_item: Queue item dict
    
    Returns:
        bool - True if filename likely matches queue item
    """
    try:
        # Normalize path separators to forward slashes for consistent matching
        filename_test = filename.lower().replace('\\', '/')
        
        artist = (queue_item['artist'] or '').lower()
        title = (queue_item['title'] or '').lower()
        album = (queue_item['album'] or '').lower()
        search_query = (queue_item.get('search_query') or '').lower()
        
        # Count how many search terms are in the filename/path
        matches = 0
        total_terms = 0
        
        # Check artist match (most important)
        if artist:
            total_terms += 1
            if artist in filename_test:
                matches += 1
        
        # Check title match
        if title:
            total_terms += 1
            if title in filename_test:
                matches += 1
        
        # Check album match (less important, but helpful for disambiguation)
        if album:
            total_terms += 1
            # Remove spaces for more flexible album name matching
            album_normalized = album.replace(' ', '')
            if album_normalized and album_normalized in filename_test.replace(' ', ''):
                matches += 1
        
        # Require at least majority of terms to match
        if total_terms > 0 and matches >= (total_terms * 0.6):  # 60% match threshold
            return True
        
        # Also try matching against search_query if set
        if search_query:
            # Split search query into individual terms and check for significant overlap
            search_terms = [t for t in search_query.split() if len(t) > 2]  # Only meaningful terms
            if search_terms:
                term_matches = sum(1 for term in search_terms if term in filename_test)
                # Require at least 60% of significant terms to match
                if term_matches / len(search_terms) >= 0.6:
                    return True
        
        return False
        
    except Exception as e:
        logger.error(f"Error matching filename {filename}: {e}")
        return False


def auto_discover_and_queue_files():
    """
    Scan /downloads folder for audio files and add them to download_queue with status 'discovered'.
    This makes manually added/downloaded files appear in the Download Monitor UI for user review.
    
    Only adds files that:
    - Are valid audio files (.mp3, .flac, .m4a, .ogg, .wav)
    - Are not already in the download_queue table
    - Are not already in the tracks table (existing library)
    
    Returns:
        Dict with statistics:
        - scanned: Total audio files found
        - queued: Files added to queue
        - already_in_queue: Files that were already queued
        - already_in_library: Files that exist in library
        - errors: List of error messages
    """
    stats = {
        'scanned': 0,
        'queued': 0,
        'already_in_queue': 0,
        'already_in_library': 0,
        'errors': []
    }
    
    try:
        downloads_dir = get_downloads_dir()
        logger.info(f"[AUTO-DISCOVER] Starting scan of: {downloads_dir}")
        logger.info(f"[AUTO-DISCOVER] Directory exists: {os.path.isdir(downloads_dir)}")
        logger.info(f"[AUTO-DISCOVER] Is absolute path: {os.path.isabs(downloads_dir)}")
        
        # Initialize scan progress tracking
        update_scan_progress(scanning=True, files_found=0)
        
        if not os.path.isdir(downloads_dir):
            error_msg = f"Downloads folder not found or not accessible: {downloads_dir} (exists={os.path.exists(downloads_dir)})"
            logger.error(f"[AUTO-DISCOVER] {error_msg}")
            stats['errors'].append(error_msg)
            update_scan_progress(scanning=False)
            return stats
        
        # Clean up queue items for files that no longer exist
        cleanup_stats = cleanup_missing_files()
        if cleanup_stats['removed'] > 0:
            logger.info(f"[AUTO-DISCOVER] Cleanup: Removed {cleanup_stats['removed']} queue items with missing files")
        stats['cleanup_removed'] = cleanup_stats['removed']
        
        conn = get_db()
        cursor = conn.cursor()
        
        # Ensure required columns exist for auto-discovery inserts
        cursor.execute("PRAGMA table_info(download_queue);")
        columns = [row[1] for row in cursor.fetchall()]
        
        required_cols = {
            'track_number': "TEXT",
            'disc_number': "TEXT",
            'album_artist': "TEXT",
            'year': "TEXT",
            'found_filename': "TEXT"
        }
        
        for col, col_type in required_cols.items():
            if col not in columns:
                logger.info(f"[AUTO-DISCOVER] Adding missing column '{col}' to download_queue")
                try:
                    cursor.execute(f"ALTER TABLE download_queue ADD COLUMN {col} {col_type};")
                    conn.commit()
                except Exception as e:
                    logger.warning(f"[AUTO-DISCOVER] Could not add {col} column: {e}")
        
        # Get all audio files from downloads folder and subdirectories
        audio_extensions = {'.mp3', '.flac', '.m4a', '.ogg', '.wav'}
        discovered_files = []
        
        try:
            for root, dirs, files in os.walk(downloads_dir):
                logger.debug(f"[AUTO-DISCOVER] Scanning: {root} ({len(files)} files)")
                for filename in files:
                    file_ext = os.path.splitext(filename)[1].lower()
                    if file_ext in audio_extensions:
                        full_path = os.path.join(root, filename)
                        rel_path = os.path.relpath(full_path, downloads_dir)
                        discovered_files.append({
                            'filename': filename,
                            'full_path': full_path,
                            'rel_path': rel_path
                        })
                        logger.debug(f"[AUTO-DISCOVER] Found audio file: {rel_path}")
        except Exception as e:
            error_msg = f"Error scanning folder: {e}"
            logger.error(f"[AUTO-DISCOVER] {error_msg}")
            stats['errors'].append(error_msg)
            return stats
        
        stats['scanned'] = len(discovered_files)
        logger.info(f"[AUTO-DISCOVER] Scanning {len(discovered_files)} audio files in {downloads_dir}")
        
        if len(discovered_files) == 0:
            global _last_no_audio_log_at
            now_ts = time.time()
            if (now_ts - _last_no_audio_log_at) >= _NO_AUDIO_LOG_INTERVAL_SECONDS:
                logger.info(
                    f"[AUTO-DISCOVER] No audio files found in {downloads_dir} "
                    f"(this message is throttled to once every {_NO_AUDIO_LOG_INTERVAL_SECONDS}s)"
                )
                _last_no_audio_log_at = now_ts
            else:
                logger.debug(f"[AUTO-DISCOVER] No audio files found in {downloads_dir}")
            update_scan_progress(scanning=False)
            return stats
        
        for file_info in discovered_files:
            try:
                full_path = file_info['full_path']
                filename = file_info['filename']
                file_ext = os.path.splitext(filename)[1].lower()
                
                # Extract metadata from file
                metadata = {}
                had_metadata_error = False
                try:
                    metadata = read_mp3_metadata(full_path)
                except Exception as e:
                    logger.warning(f"Could not read metadata from {filename}: {e}")
                    had_metadata_error = True
                
                # Extract fields with fallbacks to filename
                artist = metadata.get('artist', 'Unknown Artist')
                album = metadata.get('album', 'Unknown Album')
                title = metadata.get('title', os.path.splitext(filename)[0])
                album_artist = metadata.get('album_artist') or artist
                track_number = metadata.get('track_number')
                disc_number = metadata.get('disc_number')
                year = metadata.get('date') or metadata.get('year')
                
                # Log metadata extraction status
                if metadata and not had_metadata_error:
                    logger.debug(f"✅ Metadata read: {artist} - {album} - {title}")
                else:
                    logger.info(f"⚠️  No metadata found (using filename): {filename} → {artist} - {title}")
                
                # Update progress tracking
                file_info_for_progress = {
                    'rel_path': file_info['rel_path'],
                    'artist': artist,
                    'title': title,
                    'album': album,
                    'found_at': datetime.now().isoformat()
                }
                update_scan_progress(
                    files_found=stats['scanned'],
                    recent_file=file_info_for_progress
                )
                
                # Check if already in download_queue
                from app import _is_postgres_connection as app_is_postgres_connection
                is_pg = bool(app_is_postgres_connection(conn))
                placeholder = "%s" if is_pg else "?"
                cursor.execute(f"""
                    SELECT id, status FROM download_queue 
                    WHERE (file_path = {placeholder} OR found_filename = {placeholder})
                """, (full_path, filename))
                
                existing = cursor.fetchone()
                if existing:
                    stats['already_in_queue'] += 1
                    logger.debug(f"File already in queue (ID {existing['id']}, status {existing['status']}): {filename}")
                    continue
                
                # Check if track exists in library (case-insensitive)
                cursor.execute(f"""
                    SELECT id FROM tracks 
                    WHERE LOWER(artist) = LOWER({placeholder}) 
                    AND LOWER(album) = LOWER({placeholder}) 
                    AND LOWER(title) = LOWER({placeholder})
                """, (artist, album, title))
                
                in_library = cursor.fetchone()
                if in_library:
                    stats['already_in_library'] += 1
                    logger.debug(f"Track already in library: {artist} - {title}")
                    
                    # Still add to queue with status 'completed' so it appears in Completed & Ready to Organize
                    execute_write_with_retry(
                        cursor,
                        conn,
                        f"""
                        INSERT INTO download_queue 
                        (artist, title, album, album_artist, track_number, disc_number, year, found_filename, file_path, 
                         status, source, created_at, updated_at)
                        VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, 'completed', 'discovered', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """,
                        (artist, title, album, album_artist, track_number, disc_number, year, filename, full_path),
                        context="auto_discover in-library insert"
                    )

                    stats['queued'] += 1
                    continue

                # Check if this file matches a pending queue item (queued/searching/downloading)
                file_meta = {
                    'artist': artist,
                    'title': title,
                    'album': album,
                }
                cursor.execute(f"""
                    SELECT id, artist, title, album, album_artist, year, track_number, disc_number
                    FROM download_queue
                    WHERE status IN ('queued', 'searching', 'downloading')
                """)
                pending_items = [dict(row) for row in cursor.fetchall()]

                matched_pending = None
                for pending in pending_items:
                    if _metadata_matches_queue_item(file_meta, pending):
                        matched_pending = pending
                        break

                if matched_pending:
                    # File belongs to an existing queue item - update it and move to /music
                    item_for_move = dict(matched_pending)
                    item_for_move['file_path'] = full_path
                    # Enrich with discovered file's metadata where queue item is sparse
                    if not item_for_move.get('album_artist'):
                        item_for_move['album_artist'] = album_artist
                    if not item_for_move.get('year'):
                        item_for_move['year'] = year

                    updated = update_queue_item(
                        matched_pending['id'],
                        status='completed',
                        found_filename=filename,
                        file_path=full_path,
                        imported_at=datetime.now().isoformat()
                    )
                    if updated:
                        move_result = move_single_track_to_music_dir(item_for_move)
                        if move_result['success']:
                            update_queue_item(
                                matched_pending['id'],
                                status='imported',
                                file_path=move_result['target_path'],
                                copied_individually=1,
                                copied_individually_at=datetime.now().isoformat()
                            )
                            logger.info(
                                f"[AUTO-DISCOVER] Matched & moved: {artist} - {title} "
                                f"→ {move_result['target_path']}"
                            )
                        else:
                            logger.warning(
                                f"[AUTO-DISCOVER] Matched but could not move {filename}: "
                                f"{move_result.get('error')}"
                            )
                    stats['queued'] += 1
                    continue

                # No pending queue item matches → add as 'unmatched'
                execute_write_with_retry(
                    cursor,
                    conn,
                    f"""
                    INSERT INTO download_queue 
                    (artist, title, album, album_artist, track_number, disc_number, year, found_filename, file_path, 
                     status, source, created_at, updated_at)
                    VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, 'unmatched', 'discovered', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                    (artist, title, album, album_artist, track_number, disc_number, year, filename, full_path),
                    context="auto_discover unmatched insert"
                )

                stats['queued'] += 1

                # Log discovery with metadata and format info
                metadata_status = "✓ metadata" if metadata else "✗ fallback"
                if file_ext == '.flac':
                    logger.info(f"⚠️  Unmatched [FLAC] [{metadata_status}]: {artist} - {title}")
                else:
                    logger.info(f"⚠️  Unmatched [{metadata_status}]: {artist} - {title} from {os.path.basename(os.path.dirname(full_path))}/{filename}")
                
            except Exception as e:
                error_msg = f"Error processing {file_info['filename']}: {str(e)}"
                logger.error(error_msg)
                stats['errors'].append(error_msg)
        
        conn.close()
        
        logger.info(f"Auto-discovery complete: {stats['queued']} files added to queue, "
                   f"{stats['already_in_queue']} already queued, "
                   f"{stats['already_in_library']} in library")
        
        # Mark scan as complete
        update_scan_progress(scanning=False, files_found=stats['scanned'])
        
        # Check for complete albums and auto-process them
        if stats['queued'] > 0:
            logger.info("Checking for complete albums to auto-process...")
            album_stats = process_complete_albums()
            stats['albums_processed'] = album_stats.get('processed', 0)
            stats['albums_duplicates'] = album_stats.get('duplicates_found', 0)
            
            if album_stats.get('processed') or album_stats.get('duplicates_found'):
                logger.info(f"Album processing: {album_stats['processed']} auto-processed, "
                           f"{album_stats['duplicates_found']} marked as duplicates")
        
        return stats
        
    except Exception as e:
        error_msg = f"Error during auto-discovery: {str(e)}"
        logger.error(error_msg)
        import traceback
        logger.error(traceback.format_exc())
        stats['errors'].append(error_msg)
        update_scan_progress(scanning=False)
        return stats


def get_retry_queue(limit=50):
    """
    Get items ready for retry
    
    Returns:
        List of queue items ready to retry
    """
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT * FROM download_queue 
            WHERE status = 'queued' 
            AND (next_retry_at IS NULL OR next_retry_at <= CURRENT_TIMESTAMP)
            AND retry_count < max_retries
            ORDER BY priority ASC, next_retry_at ASC
            LIMIT ?
        """, (limit,))
        
        items = [dict(row) for row in cursor.fetchall()]
        conn.close()
        
        return items
        
    except Exception as e:
        logger.error(f"Error getting retry queue: {e}")
        return []


def get_completed_queue(limit=50):
    """
    Get completed downloads (and unmatched files) waiting for organization.

    Includes items with status 'completed' or 'unmatched' that have a file_path.

    Returns:
        List of completed/unmatched queue items
    """
    try:
        conn = get_db()
        cursor = conn.cursor()

        from app import _is_postgres_connection as app_is_postgres_connection
        is_pg = bool(app_is_postgres_connection(conn))
        placeholder = "%s" if is_pg else "?"

        cursor.execute(f"""
            SELECT * FROM download_queue 
            WHERE status IN ('completed', 'unmatched')
            AND file_path IS NOT NULL
            ORDER BY updated_at DESC
            LIMIT {placeholder}
        """, (limit,))

        items = [dict(row) for row in cursor.fetchall()]
        conn.close()

        return items

    except Exception as e:
        logger.error(f"Error getting completed queue: {e}")
        return []


def cleanup_missing_files():
    """
    Remove queue items where the file no longer exists in /downloads.
    
    This cleanup function:
    - Checks all queue items with file_path set
    - Removes items where file no longer exists (file was deleted/moved manually)
    - Keeps items without file_path (still downloading or queued)
    
    Returns:
        Dict with statistics:
        - checked: Total items checked
        - removed: Items removed due to missing files
        - errors: List of error messages
    """
    stats = {
        'checked': 0,
        'removed': 0,
        'errors': []
    }
    
    max_retries = 5
    initial_delay = 0.2

    for attempt in range(max_retries):
        conn = None
        try:
            # Try to use the app's PostgreSQL-aware DB connection
            is_pg = False
            pg_configured = bool(os.environ.get("PG_HOST") and os.environ.get("PG_USER") and os.environ.get("PG_DATABASE"))
            try:
                from app import get_db as app_get_db, _is_postgres_connection as app_is_postgres_connection
                conn = app_get_db()
                is_pg = bool(app_is_postgres_connection(conn))
            except Exception:
                if pg_configured:
                    raise RuntimeError("PostgreSQL is configured, but cleanup could not connect")
                conn = get_db()
            
            cursor = conn.cursor()
            
            # Get all queue items with file paths
            cursor.execute("""
                SELECT id, file_path, artist, title, status
                FROM download_queue
                WHERE file_path IS NOT NULL AND file_path != ''
            """)

            items = cursor.fetchall()
            stats['checked'] = len(items)

            if not items:
                return stats

            removed_ids = []

            for item in items:
                queue_id = item['id']
                file_path = item['file_path']
                status = item['status']

                # Check if file exists
                if not os.path.exists(file_path):
                    logger.info(f"Removing queue item {queue_id} (status: {status}): File no longer exists: {file_path}")
                    removed_ids.append(queue_id)

            # Remove items in batch
            if removed_ids:
                placeholder = "%s" if is_pg else "?"
                placeholders = ','.join([placeholder] * len(removed_ids))
                cursor.execute(f"DELETE FROM download_queue WHERE id IN ({placeholders})", removed_ids)
                conn.commit()
                stats['removed'] = len(removed_ids)
                logger.info(f"Cleaned up {len(removed_ids)} queue items with missing files")

            return stats

        except (sqlite3.OperationalError, Exception) as e:
            if 'database is locked' in str(e).lower():
                if attempt < max_retries - 1:
                    delay = initial_delay * (2 ** attempt)
                    logger.warning(
                        f"Database locked during cleanup_missing_files(), retrying in {delay:.2f}s "
                        f"(attempt {attempt + 1}/{max_retries})"
                    )
                    time.sleep(delay)
                    continue
                error_msg = f"Error during cleanup: database is locked after {max_retries} retries"
                logger.error(error_msg)
                stats['errors'].append(error_msg)
                return stats
            error_msg = f"Error during cleanup: {str(e)}"
            logger.error(error_msg)
            stats['errors'].append(error_msg)
            return stats

        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass


def check_and_remove_failed_downloads():
    """
    Monitor active Soulseek downloads and detect failed ones.
    Remove failed downloads and mark for retry.
    
    A failed download is detected when:
    - State contains "TimedOut" or "Failed"
    - Bytes transferred is 0 or very low (<1% of file size)
    - Download has been stuck for >5 minutes
    
    Returns:
        Dict with statistics of failed downloads detected and removed
    """
    stats = {
        "total_active": 0,
        "failed_detected": 0,
        "retry_scheduled": 0,
        "errors": []
    }
    
    try:
        # Try to import SlskdClient
        try:
            from api_clients.slskd import SlskdClient
            import os as os_module
        except ImportError:
            logger.warning("SlskdClient not available for download failure detection")
            return stats
        
        # Get slskd config from app config
        try:
            import yaml
            config_path = os_module.environ.get("CONFIG_PATH", "/config/config.yaml")
            if os_module.path.exists(config_path):
                with open(config_path, 'r') as f:
                    config = yaml.safe_load(f) or {}
                    slskd_config = config.get('slskd', {})
                    if not slskd_config.get('enabled'):
                        logger.debug("Soulseek not enabled, skipping failure detection")
                        return stats
            else:
                logger.debug("Config file not found, skipping failure detection")
                return stats
        except Exception as e:
            logger.warning(f"Could not load slskd config: {e}")
            return stats
        
        # Initialize slskd client
        web_url = slskd_config.get('web_url', 'http://localhost:5030')
        api_key = slskd_config.get('api_key', '')
        
        client = SlskdClient(web_url, api_key, enabled=True)
        
        # Get active downloads
        downloads = client.get_active_downloads()
        stats["total_active"] = len(downloads)
        
        logger.info(f"Checking {len(downloads)} active downloads for failures")
        
        conn = get_db()
        cursor = conn.cursor()
        
        for download in downloads:
            try:
                username = download.get("username", "")
                filename = download.get("filename", "")
                size = download.get("size", 0)
                bytes_transferred = download.get("bytesTransferred", 0)
                state = download.get("state", "")
                progress = download.get("progress", 0)
                
                # Check for failure conditions
                is_timed_out = "TimedOut" in state or "Timeout" in state or "timeout" in state
                is_failed = "Failed" in state or "Error" in state
                is_no_progress = bytes_transferred == 0 and state != "Initializing"
                
                if is_timed_out or (is_failed and is_no_progress) or (is_timed_out and is_no_progress):
                    logger.warning(f"Failed download detected: {filename} from {username} (state={state}, progress={progress}%, bytes={bytes_transferred}/{size})")
                    
                    # Try to cancel the download in slskd
                    try:
                        response = client.session.delete(
                            f"{client.base_url}/transfers/downloads/{username}/{filename.replace('/', '%2F')}",
                            headers=client.headers,
                            timeout=10
                        )
                        logger.info(f"Cancelled failed download: {filename} (response: {response.status_code})")
                    except Exception as cancel_error:
                        logger.warning(f"Could not cancel download {filename}: {cancel_error}")
                    
                    # Find matching queue item and mark for retry
                    cursor.execute("""
                        SELECT id FROM download_queue 
                        WHERE source = 'soulseek' 
                        AND status IN ('downloading', 'searching')
                        AND (found_filename = ? OR search_query LIKE ?)
                        LIMIT 1
                    """, (filename, f"%{filename.rsplit('/', 1)[-1]}%"))
                    
                    queue_item = cursor.fetchone()
                    if queue_item:
                        queue_id = queue_item['id']
                        logger.info(f"Marking queue item {queue_id} for retry (failed download)")
                        
                        # Mark as failed to trigger retry
                        mark_as_failed(
                            queue_id, 
                            f"Download failed: {state} (0 progress)", 
                            retry_delay_minutes=5
                        )
                        stats["retry_scheduled"] += 1
                    
                    stats["failed_detected"] += 1
                    
            except Exception as e:
                error_msg = f"Error processing download result: {e}"
                logger.error(error_msg)
                stats["errors"].append(error_msg)
        
        conn.close()
        
        if stats["failed_detected"] > 0:
            logger.info(f"Failed download detection complete: {stats['failed_detected']} failed, {stats['retry_scheduled']} retries scheduled")
        
        return stats
        
    except Exception as e:
        error_msg = f"Error in check_and_remove_failed_downloads: {e}"
        logger.error(error_msg)
        stats["errors"].append(error_msg)
        return stats


def cleanup_imported(days=7):
    """
    Remove imported items older than X days to keep queue clean
    
    Args:
        days: Days to keep imported items
    
    Returns:
        Number of items removed
    """
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cutoff_date = (datetime.now() - timedelta(days=days)).isoformat()
        
        execute_write_with_retry(
            cursor,
            conn,
            """
            DELETE FROM download_queue 
            WHERE status = 'imported' 
            AND imported_at < ?
        """,
            (cutoff_date,),
            context="cleanup_imported delete"
        )

        removed = cursor.rowcount
        conn.close()
        
        if removed > 0:
            logger.info(f"Cleaned up {removed} old imported queue items")
        
        return removed
        
    except Exception as e:
        logger.error(f"Error cleaning up queue: {e}")
        return 0


def check_album_exists_in_library(album, artist):
    """
    Check if an album already exists in the tracks database
    
    Args:
        album: Album name
        artist: Artist or album artist name
    
    Returns:
        bool: True if album exists in library, False otherwise
    """
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Check if any tracks from this album/artist combo exist
        from app import _is_postgres_connection as app_is_postgres_connection
        is_pg = bool(app_is_postgres_connection(conn))
        placeholder = "%s" if is_pg else "?"
        cursor.execute(f"""
            SELECT COUNT(*) as count FROM tracks 
            WHERE LOWER(album) = LOWER({placeholder}) 
            AND (LOWER(artist) = LOWER({placeholder}) OR LOWER(album_artist) = LOWER({placeholder}))
        """, (album, artist, artist))
        
        result = cursor.fetchone()
        conn.close()
        
        return result['count'] > 0 if result else False
        
    except Exception as e:
        logger.error(f"Error checking if album exists: {e}")
        return False


def _normalize_match_text(value):
    """Normalize text for fuzzy matching across providers."""
    if not value:
        return ""
    normalized = value.lower().strip()
    replacements = {
        "&": "and",
        "’": "'",
        "`": "'",
        "-": " ",
        "_": " ",
        "/": " ",
        "(": " ",
        ")": " ",
        "[": " ",
        "]": " ",
    }
    for src, dst in replacements.items():
        normalized = normalized.replace(src, dst)
    normalized = " ".join(normalized.split())
    return normalized


def _string_similarity(a, b):
    """Return normalized string similarity score (0-1)."""
    return SequenceMatcher(None, _normalize_match_text(a), _normalize_match_text(b)).ratio()


def _extract_lyricist_and_writers(recording_data):
    """
    Extract lyricist and writer information from a MusicBrainz recording.
    
    MusicBrainz organizes credits through relationships on works and recordings.
    This function extracts:
    - Lyricists (from work relationships with type="lyricist")
    - Composers (from work relationships with type="composer")
    - Generic writers (from work relationships with type="writer")
    - Track-level contributors (from recording relationships)
    
    Args:
        recording_data: Recording dict from MusicBrainz API response
        
    Returns:
        dict with keys: lyricists, composers, writers (all are lists of artist names)
        Example: {
            "lyricists": ["Lyricist Name"],
            "composers": ["Composer Name"],
            "writers": ["General Writer Name"]
        }
    """
    result = {
        "lyricists": [],
        "composers": [],
        "writers": []
    }
    
    if not recording_data:
        return result
    
    # Extract from work relationships if present
    # Works contain relationships to lyricists, composers, and writers
    work_relationships = recording_data.get("work-level-rels", []) or []
    if not work_relationships and "relations" in recording_data:
        # Fallback: check relations for work type
        for rel in recording_data.get("relations", []):
            if rel.get("target-type") == "work":
                work_relationships.append(rel)
    
    for work_rel in work_relationships:
        # Extract from work's own relationships
        work = work_rel.get("work", {})
        relations = work.get("relations", []) or []
        
        for rel in relations:
            rel_type = rel.get("type", "").lower()
            artist_credit = rel.get("artist-credit", [{}])[0] if rel.get("artist-credit") else {}
            artist_name = artist_credit.get("name") or rel.get("artist", {}).get("name")
            
            if not artist_name:
                continue
                
            if rel_type == "lyricist":
                if artist_name not in result["lyricists"]:
                    result["lyricists"].append(artist_name)
            elif rel_type == "composer":
                if artist_name not in result["composers"]:
                    result["composers"].append(artist_name)
            elif rel_type in ("writer", "text"):
                if artist_name not in result["writers"]:
                    result["writers"].append(artist_name)
    
    # Also check recording-level relationships for credit information
    recording_rels = recording_data.get("relations", []) or []
    for rel in recording_rels:
        rel_type = rel.get("type", "").lower()
        if rel_type not in ("lyricist", "composer", "writer", "text"):
            continue
            
        # Extract artist from relationship
        artist_credit = rel.get("artist-credit", [{}])[0] if rel.get("artist-credit") else {}
        artist_name = artist_credit.get("name") or rel.get("artist", {}).get("name")
        
        if not artist_name:
            continue
            
        if rel_type == "lyricist":
            if artist_name not in result["lyricists"]:
                result["lyricists"].append(artist_name)
        elif rel_type == "composer":
            if artist_name not in result["composers"]:
                result["composers"].append(artist_name)
        elif rel_type in ("writer", "text"):
            if artist_name not in result["writers"]:
                result["writers"].append(artist_name)
    
    return result


def _fetch_musicbrainz_album_candidates(artist, album, limit=5):
    """Fetch album candidates from MusicBrainz release groups."""
    headers = {
        "User-Agent": "sptnr/2.0.0-alpha ( https://github.com/M0VENTURA/sptnr )"
    }
    query = f'releasegroup:"{album}" AND artist:"{artist}"'

    search_resp = session.get(
        "https://musicbrainz.org/ws/2/release-group/",
        params={"query": query, "fmt": "json", "limit": limit},
        headers=headers,
        timeout=10
    )
    search_resp.raise_for_status()

    candidates = []
    for rg in search_resp.json().get("release-groups", [])[:limit]:
        rg_id = rg.get("id")
        if not rg_id:
            continue

        title = rg.get("title") or ""
        primary_type = rg.get("primary-type") or ""
        first_release_date = rg.get("first-release-date") or ""
        year = first_release_date[:4] if first_release_date else None
        artist_credit = " ".join([(ac.get("name") or "") for ac in (rg.get("artist-credit") or [])]).strip() or artist

        release_tracks = []
        release_resp = session.get(
            f"https://musicbrainz.org/ws/2/release-group/{rg_id}/releases",
            params={"fmt": "json", "limit": 1},
            headers=headers,
            timeout=10
        )
        if release_resp.ok:
            releases = release_resp.json().get("releases", [])
            if releases:
                rel_id = releases[0].get("id")
                if rel_id:
                    track_resp = session.get(
                        f"https://musicbrainz.org/ws/2/release/{rel_id}",
                        params={"fmt": "json", "inc": "recordings+artist-credits+work-level-rels+work-rels+artist-rels"},
                        headers=headers,
                        timeout=10
                    )
                    if track_resp.ok:
                        rel_json = track_resp.json()
                        media = rel_json.get("media", [])
                        for medium in media:
                            for tr in medium.get("tracks", []):
                                recording = tr.get("recording", {})
                                track_title = (recording.get("title") or tr.get("title") or "").strip()
                                if track_title:
                                    release_tracks.append(track_title)

        candidates.append({
            "release_group_id": rg_id,
            "title": title,
            "artist": artist_credit,
            "primary_type": primary_type,
            "year": year,
            "track_titles": release_tracks,
            "total_tracks": len(release_tracks),
        })

    return candidates


def _score_album_candidates(artist, album, discovered_tracks, candidates):
    """Score MusicBrainz candidates and determine exact/possible matches."""
    discovered_titles = [t.get("title") or "" for t in discovered_tracks]
    normalized_discovered = {_normalize_match_text(t) for t in discovered_titles if t}

    scored = []
    for cand in candidates:
        cand_tracks = cand.get("track_titles") or []
        normalized_cand_tracks = {_normalize_match_text(t) for t in cand_tracks if t}

        title_similarity = _string_similarity(album, cand.get("title") or "")
        artist_similarity = _string_similarity(artist, cand.get("artist") or "")

        matched_tracks = len(normalized_discovered.intersection(normalized_cand_tracks)) if normalized_cand_tracks else 0
        overlap = (matched_tracks / max(len(normalized_discovered), 1)) if normalized_discovered else 0.0
        count_similarity = 1.0 if cand.get("total_tracks") == len(discovered_tracks) else max(
            0.0,
            1.0 - (abs((cand.get("total_tracks") or 0) - len(discovered_tracks)) / max(len(discovered_tracks), 1))
        )

        confidence = (title_similarity * 0.4) + (artist_similarity * 0.2) + (overlap * 0.3) + (count_similarity * 0.1)

        is_exact = (
            title_similarity >= 0.99 and
            artist_similarity >= 0.95 and
            overlap >= 0.99 and
            (cand.get("total_tracks") or 0) == len(discovered_tracks)
        )

        scored.append({
            **cand,
            "matched_tracks": matched_tracks,
            "discovered_tracks": len(discovered_tracks),
            "title_similarity": round(title_similarity, 3),
            "artist_similarity": round(artist_similarity, 3),
            "track_overlap": round(overlap, 3),
            "confidence": round(confidence, 3),
            "is_exact": is_exact,
        })

    scored.sort(key=lambda x: x.get("confidence", 0), reverse=True)
    exact_match = next((c for c in scored if c.get("is_exact")), None)
    return exact_match, scored


def _ensure_matching_columns(cursor):
    """Ensure download_queue has columns required for manual matching workflow."""
    cursor.execute("PRAGMA table_info(download_queue);")
    columns = [row[1] for row in cursor.fetchall()]

    required = {
        "mb_match_status": "TEXT",
        "mb_match_score": "REAL",
        "mb_match_candidates": "TEXT",
        "mb_release_group_id": "TEXT",
        "mb_matched_title": "TEXT",
        "mb_matched_artist": "TEXT",
        "mb_matched_year": "TEXT",
        "mb_last_match_at": "TEXT"
    }

    for col, col_type in required.items():
        if col not in columns:
            try:
                cursor.execute(f"ALTER TABLE download_queue ADD COLUMN {col} {col_type};")
            except Exception as e:
                logger.warning(f"Could not add {col} column: {e}")


def check_album_complete(album, artist):
    """
    Check if all tracks for an album are discovered and have metadata.
    An album is considered complete when:
    1. At least one track has been discovered for this album
    2. All discovered tracks have file_path set (file found)
    3. All discovered tracks have metadata extracted
    
    Args:
        album: Album name
        artist: Artist name
    
    Returns:
        dict: {
            'is_complete': bool,
            'total_tracks': int,
            'discovered_tracks': int,
            'tracks': list of queue items
        }
    """
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        from app import _is_postgres_connection as app_is_postgres_connection
        is_pg = bool(app_is_postgres_connection(conn))
        placeholder = "%s" if is_pg else "?"
        
        # Get all queue items for this album
        cursor.execute(f"""
            SELECT * FROM download_queue 
            WHERE LOWER(album) = LOWER({placeholder}) 
            AND LOWER(artist) = LOWER({placeholder})
            AND status IN ('discovered', 'completed', 'pending_match', 'unmatched')
            AND file_path IS NOT NULL
            ORDER BY track_number ASC, title ASC
        """, (album, artist))
        
        tracks = [dict(row) for row in cursor.fetchall()]
        conn.close()
        
        if not tracks:
            return {
                'is_complete': False,
                'total_tracks': 0,
                'discovered_tracks': 0,
                'tracks': []
            }
        
        # Check if we have at least 3 tracks (minimum for an album)
        # and all tracks have necessary metadata
        has_metadata = all(
            track.get('artist') and 
            track.get('album') and 
            track.get('title') and 
            track.get('file_path')
            for track in tracks
        )
        
        is_complete = len(tracks) >= 3 and has_metadata
        
        return {
            'is_complete': is_complete,
            'total_tracks': len(tracks),
            'discovered_tracks': len(tracks),
            'tracks': tracks
        }
        
    except Exception as e:
        logger.error(f"Error checking album completeness: {e}")
        return {
            'is_complete': False,
            'total_tracks': 0,
            'discovered_tracks': 0,
            'tracks': [],
            'error': str(e)
        }


def _process_album_tracks_with_metadata(album, artist, tracks, matched_metadata=None):
    """Process album tracks into /music with either matched or existing metadata."""
    from post_download_processor import update_file_metadata, rename_and_move_file

    conn = get_db()
    cursor = conn.cursor()

    album_artists = [t.get('album_artist') or t.get('artist') for t in tracks]
    album_artist_counts = {}
    for aa in album_artists:
        if aa:
            album_artist_counts[aa] = album_artist_counts.get(aa, 0) + 1

    default_album_artist = max(album_artist_counts.items(), key=lambda x: x[1])[0] if album_artist_counts else artist
    consistent_album_artist = (matched_metadata or {}).get("artist") or default_album_artist
    consistent_album_title = (matched_metadata or {}).get("title") or album
    consistent_year = (matched_metadata or {}).get("year")
    release_group_id = (matched_metadata or {}).get("release_group_id")

    success_count = 0
    for track in tracks:
        try:
            file_path = track.get('file_path')
            if not file_path or not os.path.exists(file_path):
                logger.warning(f"File not found: {file_path}")
                continue

            metadata = {
                'track_number': track.get('track_number'),
                'disc_number': track.get('disc_number'),
                'artist': track.get('artist'),
                'album_artist': consistent_album_artist,
                'album': consistent_album_title,
                'year': consistent_year or track.get('year'),
                'title': track.get('title')
            }

            update_file_metadata(file_path, metadata)
            result = rename_and_move_file(file_path, metadata)

            if result.get('success'):
                from app import _is_postgres_connection as app_is_postgres_connection
                is_pg = bool(app_is_postgres_connection(conn))
                placeholder = "%s" if is_pg else "?"
                cursor.execute(
                    f"""
                    UPDATE download_queue
                    SET status = 'imported',
                        album_artist = {placeholder},
                        album = {placeholder},
                        year = {placeholder},
                        release_id = COALESCE({placeholder}, release_id),
                        release_source = COALESCE({placeholder}, release_source),
                        imported_at = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = {placeholder}
                    """,
                    (
                        consistent_album_artist,
                        consistent_album_title,
                        metadata['year'],
                        release_group_id,
                        'musicbrainz' if release_group_id else None,
                        track['id']
                    )
                )
                success_count += 1
            else:
                logger.error(f"Failed to process {track.get('title')}: {result.get('error')}")
        except Exception as track_error:
            logger.error(f"Error processing track {track.get('title')}: {track_error}")

    conn.commit()
    conn.close()
    return success_count


def process_album_with_existing_metadata(album, artist):
    """Manually process a pending-match album with existing file metadata."""
    completion = check_album_complete(album, artist)
    if not completion.get('is_complete'):
        return {"success": False, "error": "Album is not complete yet"}

    success_count = _process_album_tracks_with_metadata(album, artist, completion['tracks'])
    return {
        "success": success_count == len(completion['tracks']),
        "processed": success_count,
        "total": len(completion['tracks'])
    }


def apply_musicbrainz_match_and_process(album, artist, release_group_id):
    """Apply selected MusicBrainz match and process the album."""
    completion = check_album_complete(album, artist)
    if not completion.get('is_complete'):
        return {"success": False, "error": "Album is not complete yet"}

    candidates = _fetch_musicbrainz_album_candidates(artist, album, limit=10)
    selected = next((c for c in candidates if c.get("release_group_id") == release_group_id), None)
    if not selected:
        return {"success": False, "error": "Selected MusicBrainz candidate not found"}

    success_count = _process_album_tracks_with_metadata(album, artist, completion['tracks'], matched_metadata=selected)
    return {
        "success": success_count == len(completion['tracks']),
        "processed": success_count,
        "total": len(completion['tracks']),
        "selected_match": selected
    }


def get_release_tracks_with_status(artist, album, release_group_id, current_folder_files=None):
    """
    Get all tracks from a MusicBrainz release with their status.
    
    Status can be:
    - 'in_folder': In current folder
    - 'downloading': In download queue
    - 'other_folder': In other folders
    - 'missing': Not found anywhere
    
    Args:
        artist: Artist name
        album: Album name
        release_group_id: MusicBrainz release group ID
        current_folder_files: List of filenames in current folder (optional, for matching)
    
    Returns:
        dict with 'tracks' list and summary stats
    """
    try:
        headers = {
            "User-Agent": "sptnr/2.0.0-alpha ( https://github.com/M0VENTURA/sptnr )"
        }
        
        # Fetch the release group
        rg_resp = session.get(
            f"https://musicbrainz.org/ws/2/release-group/{release_group_id}",
            params={"fmt": "json"},
            headers=headers,
            timeout=10
        )
        rg_resp.raise_for_status()
        rg_data = rg_resp.json()
        
        # Get first release for this release group
        releases_resp = session.get(
            f"https://musicbrainz.org/ws/2/release-group/{release_group_id}/releases",
            params={"fmt": "json", "limit": 1},
            headers=headers,
            timeout=10
        )
        releases_resp.raise_for_status()
        releases = releases_resp.json().get("releases", [])
        
        if not releases:
            return {"success": False, "error": "No releases found for this release group", "tracks": []}
        
        release_id = releases[0].get("id")
        
        # Fetch full release with tracks
        release_resp = session.get(
            f"https://musicbrainz.org/ws/2/release/{release_id}",
            params={"fmt": "json", "inc": "recordings+artist-credits+work-level-rels+work-rels+artist-rels"},
            headers=headers,
            timeout=10
        )
        release_resp.raise_for_status()
        release_data = release_resp.json()
        
        # Extract all tracks from media
        all_tracks = []
        for medium in release_data.get("media", []):
            disc_number = medium.get("position", 1)
            for track in medium.get("tracks", []):
                recording = track.get("recording", {})
                track_title = recording.get("title") or track.get("title") or ""
                track_number = track.get("position", 0)
                duration = recording.get("length", 0)  # in milliseconds
                
                # Extract lyricist and writer information
                credits = _extract_lyricist_and_writers(recording)
                
                all_tracks.append({
                    "track_number": track_number,
                    "disc_number": disc_number,
                    "title": track_title,
                    "duration": duration,
                    "lyricists": credits["lyricists"],
                    "composers": credits["composers"],
                    "writers": credits["writers"]
                })
        
        # Get queue items for this artist/album
        conn = get_db()
        cursor = conn.cursor()
        
        from app import _is_postgres_connection as app_is_postgres_connection
        is_pg = bool(app_is_postgres_connection(conn))
        placeholder = "%s" if is_pg else "?"
        
        cursor.execute(f"""
            SELECT id, title, file_path, status
            FROM download_queue
            WHERE LOWER(artist) = LOWER({placeholder})
            AND LOWER(album) = LOWER({placeholder})
            ORDER BY track_number, title
        """, (artist, album))
        
        queue_items = {dict(row)['title'].lower(): dict(row) for row in cursor.fetchall()}
        
        # Check library for existing files
        from api_clients.musicbrainz import search_library_for_track
        music_dir = os.environ.get("MUSIC_ROOT", "/music")
        
        # Normalize filenames if provided
        current_folder_files = [f.lower() for f in (current_folder_files or [])]
        
        # Match each track with status
        tracks_with_status = []
        for track in all_tracks:
            title = track['title']
            title_lower = title.lower()
            status = "missing"
            status_details = {}
            
            # Check if in queue
            if title_lower in queue_items:
                q_item = queue_items[title_lower]
                status = "downloading"
                status_details = {
                    "queue_id": q_item.get('id'),
                    "queue_status": q_item.get('status')
                }
            # Check if in current folder
            elif current_folder_files:
                # Fuzzy match filename
                for folder_file in current_folder_files:
                    if title_lower in folder_file or folder_file in title_lower:
                        status = "in_folder"
                        status_details = {"filename": folder_file}
                        break
            
            # Check if in other folders (search library)
            if status == "missing":
                try:
                    # Search for file in music directory
                    for root, dirs, files in os.walk(music_dir):
                        for file in files:
                            if title_lower in file.lower():
                                status = "other_folder"
                                status_details = {"folder": root, "filename": file}
                                break
                        if status == "other_folder":
                            break
                except Exception as e:
                    logger.debug(f"Error searching library for {title}: {e}")
            
            tracks_with_status.append({
                **track,
                "status": status,
                "status_details": status_details
            })
        
        # Calculate summary
        statuses = [t['status'] for t in tracks_with_status]
        summary = {
            "total": len(all_tracks),
            "in_folder": statuses.count("in_folder"),
            "downloading": statuses.count("downloading"),
            "other_folder": statuses.count("other_folder"),
            "missing": statuses.count("missing")
        }
        
        conn.close()
        
        return {
            "success": True,
            "release": {
                "title": rg_data.get("title"),
                "artist": artist,
                "primary_type": rg_data.get("primary-type"),
                "release_group_id": release_group_id
            },
            "summary": summary,
            "tracks": tracks_with_status
        }
        
    except Exception as e:
        logger.error(f"Error getting release tracks: {e}")
        return {
            "success": False,
            "error": str(e),
            "tracks": []
        }


def merge_folders(source_folders, destination_folder, conflict_strategy="skip", dry_run=False):
    """
    Merge multiple source folders into a single destination folder.
    
    Args:
        source_folders: List of source folder paths
        destination_folder: Target folder path
        conflict_strategy: How to handle conflicts - "skip", "overwrite", or "keep-both"
        dry_run: If True, analyze without moving files
    
    Returns:
        Dict with merge results: summary, conflicts, operations, etc.
    """
    try:
        import hashlib
        import shutil
        
        # Validate inputs
        if not source_folders or not isinstance(source_folders, list):
            return {"success": False, "error": "source_folders must be a non-empty list"}
        
        if not destination_folder or not isinstance(destination_folder, str):
            return {"success": False, "error": "destination_folder is required"}
        
        if conflict_strategy not in ("skip", "overwrite", "keep-both"):
            return {"success": False, "error": f"Invalid conflict_strategy: {conflict_strategy}"}
        
        # Validate all source folders exist and are different from destination
        for folder in source_folders:
            if not os.path.isdir(folder):
                return {"success": False, "error": f"Source folder does not exist: {folder}"}
            if os.path.abspath(folder) == os.path.abspath(destination_folder):
                return {"success": False, "error": f"Source and destination cannot be the same: {folder}"}
        
        # Create destination if it doesn't exist
        if not dry_run:
            os.makedirs(destination_folder, exist_ok=True)
        
        # Collect all files from source folders
        source_files = {}  # filename -> [(full_path, folder_path), ...]
        total_size = 0
        
        for source_folder in source_folders:
            try:
                for root, dirs, files in os.walk(source_folder):
                    for filename in files:
                        if filename.startswith('.'):
                            continue
                        
                        full_path = os.path.join(root, filename)
                        file_size = os.path.getsize(full_path)
                        total_size += file_size
                        
                        if filename not in source_files:
                            source_files[filename] = []
                        source_files[filename].append({
                            "full_path": full_path,
                            "source_folder": source_folder,
                            "size": file_size,
                            "relative_path": os.path.relpath(full_path, source_folder)
                        })
            except Exception as e:
                logger.warning(f"Error scanning source folder {source_folder}: {e}")
        
        # Check for conflicts and duplicates
        conflicts = []
        operations = []
        files_skipped = 0
        files_moved = 0
        files_with_conflicts = 0
        
        # Check if files exist in destination
        for filename, source_entries in source_files.items():
            dest_path = os.path.join(destination_folder, filename)
            
            # Handle duplicates within source folders
            if len(source_entries) > 1:
                for idx, entry in enumerate(source_entries):
                    if conflict_strategy == "skip":
                        conflicts.append({
                            "file": filename,
                            "type": "duplicate_in_sources",
                            "sources": [e["full_path"] for e in source_entries],
                            "action": "skipped"
                        })
                        files_skipped += len(source_entries)
                        files_with_conflicts += 1
                        break
                    elif conflict_strategy == "keep-both":
                        # Rename duplicates: filename.1, filename.2, etc.
                        name_parts = os.path.splitext(filename)
                        new_name = f"{name_parts[0]}.{idx}{name_parts[1]}"
                        new_dest = os.path.join(destination_folder, new_name)
                        operations.append({
                            "type": "move",
                            "source": entry["full_path"],
                            "destination": new_dest,
                            "size": entry["size"]
                        })
                        files_moved += 1
                    elif conflict_strategy == "overwrite" and idx == 0:
                        operations.append({
                            "type": "move",
                            "source": entry["full_path"],
                            "destination": dest_path,
                            "size": entry["size"]
                        })
                        files_moved += 1
            else:
                # Single source file
                entry = source_entries[0]
                
                # Check if file exists in destination
                if os.path.exists(dest_path):
                    dest_size = os.path.getsize(dest_path)
                    source_size = entry["size"]
                    files_different = source_size != dest_size
                    
                    if conflict_strategy == "skip":
                        conflicts.append({
                            "file": filename,
                            "type": "duplicate_in_destination",
                            "source_path": entry["full_path"],
                            "dest_path": dest_path,
                            "source_size": source_size,
                            "dest_size": dest_size,
                            "files_different": files_different,
                            "action": "skipped"
                        })
                        files_skipped += 1
                        files_with_conflicts += 1
                    elif conflict_strategy == "overwrite":
                        operations.append({
                            "type": "move",
                            "source": entry["full_path"],
                            "destination": dest_path,
                            "size": entry["size"],
                            "overwrites": True
                        })
                        files_moved += 1
                        files_with_conflicts += 1
                    elif conflict_strategy == "keep-both":
                        # Rename to avoid conflict
                        name_parts = os.path.splitext(filename)
                        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        new_name = f"{name_parts[0]}_({timestamp}){name_parts[1]}"
                        new_dest = os.path.join(destination_folder, new_name)
                        operations.append({
                            "type": "move",
                            "source": entry["full_path"],
                            "destination": new_dest,
                            "size": entry["size"],
                            "renamed": True,
                            "original_dest": dest_path
                        })
                        files_moved += 1
                        files_with_conflicts += 1
                else:
                    # No conflict, move file
                    operations.append({
                        "type": "move",
                        "source": entry["full_path"],
                        "destination": dest_path,
                        "size": entry["size"]
                    })
                    files_moved += 1
        
        # If dry run, don't actually move files
        if dry_run:
            return {
                "success": True,
                "dry_run": True,
                "summary": {
                    "total_files": len(source_files),
                    "files_to_move": files_moved,
                    "files_to_skip": files_skipped,
                    "files_with_conflicts": files_with_conflicts,
                    "total_size_mb": round(total_size / (1024 * 1024), 2),
                    "conflict_strategy": conflict_strategy
                },
                "conflicts": conflicts,
                "operations": operations
            }
        
        # Execute operations
        moved_count = 0
        failed_operations = []
        
        for op in operations:
            try:
                source = op["source"]
                dest = op["destination"]
                
                # Create destination directory if needed
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                
                # Move file
                shutil.move(source, dest)
                moved_count += 1
            except Exception as e:
                logger.error(f"Error moving file {op.get('source')}: {e}")
                failed_operations.append({
                    "operation": op,
                    "error": str(e)
                })
        
        # Clean up empty source folders
        for source_folder in source_folders:
            try:
                if os.path.isdir(source_folder):
                    remaining = os.listdir(source_folder)
                    if not remaining:
                        os.rmdir(source_folder)
                        logger.info(f"Removed empty source folder: {source_folder}")
            except Exception as e:
                logger.debug(f"Could not remove empty folder {source_folder}: {e}")
        
        return {
            "success": len(failed_operations) == 0,
            "dry_run": False,
            "summary": {
                "total_files": len(source_files),
                "files_moved": moved_count,
                "files_skipped": files_skipped,
                "files_with_conflicts": files_with_conflicts,
                "failed": len(failed_operations),
                "total_size_mb": round(total_size / (1024 * 1024), 2),
                "conflict_strategy": conflict_strategy
            },
            "conflicts": conflicts,
            "failed_operations": failed_operations if failed_operations else None
        }
    
    except Exception as e:
        logger.error(f"Error merging folders: {e}")
        return {
            "success": False,
            "error": str(e)
        }


def process_complete_albums():
    """Process complete discovered albums, using MusicBrainz smart matching when possible."""
    stats = {
        'checked': 0,
        'processed': 0,
        'duplicates_found': 0,
        'pending_review': 0,
        'exact_matches': 0,
        'errors': []
    }

    try:
        conn = get_db()
        cursor = conn.cursor()
        _ensure_matching_columns(cursor)
        conn.commit()

        cursor.execute(
            """
            SELECT DISTINCT album, artist
            FROM download_queue
            WHERE status IN ('discovered', 'pending_match', 'unmatched')
            AND album IS NOT NULL
            AND artist IS NOT NULL
            AND file_path IS NOT NULL
            """
        )
        albums = [dict(row) for row in cursor.fetchall()]
        conn.close()

        logger.info(f"Checking {len(albums)} discovered albums for completeness")

        for album_info in albums:
            album = album_info['album']
            artist = album_info['artist']
            stats['checked'] += 1

            try:
                completion = check_album_complete(album, artist)
                if not completion['is_complete']:
                    logger.debug(f"Album not complete: {artist} - {album} ({completion['discovered_tracks']} tracks)")
                    continue

                logger.info(f"Complete album found: {artist} - {album} ({completion['total_tracks']} tracks)")

                if check_album_exists_in_library(album, artist):
                    conn = get_db()
                    cursor = conn.cursor()
                    for track in completion['tracks']:
                        from app import _is_postgres_connection as app_is_postgres_connection
                        is_pg = bool(app_is_postgres_connection(conn))
                        placeholder = "%s" if is_pg else "?"
                        cursor.execute(
                            f"""
                            UPDATE download_queue
                            SET status = 'possible_duplicate',
                                updated_at = CURRENT_TIMESTAMP
                            WHERE id = {placeholder}
                            """,
                            (track['id'],)
                        )
                    conn.commit()
                    conn.close()
                    stats['duplicates_found'] += 1
                    logger.warning(f"Album already exists in library (marked as possible_duplicate): {artist} - {album}")
                    continue

                # Smart-match album against MusicBrainz candidates.
                exact_match = None
                scored_candidates = []
                try:
                    candidates = _fetch_musicbrainz_album_candidates(artist, album, limit=8)
                    exact_match, scored_candidates = _score_album_candidates(artist, album, completion['tracks'], candidates)
                except Exception as match_error:
                    logger.warning(f"MusicBrainz matching failed for {artist} - {album}: {match_error}")

                if exact_match:
                    success_count = _process_album_tracks_with_metadata(
                        album,
                        artist,
                        completion['tracks'],
                        matched_metadata=exact_match
                    )
                    if success_count == len(completion['tracks']):
                        stats['processed'] += 1
                        stats['exact_matches'] += 1
                        logger.info(f"✓ Auto-processed exact MusicBrainz match: {artist} - {album}")
                    else:
                        logger.warning(f"Partially processed album: {artist} - {album} ({success_count}/{len(completion['tracks'])} tracks)")
                else:
                    conn = get_db()
                    cursor = conn.cursor()
                    _ensure_matching_columns(cursor)
                    candidates_json = json.dumps(scored_candidates[:5]) if scored_candidates else "[]"
                    best_score = scored_candidates[0].get('confidence') if scored_candidates else 0

                    for track in completion['tracks']:
                        cursor.execute(
                            """
                            UPDATE download_queue
                            SET status = 'pending_match',
                                mb_match_status = 'needs_review',
                                mb_match_score = ?,
                                mb_match_candidates = ?,
                                mb_release_group_id = NULL,
                                mb_matched_title = NULL,
                                mb_matched_artist = NULL,
                                mb_matched_year = NULL,
                                mb_last_match_at = CURRENT_TIMESTAMP,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE id = ?
                            """,
                            (best_score, candidates_json, track['id'])
                        )

                    conn.commit()
                    conn.close()
                    stats['pending_review'] += 1
                    logger.info(f"Album queued for manual review: {artist} - {album} (no 100% MusicBrainz match)")

            except Exception as album_error:
                error_msg = f"Error checking album {artist} - {album}: {str(album_error)}"
                logger.error(error_msg)
                stats['errors'].append(error_msg)

        logger.info(
            f"Album processing complete: {stats['processed']} processed, "
            f"{stats['exact_matches']} exact MB matches, "
            f"{stats['pending_review']} pending manual review, "
            f"{stats['duplicates_found']} duplicates"
        )

        if stats['processed'] > 0:
            logger.info("Triggering Navidrome library scan for newly added albums...")
            try:
                trigger_navidrome_scan()
            except Exception as scan_error:
                logger.warning(f"Could not trigger Navidrome scan: {scan_error}")

        return stats

    except Exception as e:
        error_msg = f"Error in process_complete_albums: {str(e)}"
        logger.error(error_msg)
        import traceback
        logger.error(traceback.format_exc())
        stats['errors'].append(error_msg)
        return stats


def auto_move_completed_album(release_id=None, artist=None, album=None):
    """
    Auto-move all completed tracks for an album/release to the music library.

    Called after a queue item is marked 'completed' to check whether every track
    in the same album (identified by release_id, or artist+album) is now either
    'completed' or 'imported'.  When the album is fully ready the remaining
    'completed' tracks (those NOT yet individually copied) are moved to /music
    and their queue status is updated to 'imported'.

    Tracks that were already individually copied (copied_individually=1) are
    counted as done but are not moved again.

    Args:
        release_id: MusicBrainz release ID to look up tracks by (preferred).
        artist:     Artist name – used when release_id is not available.
        album:      Album name – used when release_id is not available.

    Returns:
        dict with keys: moved (int), already_copied (int), skipped (int),
                        album_complete (bool), error (str|None)
    """
    result = {
        'moved': 0,
        'already_copied': 0,
        'skipped': 0,
        'album_complete': False,
        'error': None
    }

    if not release_id and not (artist and album):
        result['error'] = "release_id or artist+album required"
        return result

    try:
        conn = get_db()
        cursor = conn.cursor()

        from app import _is_postgres_connection as app_is_postgres_connection
        is_pg = bool(app_is_postgres_connection(conn))
        placeholder = "%s" if is_pg else "?"

        # Fetch all queue items for this album
        if release_id:
            cursor.execute(
                f"""
                SELECT id, status, file_path, found_filename, artist, title,
                       album, album_artist, track_number, disc_number, year,
                       copied_individually
                FROM download_queue
                WHERE release_id = {placeholder}
                  AND status NOT IN ('removed', 'cancelled')
                ORDER BY track_number ASC, title ASC
                """,
                (release_id,)
            )
        else:
            cursor.execute(
                f"""
                SELECT id, status, file_path, found_filename, artist, title,
                       album, album_artist, track_number, disc_number, year,
                       copied_individually
                FROM download_queue
                WHERE LOWER(artist) = LOWER({placeholder})
                  AND LOWER(album)  = LOWER({placeholder})
                  AND status NOT IN ('removed', 'cancelled')
                ORDER BY track_number ASC, title ASC
                """,
                (artist, album)
            )

        tracks = [dict(row) for row in cursor.fetchall()]

        if not tracks:
            conn.close()
            result['error'] = "No tracks found for this album"
            return result

        # --- Check album completeness ---
        # A track is "done" when it is imported, individually copied, or
        # completed with a file ready to move.
        def _is_done(t):
            if t['status'] == 'imported':
                return True
            if t.get('copied_individually') == 1:
                return True
            if t['status'] == 'completed' and t.get('file_path'):
                return True
            return False

        # Are ALL tracks done?
        all_done = all(_is_done(t) for t in tracks)

        if not all_done:
            # Album not yet complete – nothing to auto-move
            conn.close()
            return result

        result['album_complete'] = True

        # Determine destination directory from already-imported tracks or metadata
        music_root = os.environ.get("MUSIC_ROOT", "/music")

        # Use consistent album artist / year from completed tracks
        album_artists = [t.get('album_artist') or t.get('artist') for t in tracks if t.get('album_artist') or t.get('artist')]
        years = [t.get('year') for t in tracks if t.get('year')]
        albums = [t.get('album') for t in tracks if t.get('album')]

        def _most_common(lst):
            if not lst:
                return None
            counts = {}
            for v in lst:
                counts[v] = counts.get(v, 0) + 1
            return max(counts, key=counts.get)

        dest_album_artist = _most_common(album_artists) or artist or 'Unknown Artist'
        dest_album = _most_common(albums) or album or 'Unknown Album'
        dest_year = _most_common(years)

        if dest_year:
            dest_dir = os.path.join(music_root, dest_album_artist, f"{dest_year} - {dest_album}")
        else:
            dest_dir = os.path.join(music_root, dest_album_artist, dest_album)

        os.makedirs(dest_dir, exist_ok=True)

        import shutil

        for track in tracks:
            if track['status'] == 'imported':
                # Already moved (individually or previously)
                result['already_copied'] += 1
                continue

            if not track.get('file_path') or not os.path.exists(track['file_path']):
                result['skipped'] += 1
                continue

            src = track['file_path']
            filename = os.path.basename(src)
            dest = os.path.join(dest_dir, filename)

            # Avoid overwriting
            if os.path.exists(dest):
                base, ext = os.path.splitext(filename)
                counter = 1
                while os.path.exists(os.path.join(dest_dir, f"{base}_{counter}{ext}")):
                    counter += 1
                dest = os.path.join(dest_dir, f"{base}_{counter}{ext}")

            try:
                shutil.move(src, dest)
                cursor.execute(
                    f"""
                    UPDATE download_queue
                    SET status = 'imported',
                        file_path = {placeholder},
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = {placeholder}
                    """,
                    (dest, track['id'])
                )
                result['moved'] += 1
                logger.info(
                    f"[AUTO_MOVE] Moved {filename} → {dest} "
                    f"(queue id={track['id']})"
                )
            except Exception as move_err:
                logger.error(f"[AUTO_MOVE] Failed to move {src}: {move_err}")
                result['skipped'] += 1

        conn.commit()
        conn.close()

        if result['moved'] > 0:
            try:
                trigger_navidrome_scan()
            except Exception as scan_err:
                logger.warning(f"[AUTO_MOVE] Could not trigger Navidrome scan: {scan_err}")

        logger.info(
            f"[AUTO_MOVE] Album complete: {dest_album_artist} – {dest_album} | "
            f"moved={result['moved']}, already_copied={result['already_copied']}, "
            f"skipped={result['skipped']}"
        )
        return result

    except Exception as e:
        logger.error(f"[AUTO_MOVE] Error in auto_move_completed_album: {e}")
        import traceback
        logger.error(traceback.format_exc())
        result['error'] = str(e)
        return result
# ============================================================================
# Individual File Copying Functions (NEW)
# Handle copying individual files from downloads to music with MusicBrainz metadata
# ============================================================================

def copy_queue_item_file_to_music(queue_id, music_dir=None):
    """
    Copy a specific queue item file to the music directory with proper metadata.
    Uses MusicBrainz metadata stored in the queue item to update tags before copying.
    
    Args:
        queue_id: Queue item ID
        music_dir: Optional override for music directory (defaults to /music)
    
    Returns:
        dict: {
            'success': bool,
            'target_path': str or None,
            'error': str or None,
            'metadata_updated': bool,
            'file_copied': bool
        }
    """
    try:
        from download_file_manager import copy_file_to_music as file_manager_copy
        
        if music_dir is None:
            music_dir = MUSIC_DIR
        
        # Get queue item
        conn = get_db()
        cursor = conn.cursor()
        
        from app import _is_postgres_connection as app_is_postgres_connection
        is_pg = bool(app_is_postgres_connection(conn))
        placeholder = "%s" if is_pg else "?"
        
        cursor.execute(f"SELECT * FROM download_queue WHERE id = {placeholder}", (queue_id,))
        queue_item = cursor.fetchone()
        conn.close()
        
        if not queue_item:
            return {
                'success': False,
                'target_path': None,
                'error': f'Queue item {queue_id} not found',
                'metadata_updated': False,
                'file_copied': False
            }
        
        queue_item = dict(queue_item)
        file_path = queue_item.get('file_path')
        
        if not file_path or not os.path.exists(file_path):
            return {
                'success': False,
                'target_path': None,
                'error': f'File not found: {file_path}',
                'metadata_updated': False,
                'file_copied': False
            }
        
        # Use the file manager to copy with metadata
        result = file_manager_copy(file_path, queue_item, music_dir)
        
        # Mark as copied individually if successful
        if result['success']:
            mark_file_as_copied_individually(queue_id, result.get('target_path'))
        
        return result
        
    except Exception as e:
        logger.error(f"Error copying queue item {queue_id}: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return {
            'success': False,
            'target_path': None,
            'error': str(e),
            'metadata_updated': False,
            'file_copied': False
        }


def mark_file_as_copied_individually(queue_id, target_path=None):
    """
    Mark a queue item file as copied individually.
    This tracks that a file has been manually copied to /music and counts toward
    the full album completion tracking.
    
    Args:
        queue_id: Queue item ID
        target_path: Optional path where file was copied to
    
    Returns:
        Updated queue item dict or None
    """
    try:
        result = update_queue_item(
            queue_id,
            copied_individually=1,
            copied_individually_at=datetime.now().isoformat(),
            status='imported',
            file_path=target_path if target_path else None
        )
        
        if result:
            logger.info(f"✅ Marked queue item {queue_id} as copied individually")
            return result
        else:
            logger.error(f"Could not mark queue item {queue_id} as copied individually")
            return None
    
    except Exception as e:
        logger.error(f"Error marking queue item {queue_id} as copied individually: {e}")
        return None


def get_album_files_with_status(album, album_artist, downloads_dir=None):
    """
    Get all files for an album showing their current copy status.
    Useful for displaying UI showing which files have been copied and which haven't.
    
    Args:
        album: Album name
        album_artist: Album artist name
        downloads_dir: Optional override for downloads directory
    
    Returns:
        dict: {
            'album': album name,
            'artist': artist name,
            'files': [
                {
                    'queue_id': int,
                    'filename': str,
                    'file_path': str,
                    'title': str,
                    'track_number': str,
                    'status': 'discovered|downloading|completed|imported',
                    'copied': bool,
                    'copied_at': datetime or None
                }
            ],
            'summary': {
                'total': int,
                'copied': int,
                'pending': int,
                'progress_pct': float
            }
        }
    """
    try:
        if downloads_dir is None:
            downloads_dir = get_downloads_dir()
        
        conn = get_db()
        cursor = conn.cursor()
        
        from app import _is_postgres_connection as app_is_postgres_connection
        is_pg = bool(app_is_postgres_connection(conn))
        placeholder = "%s" if is_pg else "?"
        
        # Get all queue items for this album
        cursor.execute(f"""
            SELECT * FROM download_queue
            WHERE LOWER(album) = LOWER({placeholder})
            AND LOWER(COALESCE(album_artist, artist)) = LOWER({placeholder})
            ORDER BY track_number ASC, title ASC
        """, (album, album_artist))
        
        items = [dict(row) for row in cursor.fetchall()]
        conn.close()
        
        files_list = []
        total_copied = 0
        
        for item in items:
            copied = bool(item.get('copied_individually'))
            if copied:
                total_copied += 1
            
            files_list.append({
                'queue_id': item['id'],
                'filename': item.get('found_filename') or os.path.basename(item.get('file_path', '')),
                'file_path': item.get('file_path'),
                'title': item.get('title', 'Unknown'),
                'track_number': item.get('track_number', '0'),
                'status': item.get('status', 'unknown'),
                'copied': copied,
                'copied_at': item.get('copied_individually_at')
            })
        
        total = len(files_list)
        pending = total - total_copied
        progress_pct = ((total_copied / total) * 100) if total > 0 else 0
        
        return {
            'album': album,
            'artist': album_artist,
            'files': files_list,
            'summary': {
                'total': total,
                'copied': total_copied,
                'pending': pending,
                'progress_pct': round(progress_pct, 1)
            }
        }
        
    except Exception as e:
        logger.error(f"Error getting album files status: {e}")
        return {
            'album': album,
            'artist': album_artist,
            'files': [],
            'summary': {
                'total': 0,
                'copied': 0,
                'pending': 0,
                'progress_pct': 0.0
            },
            'error': str(e)
        }


def get_album_copy_progress(album, album_artist):
    """
    Get copy progress for an album (how many files have been copied to /music).
    
    Args:
        album: Album name  
        album_artist: Album artist name
    
    Returns:
        dict: {
            'album': album,
            'artist': artist,
            'total_tracks': int,
            'copied_tracks': int,
            'pending_tracks': int,
            'progress_pct': float,
            'is_complete': bool
        }
    """
    try:
        status = get_album_files_with_status(album, album_artist)
        summary = status.get('summary', {})
        
        is_complete = (
            summary.get('total', 0) > 0 and 
            summary.get('pending', 0) == 0
        )
        
        return {
            'album': album,
            'artist': album_artist,
            'total_tracks': summary.get('total', 0),
            'copied_tracks': summary.get('copied', 0),
            'pending_tracks': summary.get('pending', 0),
            'progress_pct': summary.get('progress_pct', 0.0),
            'is_complete': is_complete
        }
        
    except Exception as e:
        logger.error(f"Error getting album copy progress: {e}")
        return {
            'album': album,
            'artist': album_artist,
            'total_tracks': 0,
            'copied_tracks': 0,
            'pending_tracks': 0,
            'progress_pct': 0.0,
            'is_complete': False,
            'error': str(e)
        }
