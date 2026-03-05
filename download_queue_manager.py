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


def _ensure_download_queue_columns(conn, cursor):
    """Ensure expected queue columns exist (run once per process)."""
    global _queue_schema_checked
    if _queue_schema_checked:
        return

    with _queue_schema_lock:
        if _queue_schema_checked:
            return

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
            'release_source': "TEXT"
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
        
        # Ensure schema only on SQLite path (PRAGMA/ALTER logic is SQLite-specific).
        if not is_pg:
            _ensure_download_queue_columns(conn, cursor)
        
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
                execute_write_with_retry(
                    cursor,
                    conn,
                    """
                    INSERT INTO download_queue 
                    (artist, title, album, search_query, source, status, priority, file_path, import_group, import_type, 
                     track_number, album_artist, year, release_id, release_source, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 'queued', ?, NULL, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
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


def get_queue(status=None, source='soulseek', limit=50):
    """
    Get queue items
    
    Args:
        status: Filter by status (queued, searching, downloading, completed, failed, imported)
        source: Filter by source (soulseek, qbittorrent)
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
        
        query = "SELECT * FROM download_queue"
        params = []
        
        # Only filter by source if column exists
        if 'source' in columns:
            query += " WHERE source = ?"
            params.append(source)
            if status:
                query += " AND status = ?"
                params.append(status)
        elif status:
            query += " WHERE status = ?"
            params.append(status)
        
        # Only use priority in ORDER BY if column exists
        if 'priority' in columns:
            query += " ORDER BY priority ASC, created_at DESC LIMIT ?"
        else:
            query += " ORDER BY created_at DESC LIMIT ?"
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
            conn = get_db()
            cursor = conn.cursor()
            
            # Build update query
            updates = []
            params = []
            
            for key, value in kwargs.items():
                if key in ['status', 'source_id', 'found_filename', 'file_path', 'failure_reason', 
                           'retry_count', 'last_failure_time', 'imported_at', 'metadata', 'import_group', 'import_type']:
                    # Special handling for file_path to avoid UNIQUE constraint issues
                    if key == 'file_path' and value:
                        # Check if this file_path is already in use by another item
                        cursor.execute("SELECT COUNT(*) as cnt FROM download_queue WHERE file_path = ? AND id != ?", 
                                     (value, queue_id))
                        result = cursor.fetchone()
                        if result and result['cnt'] > 0:
                            logger.warning(f"File path {value} already in use by another queue item, skipping update")
                            continue
                    
                    updates.append(f"{key} = ?")
                    params.append(value)
            
            if not updates:
                logger.warning(f"No valid fields to update for queue item {queue_id}")
                conn.close()
                return None
            
            updates.append("updated_at = CURRENT_TIMESTAMP")
            params.append(queue_id)
            
            query = f"UPDATE download_queue SET {', '.join(updates)} WHERE id = ?"
            cursor.execute(query, params)
            conn.commit()
            
            if cursor.rowcount == 0:
                logger.warning(f"No rows updated for queue item {queue_id} - item may not exist")
                conn.close()
                return None
            
            logger.debug(f"Updated {cursor.rowcount} row(s) for queue item {queue_id}: {list(kwargs.keys())}")
            
            # Return updated item
            cursor.execute("SELECT * FROM download_queue WHERE id = ?", (queue_id,))
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
            conn = get_db()
            cursor = conn.cursor()
            
            # Get current retry count
            cursor.execute("SELECT retry_count, max_retries FROM download_queue WHERE id = ?", (queue_id,))
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
            
            cursor.execute("""
                UPDATE download_queue 
                SET status = ?, retry_count = ?, failure_reason = ?, last_failure_time = CURRENT_TIMESTAMP, next_retry_at = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (new_status, retry_count, reason, next_retry.isoformat(), queue_id))
            
            conn.commit()
            
            # Return updated item
            cursor.execute("SELECT * FROM download_queue WHERE id = ?", (queue_id,))
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


def check_downloads_folder():
    """
    Monitor /downloads folder for completed files.
    Match files to queue items and update their status.
    
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
                
                # Update queue item with completed status
                result = update_queue_item(
                    queue_item['id'],
                    status='completed',
                    found_filename=match_found,
                    file_path=match_path,
                    imported_at=datetime.now().isoformat()
                )
                
                if result:
                    logger.info(f"Updated queue item {queue_item['id']} to completed")
                    completed_items.append({
                        'queue_id': queue_item['id'],
                        'filename': match_found,
                        'file_path': match_path,
                        'artist': queue_item['artist'],
                        'title': queue_item['title'],
                        'album': queue_item['album']
                    })
                else:
                    logger.warning(f"Failed to update queue item {queue_item['id']}")
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
            logger.warning(f"[AUTO-DISCOVER] No audio files found in {downloads_dir}")
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
                cursor.execute("""
                    SELECT id, status FROM download_queue 
                    WHERE (file_path = ? OR found_filename = ?)
                """, (full_path, filename))
                
                existing = cursor.fetchone()
                if existing:
                    stats['already_in_queue'] += 1
                    logger.debug(f"File already in queue (ID {existing['id']}, status {existing['status']}): {filename}")
                    continue
                
                # Check if track exists in library (case-insensitive)
                cursor.execute("""
                    SELECT id FROM tracks 
                    WHERE LOWER(artist) = LOWER(?) 
                    AND LOWER(album) = LOWER(?) 
                    AND LOWER(title) = LOWER(?)
                """, (artist, album, title))
                
                in_library = cursor.fetchone()
                if in_library:
                    stats['already_in_library'] += 1
                    logger.debug(f"Track already in library: {artist} - {title}")
                    
                    # Still add to queue with status 'completed' so it appears in Completed & Ready to Organize
                    execute_write_with_retry(
                        cursor,
                        conn,
                        """
                        INSERT INTO download_queue 
                        (artist, title, album, album_artist, track_number, disc_number, year, found_filename, file_path, 
                         status, source, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed', 'discovered', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """,
                        (artist, title, album, album_artist, track_number, disc_number, year, filename, full_path),
                        context="auto_discover in-library insert"
                    )

                    stats['queued'] += 1
                    continue
                
                # Add to queue with 'completed' status (found in downloads folder, ready to organize)
                execute_write_with_retry(
                    cursor,
                    conn,
                    """
                    INSERT INTO download_queue 
                    (artist, title, album, album_artist, track_number, disc_number, year, found_filename, file_path, 
                     status, source, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed', 'discovered', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                    (artist, title, album, album_artist, track_number, disc_number, year, filename, full_path),
                    context="auto_discover insert"
                )

                stats['queued'] += 1
                
                # Log discovery with metadata and format info
                metadata_status = "✓ metadata" if metadata else "✗ fallback"
                if file_ext == '.flac':
                    logger.info(f"✅ Discovered [FLAC→MP3] [{metadata_status}]: {artist} - {title}")
                else:
                    logger.info(f"✅ Discovered [{metadata_status}]: {artist} - {title} from {os.path.basename(os.path.dirname(full_path))}/{filename}")
                
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
    Get completed downloads waiting for organization
    
    Returns:
        List of completed queue items
    """
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute("""
            SELECT * FROM download_queue 
            WHERE status = 'completed'
            AND file_path IS NOT NULL
            ORDER BY updated_at DESC
            LIMIT ?
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
                placeholders = ','.join('?' * len(removed_ids))
                cursor.execute(f"DELETE FROM download_queue WHERE id IN ({placeholders})", removed_ids)
                conn.commit()
                stats['removed'] = len(removed_ids)
                logger.info(f"Cleaned up {len(removed_ids)} queue items with missing files")

            return stats

        except sqlite3.OperationalError as e:
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

        except Exception as e:
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
        cursor.execute("""
            SELECT COUNT(*) as count FROM tracks 
            WHERE LOWER(album) = LOWER(?) 
            AND (LOWER(artist) = LOWER(?) OR LOWER(album_artist) = LOWER(?))
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
                        params={"fmt": "json", "inc": "recordings+artist-credits+labels"},
                        headers=headers,
                        timeout=10
                    )
                    if track_resp.ok:
                        rel_json = track_resp.json()
                        media = rel_json.get("media", [])
                        for medium in media:
                            for tr in medium.get("tracks", []):
                                track_title = ((tr.get("recording") or {}).get("title") or tr.get("title") or "").strip()
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
        
        # Get all queue items for this album
        cursor.execute("""
            SELECT * FROM download_queue 
            WHERE LOWER(album) = LOWER(?) 
            AND LOWER(artist) = LOWER(?)
            AND status IN ('discovered', 'completed', 'pending_match')
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
                cursor.execute(
                    """
                    UPDATE download_queue
                    SET status = 'imported',
                        album_artist = ?,
                        album = ?,
                        year = ?,
                        release_id = COALESCE(?, release_id),
                        release_source = COALESCE(?, release_source),
                        imported_at = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
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
            WHERE status IN ('discovered', 'pending_match')
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
                        cursor.execute(
                            """
                            UPDATE download_queue
                            SET status = 'possible_duplicate',
                                updated_at = CURRENT_TIMESTAMP
                            WHERE id = ?
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
