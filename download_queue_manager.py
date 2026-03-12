#!/usr/bin/env python3
"""
Download Queue Manager
Manages download queue and file completion tracking for Soulseek downloads.
Monitors /downloads folder for completed files and matches them to queue items.

NOTE: PostgreSQL-only implementation to avoid SQLite database locking issues.
SQLite has limited concurrent access handling, causing 'database is locked' errors
during parallel scan operations. PostgreSQL provides reliable concurrent access.
"""

import os
import re
import psycopg2
import psycopg2.extras
import json
import logging
import time
import threading
import yaml
import requests
from difflib import SequenceMatcher
from datetime import datetime, timedelta
from pathlib import Path
from helpers.db_utils import get_db_connection, is_postgres_configured, _is_postgres_connection
from helpers.metadata_reader import read_mp3_metadata
from api_clients import session  # Use shared session with retry logic & connection pooling
from api_clients.musicbrainz import _USER_AGENT as MUSICBRAINZ_USER_AGENT
from download_file_verification import verify_file_in_music, mark_queue_item_moved

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [Download Queue] %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("/config/download_queue.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# PostgreSQL configuration (required - SQLite not supported)
PG_HOST = os.environ.get("PG_HOST")
PG_USER = os.environ.get("PG_USER") 
PG_PASSWORD = os.environ.get("PG_PASSWORD")
PG_DATABASE = os.environ.get("PG_DATABASE", "sptnr")
PG_PORT = os.environ.get("PG_PORT", "5432")
DB_PATH = os.environ.get("DB_PATH", "/database/sptnr.db")  # Kept for backward compatibility logging

# Global state for tracking scan progress (used by /api/downloads/scan-progress)
_scan_progress = {
    'scanning': False,
    'files_found': 0,
    'recent_files': [],  # List of last 50 files found
    'start_time': None,
    'current_path': '',
}

# Validate PostgreSQL configuration at module load time
def _validate_postgres_config():
    """Ensure PostgreSQL is configured - SQLite is no longer supported due to locking issues"""
    if not is_postgres_configured():
        error_msg = (
            "❌ PostgreSQL configuration is REQUIRED but not fully configured.\n"
            "   SQLite is no longer supported due to 'database is locked' errors with concurrent access.\n"
            "   Please set these environment variables:\n"
            "   - DATABASE_URL or PG_DSN (recommended), OR\n"
            "   - PG_HOST (e.g., 'db.example.com')\n"
            "   - PG_USER (e.g., 'sptnr')\n"
            "   - PG_DATABASE (e.g., 'sptnr', default if not set)\n"
            "   - PG_PASSWORD (optional)\n"
            "   - PG_PORT (optional, default: 5432)"
        )
        logger.error(error_msg)
        raise RuntimeError(error_msg)
    
    logger.info(f"✓ PostgreSQL configured: {PG_USER}@{PG_HOST}:{PG_PORT}/{PG_DATABASE}")

# Validate config on module import
try:
    _validate_postgres_config()
except RuntimeError as e:
    logger.error(f"FATAL: {e}")
    # Don't re-raise to allow module to load, but database operations will fail with clear error

# Throttle repetitive empty-scan logs to avoid warning spam when downloads folder is idle.
_last_no_audio_log_at = 0.0
_NO_AUDIO_LOG_INTERVAL_SECONDS = 600

_queue_schema_checked = False
_queue_schema_lock = threading.Lock()

# Throttle expensive downloads-folder checks triggered by frequent UI polling.
_downloads_check_lock = threading.Lock()
_downloads_check_cache = {
    'timestamp': 0.0,
    'result': []
}
_DOWNLOADS_CHECK_MIN_INTERVAL_SECONDS = 20

# In-memory event queue for displaying logs on UI (keep last 200 events)
_queue_events = []
_queue_events_lock = threading.Lock()
_MAX_QUEUE_EVENTS = 200

def log_queue_event(event_type, message, item_id=None, details=None):
    """Log a download queue event for UI display.
    
    Args:
        event_type: 'file_found', 'status_change', 'error', 'info'
        message: Human-readable event message
        item_id: Optional queue item ID
        details: Optional dict with additional context
    """
    global _queue_events
    
    event = {
        'timestamp': datetime.now().isoformat(),
        'type': event_type,
        'message': message,
        'item_id': item_id,
        'details': details or {}
    }
    
    with _queue_events_lock:
        _queue_events.append(event)
        # Keep only last 200 events
        if len(_queue_events) > _MAX_QUEUE_EVENTS:
            _queue_events = _queue_events[-_MAX_QUEUE_EVENTS:]
    
    # Also log to file
    logger.info(f"[QUEUE_EVENT] {event_type}: {message}" + (f" (item_id={item_id})" if item_id else ""))

def get_queue_events(limit=50, event_type=None):
    """Get recent queue events for UI display.
    
    Args:
        limit: Max number of events to return
        event_type: Filter by event type (optional)
    
    Returns:
        List of events in reverse chronological order (newest first)
    """
    with _queue_events_lock:
        events = list(reversed(_queue_events))
        
        if event_type:
            events = [e for e in events if e['type'] == event_type]
        
        return events[:limit]

def clear_queue_events_for_items(queue_ids):
    """Remove event log entries for deleted queue items.
    
    This prevents stale log entries from appearing as if items are still processing
    after they've been removed from the queue.
    
    Args:
        queue_ids: Single queue ID (int/str) or list of queue IDs to remove events for
    """
    global _queue_events
    
    # Normalize to set for comparison
    if not isinstance(queue_ids, (list, tuple, set)):
        queue_ids = [queue_ids]
    queue_ids_to_remove = set(str(qid) for qid in queue_ids)
    
    with _queue_events_lock:
        # Filter out events for deleted queue items
        _queue_events = [
            e for e in _queue_events 
            if e.get('item_id') is None or str(e.get('item_id')) not in queue_ids_to_remove
        ]
    
    if queue_ids_to_remove:
        logger.debug(f"Cleared event log entries for queue items: {queue_ids_to_remove}")

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
    def _pick_existing_downloads_path(path: str) -> str:
        if not path:
            return path
        normalized = os.path.normpath(path)
        if os.path.basename(normalized).lower() == "downloads":
            music_subdir = os.path.join(normalized, "Music")
            # Prefer /downloads/Music when present, but gracefully fall back to /downloads
            # for setups that download directly into the root downloads folder.
            if os.path.isdir(music_subdir):
                return music_subdir
            return normalized
        return normalized

    config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")
    try:
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            configured = (cfg.get("downloads") or {}).get("folder")
            if configured:
                return _pick_existing_downloads_path(configured)
    except Exception as e:
        logger.warning(f"Could not read downloads folder from config: {e}")

    env_dir = os.environ.get("DOWNLOADS_DIR")
    if env_dir:
        return _pick_existing_downloads_path(env_dir)

    return "/downloads/Music"


def resolve_music_dir():
    """Resolve music library root from config/env with robust fallback."""
    config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")
    try:
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            configured = ((cfg.get("navidrome") or {}).get("music_folder") or "").strip()
            if configured:
                return os.path.normpath(configured)
    except Exception as e:
        logger.warning(f"Could not read music folder from config: {e}")

    return os.path.normpath(
        os.environ.get("MUSIC_ROOT")
        or os.environ.get("MUSIC_FOLDER")
        or "/music"
    )


def get_downloads_dir():
    """Dynamically get downloads directory (re-evaluates on each call for config changes)."""
    return resolve_downloads_dir()

MUSIC_DIR = resolve_music_dir()


def retry_on_db_lock(max_retries=3, initial_delay=0.5):
    """Decorator to retry database operations on transient PostgreSQL errors"""
    def decorator(func):
        def wrapper(*args, **kwargs):
            delay = initial_delay
            last_error = None
            
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except (psycopg2.DatabaseError, psycopg2.OperationalError) as e:
                    last_error = e
                    if attempt < max_retries - 1:
                        time.sleep(delay)
                        delay = min(delay * 2, 5.0)  # Exponential backoff, max 5 seconds
                        logger.warning(f"Database error, retrying (attempt {attempt + 1}/{max_retries})...")
                        continue
                    raise
            
            if last_error:
                raise last_error
        return wrapper
    return decorator


def get_db():
    """Get PostgreSQL database connection (SQLite no longer supported due to locking issues)"""
    conn = get_db_connection()
    if not _is_postgres_connection(conn):
        try:
            conn.close()
        except Exception:
            pass
        raise RuntimeError(
            "download_queue_manager requires PostgreSQL. "
            "SQLite connections are not supported for queue manager operations."
        )
    try:
        conn.set_session(autocommit=False)
    except Exception:
        pass
    return conn


def _ensure_download_queue_columns(conn, cursor, is_pg=True):
    """Ensure expected queue columns exist (PostgreSQL only)"""
    global _queue_schema_checked
    if _queue_schema_checked:
        return

    with _queue_schema_lock:
        if _queue_schema_checked:
            return

        try:
            # PostgreSQL-only column checking
            cursor.execute("""
                SELECT column_name FROM information_schema.columns
                WHERE table_name = 'download_queue'
                  AND table_schema = 'public'
                ORDER BY column_name
            """)
            columns = [row['column_name'] for row in cursor.fetchall()]

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
                'release_mbid': "TEXT",
                'recording_mbid': "TEXT",
                'release_year': "INTEGER",
                'duration': "INTEGER",
                'matched_file_path': "TEXT",
                'in_collection': "INTEGER DEFAULT 0",
                'collection_track_id': "TEXT",
                'collection_matched_at': "TEXT",
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
                        # Rollback failed ALTER on error to recover transaction
                        try:
                            conn.rollback()
                        except:
                            pass
                        logger.warning(f"Could not add {col} column: {e}")

            # Prevent duplicate active queue rows under concurrent enqueue requests.
            # 'in_collection' is treated as a terminal state (like 'completed') and must be
            # excluded so that re-queueing a track whose prior row was cleared to in_collection
            # does not trigger a unique-key conflict.
            try:
                # Drop the existing index before recreating to ensure the WHERE clause is
                # always up-to-date with the current definition.  IF NOT EXISTS on the CREATE
                # acts as a safety net in case the drop was silently skipped.
                cursor.execute(
                    "DROP INDEX IF EXISTS uq_download_queue_active_identity"
                )
                conn.commit()
                cursor.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_download_queue_active_identity
                    ON download_queue (LOWER(artist), LOWER(COALESCE(album, '')), LOWER(title), source)
                    WHERE status NOT IN ('completed', 'deleted', 'imported', 'removed', 'cancelled', 'in_collection')
                    """
                )
                conn.commit()
            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                logger.warning(f"Could not create active queue dedupe index: {e}")

            _queue_schema_checked = True
        except Exception as e:
            logger.warning(f"Schema check failed: {e}")
            try:
                conn.rollback()
            except:
                pass


def execute_write_with_retry(cursor, conn, query, params=(), context="database write", max_retries=5, initial_delay=0.1):
    """Execute a write query with commit retry on transient errors (SQLite lock or psycopg2 serialization)."""
    import sqlite3 as _sqlite3
    delay = initial_delay
    for attempt in range(max_retries):
        try:
            cursor.execute(query, params)
            conn.commit()
            return True
        except (_sqlite3.OperationalError, psycopg2.OperationalError) as e:
            if attempt < max_retries - 1 and any(
                kw in str(e).lower() for kw in ('database is locked', 'deadlock', 'could not serialize')
            ):
                logger.warning(
                    f"Database contention during {context}, retrying in {delay:.2f}s "
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


def _add_queue_item_to_tracks_table(conn, cursor, is_pg, artist, title, album, album_artist,
                                     track_number, year, duration, disc_number, release_mbid,
                                     recording_mbid, queue_id, status):
    """
    Sync queue item to tracks table for consistent tracking across pages.
    Similar to how Navidrome imports work - adds tracks immediately to main database.
    
    Uses a special file_path marker to indicate this is a queued download, not a completed track.
    When the download completes, this record will be updated with the actual file_path.
    """
    try:
        # Generate a unique track ID for this queue item
        track_id = f"queue_{queue_id}"
        
        # Special marker for queued downloads
        file_path_marker = f"__queued_for_download__queue_id_{queue_id}"
        
        placeholder = "%s" if is_pg else "?"
        
        # Use UPSERT pattern to avoid duplicates
        if is_pg:
            cursor.execute(f"""
                INSERT INTO tracks (
                    id, artist, album, title, album_artist, track_number, year,
                    duration, disc_number, mbid, suggested_mbid, file_path,
                    score, spotify_score, lastfm_score, listenbrainz_score, age_score,
                    stars, is_single, single_confidence, last_scanned
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    0, 0, 0, 0, 0,
                    0, FALSE, 'unknown', CURRENT_TIMESTAMP
                )
                ON CONFLICT (id) DO UPDATE SET
                    artist = EXCLUDED.artist,
                    album = EXCLUDED.album,
                    title = EXCLUDED.title,
                    album_artist = EXCLUDED.album_artist,
                    track_number = EXCLUDED.track_number,
                    year = EXCLUDED.year,
                    duration = EXCLUDED.duration,
                    disc_number = EXCLUDED.disc_number,
                    mbid = EXCLUDED.mbid,
                    suggested_mbid = EXCLUDED.suggested_mbid,
                    file_path = EXCLUDED.file_path,
                    last_scanned = CURRENT_TIMESTAMP
            """, (
                track_id, artist, album, title, album_artist or artist, track_number, year,
                duration, disc_number, recording_mbid, release_mbid, file_path_marker
            ))
        else:
            cursor.execute(f"""
                INSERT OR REPLACE INTO tracks (
                    id, artist, album, title, album_artist, track_number, year,
                    duration, disc_number, mbid, suggested_mbid, file_path,
                    score, spotify_score, lastfm_score, listenbrainz_score, age_score,
                    stars, is_single, single_confidence, last_scanned
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?,
                    0, 0, 0, 0, 0,
                    0, 0, 'unknown', datetime('now')
                )
            """, (
                track_id, artist, album, title, album_artist or artist, track_number, year,
                duration, disc_number, recording_mbid, release_mbid, file_path_marker
            ))
        
        conn.commit()
        logger.debug(f"Synced queue item {queue_id} to tracks table as {track_id}")
        
    except Exception as e:
        # Import might not exist yet, or tracks table might be missing columns
        # Non-fatal error - queue still works without this
        logger.debug(f"Could not sync queue item to tracks table: {e}")
        try:
            conn.rollback()
        except:
            pass


def add_to_queue(artist, title, album=None, source='soulseek', priority=5, import_group=None, import_type='song',
                 track_number=None, album_artist=None, year=None, release_id=None, release_source=None,
                 duration=None, disc_number=None, release_mbid=None, recording_mbid=None, status=None,
                 matched_file_path=None):
    """
    Add a song to the download queue with comprehensive metadata and duplicate detection
    
    Args:
        artist: Artist name
        title: Song title
        album: Album name (optional)
        source: 'soulseek', 'qbittorrent', or 'local'
        priority: Priority level (1-10, lower = higher priority)
        import_group: Group ID for batch imports (optional, e.g., for albums/playlists)
        import_type: Type of import - 'song', 'album', or 'playlist' (defaults to 'song')
        track_number: Track number from MusicBrainz/Discogs (optional)
        album_artist: Album artist from MusicBrainz/Discogs (optional)
        year: Release year from MusicBrainz/Discogs (optional)
        release_id: MusicBrainz/Discogs release ID (optional)
        release_source: Source of metadata - 'musicbrainz' or 'discogs' (optional)
        duration: Track duration in seconds (optional)
        disc_number: Disc number for multi-disc albums (optional)
        release_mbid: MusicBrainz release ID (optional)
        recording_mbid: MusicBrainz recording ID (optional)
        status: Initial status (optional, defaults to 'queued' or detected duplicate/collection status)
        matched_file_path: File path if already matched (for unmatched files workflow)
    
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

        # Normalize duration to seconds. Some MusicBrainz paths supply milliseconds.
        if duration not in (None, ""):
            try:
                duration = float(duration)
                if duration <= 0:
                    duration = None
                else:
                    if duration > 10000:
                        duration = duration / 1000.0
                    duration = int(round(duration))
            except (TypeError, ValueError):
                duration = None
        
        # Duplicate detection: Check for existing active entry with same (artist, album, title, source).
        # The status exclusion list mirrors the uq_download_queue_active_identity partial index so that
        # the pre-check catches exactly the rows the unique constraint covers, preventing spurious
        # IntegrityErrors from race conditions between concurrent queue additions.

        if album:  # Only check duplicates if album is provided
            duplicate_check_query = """
                SELECT * FROM download_queue
                WHERE LOWER(artist) = LOWER(?) AND LOWER(COALESCE(album, '')) = LOWER(COALESCE(?, '')) AND LOWER(title) = LOWER(?)
                AND source = ?
                AND status NOT IN ('completed', 'deleted', 'imported', 'removed', 'cancelled', 'in_collection')
                ORDER BY created_at ASC
                LIMIT 1
            """ if not is_pg else """
                SELECT * FROM download_queue
                WHERE LOWER(artist) = LOWER(%s) AND LOWER(COALESCE(album, '')) = LOWER(COALESCE(%s, '')) AND LOWER(title) = LOWER(%s)
                AND source = %s
                AND status NOT IN ('completed', 'deleted', 'imported', 'removed', 'cancelled', 'in_collection')
                ORDER BY created_at ASC
                LIMIT 1
            """
            
            cursor.execute(duplicate_check_query, (artist, album, title, source))
            existing = cursor.fetchone()
            
            if existing:
                existing_id = existing[0] if isinstance(existing, tuple) else existing.get('id')
                logger.info(f"Duplicate skipped: {artist} - {title} already in queue (ID {existing_id})")
                conn.close()
                return dict(existing) if hasattr(existing, 'keys') else None
        
        # If no album provided, check by artist + title + source only
        elif not album:
            duplicate_check_query = """
                SELECT * FROM download_queue
                WHERE LOWER(artist) = LOWER(?) AND LOWER(title) = LOWER(?)
                AND COALESCE(album, '') = ''
                AND source = ?
                AND status NOT IN ('completed', 'deleted', 'imported', 'removed', 'cancelled', 'in_collection')
                ORDER BY created_at ASC
                LIMIT 1
            """ if not is_pg else """
                SELECT * FROM download_queue
                WHERE LOWER(artist) = LOWER(%s) AND LOWER(title) = LOWER(%s)
                AND COALESCE(album, '') = ''
                AND source = %s
                AND status NOT IN ('completed', 'deleted', 'imported', 'removed', 'cancelled', 'in_collection')
                ORDER BY created_at ASC
                LIMIT 1
            """
            
            cursor.execute(duplicate_check_query, (artist, title, source))
            existing = cursor.fetchone()
            
            if existing:
                existing_id = existing[0] if isinstance(existing, tuple) else existing.get('id')
                logger.info(f"Duplicate skipped: {artist} - {title} already in queue (ID {existing_id})")
                conn.close()
                return dict(existing) if hasattr(existing, 'keys') else None
        
        # No duplicate found, proceed with insertion
        is_duplicate = False
        duplicate_of_id = None
        auto_delete_at = None
        initial_status = status if status else 'queued'
        
        # Collection matching: Check if track already exists in Navidrome collection
        in_collection = False
        collection_track_id = None
        
        if release_mbid or release_id:  # Only check if we have MBID
            mbid_to_check = release_mbid or release_id
            collection_check_query = """
                SELECT id, file_path FROM tracks
                WHERE LOWER(artist) = LOWER(?) AND LOWER(title) = LOWER(?)
                AND (release_group_mbid = ? OR suggested_mbid = ?)
                LIMIT 1
            """ if not is_pg else """
                SELECT id, file_path FROM tracks
                WHERE LOWER(artist) = LOWER(%s) AND LOWER(title) = LOWER(%s)
                AND (release_group_mbid = %s OR suggested_mbid = %s)
                LIMIT 1
            """
            
            try:
                cursor.execute(collection_check_query, (artist, title, mbid_to_check, mbid_to_check))
                collection_track = cursor.fetchone()
                
                if collection_track:
                    in_collection = True
                    collection_track_id = collection_track[0] if isinstance(collection_track, tuple) else collection_track.get('id')
                    initial_status = 'in_collection'
                    logger.info(f"Track already in collection: {artist} - {title} (track ID {collection_track_id})")
            except Exception as e_collection:
                # tracks table might not exist or columns missing, that's okay
                # CRITICAL: Must rollback transaction to recover from failed query
                try:
                    conn.rollback()
                except:
                    pass
                logger.debug(f"Collection check error (table may not exist): {e_collection}")

        
        # Prepare release_year from year parameter (normalize to INTEGER)
        release_year = None
        if year:
            try:
                release_year = int(year) if str(year).isdigit() else None
            except (ValueError, TypeError):
                release_year = None
        
        committed = False
        try:
            if is_pg:
                cursor.execute(
                    """
                    INSERT INTO download_queue 
                    (artist, title, album, search_query, source, status, priority, file_path, import_group, import_type, 
                     track_number, album_artist, year, release_id, release_source,
                     duration, disc_number, release_mbid, recording_mbid, release_year, matched_file_path,
                     in_collection, collection_track_id, collection_matched_at,
                     created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, NULL, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s,
                            %s, %s, %s,
                            CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    RETURNING id
                    """,
                    (artist, title, album, search_query, source, initial_status, priority, import_group, import_type,
                     track_number, album_artist, year, release_id, release_source,
                     duration, disc_number, release_mbid, recording_mbid, release_year, matched_file_path,
                     1 if in_collection else 0, collection_track_id,
                     datetime.now().isoformat() if in_collection else None),
                )
                inserted = cursor.fetchone()
                conn.commit()
                committed = True
                queue_id = inserted['id'] if hasattr(inserted, 'keys') else inserted[0]
            else:
                placeholder = "?"
                execute_write_with_retry(
                    cursor,
                    conn,
                    f"""
                    INSERT INTO download_queue 
                    (artist, title, album, search_query, source, status, priority, file_path, import_group, import_type, 
                     track_number, album_artist, year, release_id, release_source,
                     duration, disc_number, release_mbid, recording_mbid, release_year, matched_file_path,
                     in_collection, collection_track_id, collection_matched_at,
                     created_at, updated_at)
                    VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, NULL, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder},
                            {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder},
                            {placeholder}, {placeholder}, {placeholder},
                            CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """,
                    (artist, title, album, search_query, source, initial_status, priority, import_group, import_type,
                     track_number, album_artist, year, release_id, release_source,
                     duration, disc_number, release_mbid, recording_mbid, release_year, matched_file_path,
                     1 if in_collection else 0, collection_track_id,
                     datetime.now().isoformat() if in_collection else None),
                    context="add_to_queue insert",
                    max_retries=8,
                    initial_delay=0.2,
                )
                committed = True
                queue_id = cursor.lastrowid
            
            logger.info(f"Added to queue: {search_query} (ID: {queue_id}, source: {source}, status: {initial_status})")
            
            # Also add/update in tracks table for consistent tracking across pages
            # Similar to how Navidrome imports work
            try:
                _add_queue_item_to_tracks_table(
                    conn, cursor, is_pg,
                    artist=artist,
                    title=title,
                    album=album,
                    album_artist=album_artist,
                    track_number=track_number,
                    year=year,
                    duration=duration,
                    disc_number=disc_number,
                    release_mbid=release_mbid,
                    recording_mbid=recording_mbid,
                    queue_id=queue_id,
                    status=initial_status
                )
            except Exception as e_tracks:
                # Non-fatal - queue still created successfully
                logger.warning(f"Failed to sync queue item to tracks table: {e_tracks}")
            
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
            if not committed:
                try:
                    conn.rollback()
                except Exception:
                    pass
            try:
                conn.close()
            except Exception:
                pass
        
    except psycopg2.IntegrityError as e:
        # Duplicate key race condition: two concurrent add_to_queue calls both passed the
        # pre-check before either committed. Log at WARNING (not ERROR) since this is a
        # handled, non-fatal edge case that resolves itself by returning the existing item.
        logger.warning(f"Duplicate key skipped for {artist!r} - {title!r} (source={source}): {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        # Unique-index dedupe race: return existing active item when available.
        try:
            conn2 = get_db()
            cursor2 = conn2.cursor()
            if album:
                cursor2.execute(
                    """
                    SELECT * FROM download_queue
                    WHERE LOWER(artist) = LOWER(%s)
                    AND LOWER(COALESCE(album, '')) = LOWER(%s)
                    AND LOWER(title) = LOWER(%s)
                    AND source = %s
                    AND status NOT IN ('completed', 'deleted', 'imported', 'removed', 'cancelled', 'in_collection')
                    ORDER BY created_at ASC
                    LIMIT 1
                    """,
                    (artist, album, title, source),
                )
            else:
                cursor2.execute(
                    """
                    SELECT * FROM download_queue
                    WHERE LOWER(artist) = LOWER(%s)
                    AND LOWER(COALESCE(album, '')) = ''
                    AND LOWER(title) = LOWER(%s)
                    AND source = %s
                    AND status NOT IN ('completed', 'deleted', 'imported', 'removed', 'cancelled', 'in_collection')
                    ORDER BY created_at ASC
                    LIMIT 1
                    """,
                    (artist, title, source),
                )
            existing = cursor2.fetchone()
            conn2.close()
            if existing:
                return dict(existing)
        except Exception as lookup_err:
            logger.warning(f"Could not resolve dedupe race by lookup: {lookup_err}")
        return None
    except psycopg2.DatabaseError as e:
        logger.error(f"Database error adding to queue: {e}")
        try:
            conn.rollback()
        except:
            pass
        return None
    except Exception as e:
        logger.error(f"Error adding to queue: {type(e).__name__}: {e}")
        import traceback
        logger.error(traceback.format_exc())
        try:
            conn.rollback()
        except:
            pass
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
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # First ensure required columns exist (PostgreSQL information_schema)
        cursor.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'download_queue' AND table_schema = 'public'
        """)
        # RealDictCursor returns dict-like rows, so use key lookup.
        columns = [row.get('column_name') for row in cursor.fetchall() if row.get('column_name')]

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

        is_pg = True  # PostgreSQL is now required
        placeholder = "%s"

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
                is_pg = isinstance(conn, psycopg2.extensions.connection)
            
            cursor = conn.cursor()
            placeholder = "%s" if is_pg else "?"
            
            # Build update query
            updates = []
            params = []

            # Guardrail: completed rows must always have a file_path, either newly
            # provided or already persisted on the queue item.
            if kwargs.get('status') == 'completed':
                new_file_path = kwargs.get('file_path')
                if not new_file_path:
                    cursor.execute(
                        f"SELECT file_path FROM download_queue WHERE id = {placeholder}",
                        (queue_id,)
                    )
                    existing_row = cursor.fetchone()
                    existing_file_path = None
                    if existing_row:
                        existing_file_path = (
                            existing_row.get('file_path')
                            if hasattr(existing_row, 'get')
                            else existing_row[0]
                        )

                    if not existing_file_path:
                        logger.warning(
                            f"Refusing to mark queue item {queue_id} as completed without file_path"
                        )
                        conn.close()
                        return None
            
            for key, value in kwargs.items():
                if key in ['status', 'source_id', 'found_filename', 'file_path', 'failure_reason', 
                           'retry_count', 'last_failure_time', 'imported_at', 'metadata', 'import_group', 'import_type',
                           'copied_individually', 'copied_individually_at', 'duration']:
                    # Special handling for file_path to avoid UNIQUE constraint issues
                    if key == 'file_path' and value:
                        # Check if this file_path is already in use by another item
                        cursor.execute(f"SELECT COUNT(*) as cnt FROM download_queue WHERE file_path = {placeholder} AND id != {placeholder}", 
                                     (value, queue_id))
                        result = cursor.fetchone()
                        cnt = result['cnt'] if hasattr(result, 'keys') else (result[0] if result else 0)
                        if cnt > 0:
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
        
        except psycopg2.OperationalError as e:
            last_error = e
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 5.0)
                logger.warning(f"DB operational error updating queue item {queue_id}, retrying (attempt {attempt + 1}/{max_retries})...")
                continue
            logger.error(f"OperationalError updating queue item {queue_id}: {e}")
            return None
        except psycopg2.IntegrityError as e:
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
                is_pg = isinstance(conn, psycopg2.extensions.connection)
            
            cursor = conn.cursor()
            placeholder = "%s" if is_pg else "?"
            
            # Get current retry count
            cursor.execute(f"SELECT retry_count, max_retries FROM download_queue WHERE id = {placeholder}", (queue_id,))
            row = cursor.fetchone()
            
            if not row:
                conn.close()
                return None
            
            retry_count = (row['retry_count'] if hasattr(row, 'keys') else row[0] or 0) + 1
            max_r = row['max_retries'] if hasattr(row, 'keys') else row[1]
            next_retry = datetime.now() + timedelta(minutes=retry_delay_minutes)
            
            # Check if we've exceeded max retries
            if max_r and retry_count >= max_r:
                new_status = 'failed'
                logger.warning(f"Queue item {queue_id} exceeded max retries ({retry_count}/{max_r}): {reason}")
            else:
                new_status = 'queued'
                logger.info(f"Queue item {queue_id} scheduled for retry (attempt {retry_count}/{max_r}) at {next_retry}: {reason}")
            
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
        
        except psycopg2.OperationalError as e:
            if attempt < max_retries - 1:
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 5.0)
                logger.warning(f"DB operational error marking failed, retrying (attempt {attempt + 1}/{max_retries})...")
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
    Clear all items from the download queue and their event logs.
    
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
            # Clear everything - wipe event logs completely
            global _queue_events
            with _queue_events_lock:
                _queue_events = []
            logger.debug("Cleared all queue event log entries")
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


def _build_release_import_group(artist, album):
    """Create a stable import_group key for discovered release grouping."""
    safe_artist = _sanitize_path_component((artist or 'Unknown Artist').strip())
    safe_album = _sanitize_path_component((album or 'Unknown Album').strip())
    group = f"discovered_{safe_artist}_{safe_album}"
    return group.replace(' ', '_')[:100]


def move_single_track_to_music_dir(queue_item_dict, music_dir=None):
    """
    Copy a single completed track from /downloads into the /music library tree.

    Folder structure: <music_root>/<album_artist>/<year> - <album>/
    (year defaults to 'Unknown' when not available)
    """
    import re
    import shutil

    def _extract_year(value):
        if value is None:
            return None
        m = re.search(r"(19|20)\d{2}", str(value))
        return m.group(0) if m else None

    try:
        file_path = queue_item_dict.get('file_path')
        if not file_path:
            queue_id = queue_item_dict.get('id', 'unknown')
            return {'success': False, 'target_path': None, 'error': f'Queue item {queue_id} has no file_path (may not be completed yet)'}
        if not os.path.exists(file_path):
            return {'success': False, 'target_path': None, 'error': f'File not found: {file_path}'}

        music_root = music_dir or resolve_music_dir()

        album_artist = _sanitize_path_component(
            queue_item_dict.get('album_artist') or queue_item_dict.get('artist') or 'Unknown Artist'
        )
        album = _sanitize_path_component(queue_item_dict.get('album') or 'Unknown Album')
        artist = queue_item_dict.get('artist', 'Unknown Artist')
        title = queue_item_dict.get('title', 'Unknown Title')
        track_num = queue_item_dict.get('track_number', '00')
        disc_num = queue_item_dict.get('disc_number', 1)
        ext = os.path.splitext(file_path)[1].lower()

        # Resolve year with priority:
        # 1) queue year, 2) embedded file tags, 3) MusicBrainz release metadata
        year = _extract_year(
            queue_item_dict.get('year')
            or queue_item_dict.get('release_year')
            or queue_item_dict.get('mb_matched_year')
        )
        if not year:
            try:
                embedded = read_mp3_metadata(file_path)
                year = _extract_year(embedded.get('year') or embedded.get('date'))
            except Exception:
                year = None

        tag_metadata = {
            'title': title,
            'artist': artist,
            'album_artist': queue_item_dict.get('album_artist') or artist,
            'album': queue_item_dict.get('album') or 'Unknown Album',
            'year': year or '',
            'track_number': queue_item_dict.get('track_number'),
            # Default: suppress disc tag until MusicBrainz confirms multi-disc
            'disc_number': None,
        }

        cover_art_data = None
        release_id = queue_item_dict.get('release_id')
        if release_id:
            try:
                from post_download_processor import fetch_musicbrainz_release_metadata
                mb_release = fetch_musicbrainz_release_metadata(release_id)

                if mb_release:
                    if not year:
                        year = _extract_year(
                            mb_release.get('first_release_date')
                            or mb_release.get('date')
                            or mb_release.get('year')
                        )

                    # If the release has only one disc, strip the disc number tag entirely
                    is_single_disc = mb_release.get('disc_count', 1) <= 1

                    for track in mb_release.get('tracks', []):
                        track_title = track.get('title', '').lower().strip()
                        queue_title = title.lower().strip()
                        if track_title and queue_title and track_title == queue_title:
                            if is_single_disc:
                                disc_num = None
                                tag_metadata['disc_number'] = None
                                logger.info(
                                    f"[COPY] Queue {queue_item_dict.get('id', 'unknown')}: "
                                    f"Single-disc release — disc_number removed from tags"
                                )
                            else:
                                disc_num = track.get('disc_number', disc_num)
                                tag_metadata['disc_number'] = disc_num
                                logger.info(
                                    f"[COPY] Queue {queue_item_dict.get('id', 'unknown')}: "
                                    f"Updated disc_number from MusicBrainz: {disc_num}"
                                )
                            break

                    if mb_release.get('cover_art'):
                        cover_art_data = mb_release['cover_art']
                        logger.info(f"[COPY] Queue {queue_item_dict.get('id', 'unknown')}: Using MusicBrainz album art")
            except Exception as mb_err:
                logger.warning(f"[COPY] Could not fetch MusicBrainz metadata for release {release_id}: {mb_err}")

        if not year:
            year = 'Unknown'
        tag_metadata['year'] = year

        dest_folder = os.path.join(music_root, album_artist, f"{year} - {album}")
        os.makedirs(dest_folder, exist_ok=True)

        try:
            track_num_int = int(str(track_num).split('/')[0]) if track_num else 0
            disc_num_int = int(str(disc_num).split('/')[0]) if disc_num else 1
            if disc_num_int > 1:
                track_num_fmt = f"{disc_num_int}{track_num_int:02d}"
            else:
                track_num_fmt = f"{track_num_int:02d}"
        except Exception:
            track_num_fmt = "00"

        try:
            from post_download_processor import update_file_metadata_with_albumart
            update_file_metadata_with_albumart(file_path, tag_metadata, cover_art_data)
        except Exception as tag_err:
            logger.warning(f"[COPY] Could not update file tags before copy (non-fatal): {tag_err}")

        filename = _sanitize_path_component(f"{track_num_fmt}. {artist} - {title}{ext}")
        dest_path = os.path.join(dest_folder, filename)

        if os.path.exists(dest_path):
            logger.info(f"[COPY] Destination already exists, skipping copy: {dest_path}")
            return {'success': True, 'target_path': dest_path, 'error': None, 'skipped': True}

        shutil.copy2(file_path, dest_path)
        logger.info(f"[COPY] {filename} → {dest_path}")
        return {'success': True, 'target_path': dest_path, 'error': None}

    except Exception as e:
        logger.error(f"[COPY] Failed to copy file: {e}")
        return {'success': False, 'target_path': None, 'error': str(e)}


def rename_album_files(artist, album, db_conn, music_dir=None):
    """
    Rename all files in an album based on current metadata.
    
    This function:
    1. Fetches all tracks for the album from the database
    2. Calculates new file paths based on current album_artist/artist/album metadata
    3. Moves files to new paths maintaining disc/track numbering
    4. Updates database file_path columns with new locations
    5. Handles naming conflicts using suffix counters
    
    Args:
        artist: Current artist name (from URL/database query)
        album: Current album name (from URL/database query)
        db_conn: Database connection
        music_dir: Base music directory (defaults to MUSIC_DIR env var)
    
    Returns:
        Dict with:
        - success: bool
        - renamed_count: Number of files successfully renamed
        - updated_db_count: Number of database records updated
        - errors: List of error messages
        - details: List of renamed files with old/new paths
    """
    import re
    import shutil
    from pathlib import Path
    
    result = {
        'success': False,
        'renamed_count': 0,
        'updated_db_count': 0,
        'errors': [],
        'details': []
    }
    
    try:
        cursor = db_conn.cursor()
        is_pg = False
        
        # Try to detect if this is PostgreSQL
        try:
            from app import _is_postgres_connection
            is_pg = bool(_is_postgres_connection(db_conn))
        except Exception:
            pass
        
        placeholder = "%s" if is_pg else "?"
        
        # Query all tracks for this album
        # Use COALESCE logic like album_detail to match albums by album_artist
        cursor.execute(f"""
            SELECT id, artist, album, album_artist, title, track_number, disc_number, 
                   file_path, beets_path, year, release_year
            FROM tracks
            WHERE COALESCE(NULLIF(album_artist, ''), artist) = {placeholder} 
              AND album = {placeholder}
            ORDER BY COALESCE(disc_number, 1), COALESCE(track_number, 999), title
        """, (artist, album))
        
        tracks = cursor.fetchall()
        
        if not tracks:
            result['errors'].append(f"No tracks found for album: {artist} - {album}")
            return result
        
        logger.info(f"[RENAME] Starting rename for album: {artist} - {album} ({len(tracks)} tracks)")
        
        music_root = music_dir or MUSIC_DIR
        
        # Process each track
        for track_row in tracks:
            try:
                # Extract track data (handle both dict-like and tuple returns)
                if isinstance(track_row, dict):
                    track_id = track_row.get('id')
                    track_artist = track_row.get('artist', 'Unknown Artist')
                    track_album = track_row.get('album', 'Unknown Album')
                    track_album_artist = track_row.get('album_artist') or track_artist
                    track_title = track_row.get('title', 'Unknown Title')
                    track_number = track_row.get('track_number', '00')
                    disc_number = track_row.get('disc_number', 1)
                    file_path = track_row.get('file_path')
                    year = track_row.get('year') or track_row.get('release_year')
                else:
                    # Tuple format (fallback for SQLite)
                    track_id, track_artist, track_album, track_album_artist, track_title, \
                    track_number, disc_number, file_path, beets_path, year, release_year = track_row
                    year = year or release_year
                    track_album_artist = track_album_artist or track_artist
                
                # Skip tracks without file paths
                if not file_path or file_path.startswith('__queued_'):
                    logger.debug(f"[RENAME] Skipping track {track_id} - no file path or queued")
                    continue
                
                # Check file exists
                if not os.path.exists(file_path):
                    error_msg = f"File not found: {file_path} (track: {track_artist} - {track_title})"
                    logger.warning(f"[RENAME] {error_msg}")
                    result['errors'].append(error_msg)
                    continue
                
                # Extract year
                def _extract_year(value):
                    if value is None:
                        return None
                    m = re.search(r"(19|20)\d{2}", str(value))
                    return m.group(0) if m else None
                
                year_fmt = _extract_year(year)
                if not year_fmt:
                    year_fmt = 'Unknown'
                
                # Build new path: <music_root>/<album_artist>/<year> - <album>/
                album_artist_safe = _sanitize_path_component(track_album_artist or track_artist or 'Unknown Artist')
                album_safe = _sanitize_path_component(track_album or 'Unknown Album')
                
                dest_folder = os.path.join(music_root, album_artist_safe, f"{year_fmt} - {album_safe}")
                os.makedirs(dest_folder, exist_ok=True)
                
                # Format track number
                try:
                    track_num_int = int(str(track_number).split('/')[0]) if track_number else 0
                    disc_num_int = int(str(disc_number).split('/')[0]) if disc_number else 1
                    if disc_num_int > 1:
                        track_num_fmt = f"{disc_num_int}{track_num_int:02d}"
                    else:
                        track_num_fmt = f"{track_num_int:02d}"
                except Exception:
                    track_num_fmt = "00"
                
                # Build new filename
                ext = os.path.splitext(file_path)[1].lower()
                filename = _sanitize_path_component(f"{track_num_fmt}. {track_artist} - {track_title}{ext}")
                new_path = os.path.join(dest_folder, filename)
                
                # Handle conflicts with counter
                if os.path.exists(new_path) and new_path != file_path:
                    base, ext_only = os.path.splitext(filename)
                    counter = 1
                    while os.path.exists(os.path.join(dest_folder, f"{base}_{counter}{ext_only}")):
                        counter += 1
                    new_path = os.path.join(dest_folder, f"{base}_{counter}{ext_only}")
                
                # Only move if path changed
                if new_path != file_path:
                    # Move the file
                    shutil.move(file_path, new_path)
                    logger.info(f"[RENAME] {os.path.basename(file_path)} → {os.path.basename(new_path)}")
                    result['renamed_count'] += 1
                    
                    # Update database
                    try:
                        cursor.execute(f"""
                            UPDATE tracks
                            SET file_path = {placeholder},
                                beets_path = {placeholder},
                                updated_at = {'CURRENT_TIMESTAMP' if is_pg else "datetime('now')"}
                            WHERE id = {placeholder}
                        """, (new_path, new_path, track_id))
                        
                        result['updated_db_count'] += 1
                        result['details'].append({
                            'track_id': track_id,
                            'track': f"{track_artist} - {track_title}",
                            'old_path': file_path,
                            'new_path': new_path
                        })
                    except Exception as db_err:
                        logger.error(f"[RENAME] Failed to update database for {track_id}: {db_err}")
                        result['errors'].append(f"Database update failed for {track_artist} - {track_title}: {db_err}")
                        # Note: file was moved but DB not updated - consider this a partial success
                else:
                    logger.debug(f"[RENAME] Path unchanged for {track_artist} - {track_title}")
                    
            except Exception as track_err:
                error_msg = f"Error renaming track {track_id}: {track_err}"
                logger.error(f"[RENAME] {error_msg}")
                result['errors'].append(error_msg)
        
        # Commit database changes
        try:
            db_conn.commit()
        except Exception as commit_err:
            logger.error(f"[RENAME] Error committing database changes: {commit_err}")
            result['errors'].append(f"Database commit failed: {commit_err}")
            try:
                db_conn.rollback()
            except:
                pass
        
        # Set success flag
        result['success'] = result['renamed_count'] > 0 or len(result['errors']) == 0
        
        logger.info(f"[RENAME] Album rename complete: {result['renamed_count']} renamed, "
                   f"{result['updated_db_count']} DB updated, {len(result['errors'])} errors")
        
        return result
        
    except Exception as e:
        logger.error(f"[RENAME] Fatal error renaming album: {e}")
        import traceback
        logger.error(traceback.format_exc())
        result['errors'].append(f"Fatal error: {str(e)}")
        return result


def _read_track_file_name_format():
    """Read configurable file naming format from config, with sensible default."""
    config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")
    try:
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            fmt = (cfg.get("downloads") or {}).get("file_name_format")
            if isinstance(fmt, str) and fmt.strip():
                return fmt.strip()
    except Exception as cfg_err:
        logger.debug(f"[RENAME] Could not read naming format from config: {cfg_err}")
    return "{album_artist}/{year} - {album}/{track_number}. {artist} - {title}"


def _format_track_number_for_rename(track_number, disc_number=None):
    """Format a track number for use in path format strings."""
    try:
        disc_num = int(str(disc_number).split("/")[0]) if disc_number else 1
        track_num = int(str(track_number).split("/")[0]) if track_number else 0
        if disc_num > 1:
            return f"{disc_num}{track_num:02d}"
        return f"{track_num:02d}"
    except Exception:
        return "00"


def rename_track_file(track_id, db_conn, music_dir=None):
    """
    Rename/move a single track file based on the configured file_name_format from config.

    Reads downloads.file_name_format from config (same setting used by Queue Manager organize).
    Moves the file to the new path and updates the database file_path.

    Args:
        track_id: ID of the track to rename
        db_conn: Database connection
        music_dir: Base music directory (defaults to resolve_music_dir())

    Returns:
        Dict with:
        - success: bool
        - renamed: bool (True if file was actually moved)
        - old_path: original file path
        - new_path: destination file path
        - message: human-readable result
        - error: error message (if failed)
    """
    import re as _re
    import shutil as _shutil

    result = {
        "success": False,
        "renamed": False,
        "old_path": None,
        "new_path": None,
        "message": "",
        "error": "",
    }

    try:
        cursor = db_conn.cursor()
        is_pg = False
        try:
            from app import _is_postgres_connection
            is_pg = bool(_is_postgres_connection(db_conn))
        except Exception:
            pass
        placeholder = "%s" if is_pg else "?"

        cursor.execute(
            f"SELECT id, artist, album, album_artist, title, track_number, disc_number, "
            f"file_path, year FROM tracks WHERE id = {placeholder}",
            (track_id,),
        )
        row = cursor.fetchone()
        if not row:
            result["error"] = f"Track {track_id} not found"
            return result

        track = dict(row)
        file_path = track.get("file_path") or ""
        result["old_path"] = file_path

        if not file_path or file_path.startswith("__queued_"):
            result["error"] = "Track has no file path (may be a queued placeholder)"
            return result

        if not os.path.exists(file_path):
            result["error"] = f"File not found on disk: {file_path}"
            return result

        music_root = music_dir or resolve_music_dir()
        file_name_format = _read_track_file_name_format()

        # Extract year (handle full date strings)
        year_raw = track.get("year") or track.get("release_year")
        year_val = "Unknown"
        if year_raw:
            m = _re.search(r"(19|20)\d{2}", str(year_raw))
            if m:
                year_val = m.group(0)

        track_artist = track.get("artist") or "Unknown Artist"
        album_artist = track.get("album_artist") or track_artist
        album = track.get("album") or "Unknown Album"
        title = track.get("title") or "Unknown Title"
        track_number = _format_track_number_for_rename(
            track.get("track_number"), track.get("disc_number")
        )

        format_vars = {
            "album_artist": _sanitize_path_component(album_artist),
            "year": year_val,
            "album": _sanitize_path_component(album),
            "track_number": track_number,
            "artist": _sanitize_path_component(track_artist),
            "title": _sanitize_path_component(title),
        }

        try:
            relative_path = file_name_format.format(**format_vars)
        except Exception:
            relative_path = (
                f"{format_vars['album_artist']}/{format_vars['year']} - {format_vars['album']}/"
                f"{format_vars['track_number']}. {format_vars['artist']} - {format_vars['title']}"
            )

        # Sanitize each component
        relative_path = relative_path.strip().replace("\\", "/").lstrip("/")
        parts = []
        for part in relative_path.split("/"):
            clean = _sanitize_path_component(part)
            if clean and clean not in (".", ".."):
                parts.append(clean)

        if not parts:
            result["error"] = "Could not build a valid target path from format"
            return result

        ext = os.path.splitext(file_path)[1].lower()
        relative_joined = os.path.join(*parts)
        base_name, ext_from_format = os.path.splitext(relative_joined)
        if not ext_from_format:
            relative_joined = relative_joined + ext

        new_path = os.path.join(music_root, relative_joined)
        new_dir = os.path.dirname(new_path)
        os.makedirs(new_dir, exist_ok=True)

        # Handle filename conflict (path changed AND target already occupied by a different file)
        if os.path.exists(new_path) and os.path.abspath(new_path) != os.path.abspath(file_path):
            stem, file_ext = os.path.splitext(new_path)
            counter = 1
            while os.path.exists(f"{stem}_{counter}{file_ext}"):
                counter += 1
            new_path = f"{stem}_{counter}{file_ext}"

        result["new_path"] = new_path

        if os.path.abspath(new_path) == os.path.abspath(file_path):
            result["success"] = True
            result["renamed"] = False
            result["message"] = "File is already at the correct path"
            return result

        _shutil.move(file_path, new_path)
        logger.info(f"[RENAME] Moved: {file_path!r} -> {new_path!r}")

        cursor.execute(
            f"UPDATE tracks SET file_path = {placeholder}, beets_path = {placeholder} "
            f"WHERE id = {placeholder}",
            (new_path, new_path, track_id),
        )
        db_conn.commit()

        result["success"] = True
        result["renamed"] = True
        result["message"] = f"Renamed to: {os.path.basename(new_path)}"
        return result

    except Exception as e:
        logger.error(f"[RENAME] Error renaming track {track_id}: {e}")
        import traceback
        logger.error(traceback.format_exc())
        result["error"] = str(e)
        return result


def _load_format_bitrate_config():
    """
    Load format/bitrate priority configuration from /config/config.yaml
    
    Returns:
        dict with 'enabled', 'priorities', 'bitrate_tolerance', 'reject_others'
    """
    config = {
        'enabled': False,
        'priorities': [],  # List of {'format': 'mp3', 'bitrate_kbps': 320}, {'format': 'flac', 'bitrate_kbps': None}
        'bitrate_tolerance': 5,  # ±5 kbps tolerance
        'reject_others': False  # Reject files not matching any priority
    }
    
    try:
        config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")
        if not os.path.exists(config_path):
            return config
        
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        
        # Look for download quality settings
        downloads_cfg = cfg.get("downloads") or {}
        quality_cfg = downloads_cfg.get("quality_filter") or {}
        
        if quality_cfg.get("enabled"):
            config['enabled'] = True
            
            # Parse priorities: [{'format': 'mp3', 'bitrate_kbps': 320}, {'format': 'flac'}]
            priorities = quality_cfg.get("priorities", [])
            if isinstance(priorities, list):
                config['priorities'] = priorities
            
            config['bitrate_tolerance'] = quality_cfg.get("bitrate_tolerance", 5)
            config['reject_others'] = quality_cfg.get("reject_others", False)
            
            logger.info(f"[FORMAT-FILTER] Enabled with {len(config['priorities'])} priority rule(s): "
                       f"{config['priorities']}")
    except Exception as e:
        logger.warning(f"[FORMAT-FILTER] Error reading config: {e}")
    
    return config


def _get_file_format_and_bitrate(file_path, file_meta=None):
    """
    Extract format and bitrate from file.
    
    Args:
        file_path: Path to audio file
        file_meta: Optional pre-read metadata dict
    
    Returns:
        dict with 'format' and 'bitrate_kbps' (bitrate in kbps or None for lossless)
    """
    result = {'format': None, 'bitrate_kbps': None}
    
    try:
        # Get format from extension
        ext = os.path.splitext(file_path)[1].lower().lstrip('.')
        if ext in ['mp3', 'flac', 'm4a', 'ogg', 'wav', 'aac']:
            result['format'] = ext
        
        # Get bitrate from metadata if available
        if file_meta and file_meta.get('bitrate'):
            kbps = file_meta['bitrate'] / 1000  # Convert from bps to kbps
            result['bitrate_kbps'] = int(round(kbps))
        elif not file_meta:
            # Try to read metadata
            try:
                file_meta = read_mp3_metadata(file_path)
                if file_meta and file_meta.get('bitrate'):
                    kbps = file_meta['bitrate'] / 1000
                    result['bitrate_kbps'] = int(round(kbps))
            except:
                pass
    except Exception as e:
        logger.warning(f"Failed to extract format/bitrate from {file_path}: {e}")
    
    return result


def _matches_format_bitrate_priority(file_path, file_meta=None):
    """
    Check if file matches configured format/bitrate priorities.
    
    Args:
        file_path: Path to audio file
        file_meta: Optional pre-read metadata dict
    
    Returns:
        dict with 'matches' (bool), 'reason' (str), 'format' (str), 'bitrate_kbps' (int)
    """
    result = {
        'matches': True,
        'reason': 'Format filter disabled or no rules configured',
        'format': None,
        'bitrate_kbps': None
    }
    
    config = _load_format_bitrate_config()
    if not config['enabled'] or not config['priorities']:
        return result
    
    file_info = _get_file_format_and_bitrate(file_path, file_meta)
    result['format'] = file_info['format']
    result['bitrate_kbps'] = file_info['bitrate_kbps']
    
    if not file_info['format']:
        result['matches'] = False
        result['reason'] = 'Could not determine file format'
        return result
    
    tolerance = config['bitrate_tolerance']
    
    # Check if file matches any priority rule
    for priority in config['priorities']:
        priority_format = priority.get('format', '').lower()
        priority_bitrate = priority.get('bitrate_kbps')
        
        if file_info['format'].lower() != priority_format:
            continue  # Format doesn't match this rule
        
        # Format matches - check bitrate if specified
        if priority_bitrate is None:
            # No bitrate requirement (lossless formats)
            result['matches'] = True
            result['reason'] = f'Matches priority: {priority_format}'
            return result
        
        if file_info['bitrate_kbps'] is None:
            # File has no bitrate metadata (might be lossless), skip bitrate check
            result['matches'] = True
            result['reason'] = f'Matches priority: {priority_format} (no bitrate info)'
            return result
        
        # Check if bitrate is within tolerance
        diff = abs(file_info['bitrate_kbps'] - priority_bitrate)
        if diff <= tolerance:
            result['matches'] = True
            result['reason'] = f'Matches priority: {priority_format} {file_info["bitrate_kbps"]} kbps'
            return result
        else:
            logger.debug(f"[FORMAT-FILTER] Bitrate mismatch: {file_path} has "
                        f"{file_info['bitrate_kbps']} kbps, expected {priority_bitrate} ±{tolerance}")
    
    # No priority rules matched
    if config['reject_others']:
        result['matches'] = False
        result['reason'] = f'No matching priority: {file_info["format"]} {file_info["bitrate_kbps"]} kbps'
        log_queue_event('quality_filter_reject', f"Rejected {os.path.basename(file_path)}: {result['reason']}")
        return result
    
    # Accept by default if reject_others is False
    result['matches'] = True
    result['reason'] = f'Accepted (no reject_others): {file_info["format"]} {file_info["bitrate_kbps"]} kbps'
    return result


def _metadata_matches_queue_item(file_meta, queue_item, threshold=0.68, file_path=None):
    """
    Check if discovered file metadata is a good match for a pending queue item.

    Compares artist + title (required), with album as a bonus.
    Also validates format/bitrate against configured priorities if enabled.

    Returns:
        True: metadata exists and strongly matches
        False: metadata exists but mismatches (including format/bitrate rejection)
        None: metadata missing/incomplete, caller can use filename fallback
    """
    # First check format/bitrate priority if file_path provided
    if file_path:
        quality_check = _matches_format_bitrate_priority(file_path, file_meta)
        if not quality_check['matches']:
            logger.info(f"[QUALITY-FILTER] Rejected: {file_path} - {quality_check['reason']}")
            return False
    
    def _sim(a, b):
        if not a or not b:
            return 0.0
        return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()

    file_artist = (file_meta.get('artist') or '').strip()
    file_title = (file_meta.get('title') or '').strip()
    queue_artist = (queue_item.get('artist') or '').strip()
    queue_title = (queue_item.get('title') or '').strip()

    if not file_artist or not file_title or not queue_artist or not queue_title:
        return None

    artist_score = _sim(file_artist, queue_artist)
    title_score = _sim(file_title, queue_title)

    # Each individual field must clear a minimum similarity floor before
    # the weighted average is tested against the overall threshold parameter.
    _FIELD_MIN = 0.55
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
    lock_acquired = False
    try:
        now = time.time()
        cache_age = now - _downloads_check_cache['timestamp']
        if cache_age < _DOWNLOADS_CHECK_MIN_INTERVAL_SECONDS:
            return list(_downloads_check_cache['result'])

        # If a check is already running in this process, return cached data.
        if not _downloads_check_lock.acquire(blocking=False):
            return list(_downloads_check_cache['result'])
        lock_acquired = True

        downloads_dir = get_downloads_dir()
        if not os.path.isdir(downloads_dir):
            logger.warning(f"Downloads folder not found: {downloads_dir}")
            return []
        
        completed_items = []
        conn = get_db()
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        
        # Get all active queue items (not yet completed or imported)
        cursor.execute("""
            SELECT * FROM download_queue 
            WHERE status IN ('queued', 'searching', 'downloading')
            ORDER BY created_at ASC
        """)
        queue_items = cursor.fetchall()
        
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
        
        music_root = os.path.abspath(MUSIC_DIR)

        def _is_within_music(path_value):
            if not path_value:
                return False
            try:
                abs_path = os.path.abspath(path_value)
                return os.path.commonpath([abs_path, music_root]) == music_root
            except Exception:
                return False

        def _find_existing_music_file(queue_item):
            """Find a likely already-imported /music file for an active queue item."""
            try:
                # 1) Strongest match: recording MBID already present on tracks.mbid
                recording_mbid = (queue_item.get('recording_mbid') or '').strip()
                if recording_mbid:
                    cursor.execute(
                        """
                        SELECT file_path
                        FROM tracks
                        WHERE mbid = %s
                          AND file_path IS NOT NULL
                          AND file_path != ''
                          AND file_path NOT LIKE '__queued_for_download__%'
                        ORDER BY id DESC
                        LIMIT 1
                        """,
                        (recording_mbid,)
                    )
                    row = cursor.fetchone()
                    if row:
                        path_value = row.get('file_path') if hasattr(row, 'keys') else row[0]
                        if path_value and _is_within_music(path_value) and os.path.isfile(path_value):
                            return path_value

                # 2) Fallback: artist+title (+album preference) against imported tracks
                artist = (queue_item.get('artist') or '').strip()
                title = (queue_item.get('title') or '').strip()
                album = (queue_item.get('album') or '').strip()
                if not artist or not title:
                    return None

                cursor.execute(
                    """
                    SELECT file_path,
                           CASE
                             WHEN %s != '' AND LOWER(COALESCE(album, '')) = LOWER(%s) THEN 0
                             ELSE 1
                           END AS album_rank
                    FROM tracks
                    WHERE LOWER(COALESCE(artist, '')) = LOWER(%s)
                      AND LOWER(COALESCE(title, '')) = LOWER(%s)
                      AND file_path IS NOT NULL
                      AND file_path != ''
                      AND file_path NOT LIKE '__queued_for_download__%'
                    ORDER BY album_rank ASC, id DESC
                    LIMIT 5
                    """,
                    (album, album, artist, title)
                )
                rows = cursor.fetchall() or []
                for row in rows:
                    path_value = row.get('file_path') if hasattr(row, 'keys') else row[0]
                    if path_value and _is_within_music(path_value) and os.path.isfile(path_value):
                        return path_value

                return None
            except Exception as e:
                logger.debug(f"[RECONCILE] Failed to query tracks for queue item {queue_item.get('id')}: {e}")
                return None

        # Try to match files to queue items
        for queue_item in queue_items:
            import shutil
            import re
            match_found = None
            match_path = None
            
            # Try exact filename match first (but still verify metadata when available)
            if queue_item['found_filename']:
                found_name = str(queue_item['found_filename']).replace('\\', '/').strip()
                basename_candidates = [f for f in downloads_files if f['filename'] == found_name]
                basename_is_unique = len(basename_candidates) == 1

                for file_info in downloads_files:
                    rel_name = file_info['rel_path'].replace('\\', '/')
                    full_name = file_info['full_path'].replace('\\', '/')

                    is_rel_or_full = (rel_name == found_name or full_name == found_name)
                    is_basename = (file_info['filename'] == found_name)

                    if is_rel_or_full or is_basename:
                        metadata = None
                        try:
                            metadata = read_mp3_metadata(file_info['full_path'])
                        except Exception:
                            metadata = None

                        meta_state = _metadata_matches_queue_item(metadata or {}, queue_item, file_path=file_info['full_path'])
                        if meta_state is False:
                            # File metadata doesn't match queue item, skip this file
                            continue

                        # If only basename matches and metadata is missing/neutral,
                        # only accept when basename is unique in this scan pass.
                        if is_basename and not is_rel_or_full and meta_state is not True and not basename_is_unique:
                            continue

                        else:
                            match_found = file_info['filename']
                            match_path = file_info['full_path']
                            break
            
            # If not found by filename, try fuzzy matching based on artist/title
            if not match_found:
                for file_info in downloads_files:
                    metadata = None
                    try:
                        metadata = read_mp3_metadata(file_info['full_path'])
                    except Exception:
                        metadata = None

                    meta_state = _metadata_matches_queue_item(metadata or {}, queue_item, file_path=file_info['full_path'])

                    # If tags exist and disagree, never allow filename fallback.
                    if meta_state is False:
                        continue

                    # Only accept a fuzzy match when metadata strongly matches.
                    # Avoid "first file wins" behavior when metadata is missing.
                    if meta_state is True:
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
                        target_path = move_result['target_path']
                        
                        # Verify file exists at new location before marking as imported
                        verify_result = verify_file_in_music(queue_item['id'], target_path)
                        
                        if verify_result['success']:
                            # File verified - mark as moved and imported
                            mark_queue_item_moved(queue_item['id'], target_path)
                            update_queue_item(
                                queue_item['id'],
                                status='imported',
                                file_path=target_path,
                                copied_individually=1,
                                copied_individually_at=datetime.now().isoformat()
                            )
                            logger.info(
                                f"[MOVE] Queue {queue_item['id']}: verified and imported to {target_path}"
                            )
                            completed_items.append({
                                'queue_id': queue_item['id'],
                                'filename': match_found,
                                'file_path': target_path,
                                'artist': queue_item['artist'],
                                'title': queue_item['title'],
                                'album': queue_item['album'],
                                'moved': True
                            })
                        else:
                            # Verification failed - mark back to completed for retry
                            logger.warning(
                                f"[MOVE] Queue {queue_item['id']}: verification FAILED ({verify_result.get('error')}), "
                                f"marking back to 'completed' for retry"
                            )
                            update_queue_item(
                                queue_item['id'],
                                status='completed',
                                file_path=match_path  # Keep original path
                            )
                            completed_items.append({
                                'queue_id': queue_item['id'],
                                'filename': match_found,
                                'file_path': match_path,
                                'artist': queue_item['artist'],
                                'title': queue_item['title'],
                                'album': queue_item['album'],
                                'moved': False,
                                'verification_failed': True
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
                # If no file remains in /downloads, reconcile against /music in case a
                # separate mover/importer already transferred the completed download.
                existing_music_path = _find_existing_music_file(queue_item)
                if existing_music_path:
                    logger.info(
                        f"[RECONCILE] Queue {queue_item['id']} found in library: {existing_music_path}"
                    )
                    verify_result = verify_file_in_music(queue_item['id'], existing_music_path)
                    if verify_result.get('success'):
                        mark_queue_item_moved(queue_item['id'], existing_music_path)
                        update_queue_item(
                            queue_item['id'],
                            status='imported',
                            found_filename=os.path.basename(existing_music_path),
                            file_path=existing_music_path,
                            copied_individually=1,
                            copied_individually_at=datetime.now().isoformat()
                        )
                        completed_items.append({
                            'queue_id': queue_item['id'],
                            'filename': os.path.basename(existing_music_path),
                            'file_path': existing_music_path,
                            'artist': queue_item.get('artist'),
                            'title': queue_item.get('title'),
                            'album': queue_item.get('album'),
                            'moved': True,
                            'reconciled_from_library': True,
                        })
                    else:
                        logger.debug(
                            f"[RECONCILE] Queue {queue_item['id']} found candidate in /music but verification failed"
                        )
                else:
                    # Debug: show what we're looking for
                    search_query = queue_item.get('search_query', f"{queue_item.get('artist', '')} {queue_item.get('title', '')}")
                    logger.debug(f"No match found for queue item {queue_item['id']}: {search_query}")

        conn.close()

        if completed_items:
            logger.info(f"Found {len(completed_items)} completed downloads")

        _downloads_check_cache['timestamp'] = time.time()
        _downloads_check_cache['result'] = list(completed_items)
        return completed_items
        
    except Exception as e:
        logger.error(f"Error checking downloads folder: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return []
    finally:
        if lock_acquired:
            try:
                _downloads_check_lock.release()
            except Exception:
                pass


def is_match(filename, queue_item):
    """
    Conservative filename/path fallback when metadata is unavailable.
    
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
        search_query = (queue_item.get('query') or f"{artist} {title}".strip()).lower()
        
        # Require artist/title presence first; this is a fallback only.
        if not artist or not title:
            return False

        artist_in_path = artist in filename_test
        title_in_path = title in filename_test
        if artist_in_path and title_in_path:
            return True

        # Use stricter sequence similarity fallback to avoid cross-track collisions.
        combined_target = f"{artist} {title} {album}".strip()
        score = SequenceMatcher(None, combined_target, filename_test).ratio()

        if score >= 0.60 and (artist_in_path or title_in_path):
            return True

        # Last-resort search_query overlap with stronger threshold.
        if search_query:
            search_terms = [t for t in search_query.split() if len(t) > 2]
            if search_terms:
                term_matches = sum(1 for term in search_terms if term in filename_test)
                if term_matches / len(search_terms) >= 0.75:
                    return True

        return False
        
    except Exception as e:
        logger.error(f"Error matching filename {filename}: {e}")
        return False


def _strip_track_number_prefix(title):
    """
    Remove a leading track-number prefix from a title string.

    Handles common filename conventions such as:
        "05 - CINEMA"  →  "CINEMA"
        "05. CINEMA"   →  "CINEMA"
        "5 - CINEMA"   →  "CINEMA"
        "05 CINEMA"    →  unchanged  (no separator, avoid stripping real words)
    """
    # Match 1-3 leading digits followed by optional spaces + '-' or '.' + optional spaces
    cleaned = re.sub(r'^\d{1,3}\s*[-\.]\s*', '', title).strip()
    return cleaned if cleaned else title


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
        from app import _is_postgres_connection as app_is_postgres_connection
        is_pg = bool(app_is_postgres_connection(conn))
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) if is_pg else conn.cursor()

        def _row_get(row, key, index=None, default=None):
            if row is None:
                return default
            try:
                return row[key]
            except Exception:
                pass
            if hasattr(row, 'get'):
                try:
                    return row.get(key, default)
                except Exception:
                    pass
            if index is not None:
                try:
                    return row[index]
                except Exception:
                    pass
            return default

        def _rows_to_dicts(rows, description):
            if not rows:
                return []
            first = rows[0]
            if isinstance(first, dict):
                return rows
            col_names = [d[0] for d in description]
            return [dict(zip(col_names, r)) for r in rows]
        
        # Ensure required columns exist for auto-discovery inserts
        if is_pg:
            cursor.execute("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'download_queue' AND table_schema = 'public'
            """)
            columns = [
                (row.get('column_name') if hasattr(row, 'get') else row[0])
                for row in cursor.fetchall()
            ]
        else:
            cursor.execute("PRAGMA table_info(download_queue)")
            columns = [row[1] for row in cursor.fetchall()]
        
        required_cols = {
            'track_number': "TEXT",
            'disc_number': "TEXT",
            'album_artist': "TEXT",
            'year': "TEXT",
            'found_filename': "TEXT",
            'import_group': "TEXT",
            'import_type': "TEXT DEFAULT 'song'"
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
                # When no title tag is present, derive it from the filename stem and strip
                # any leading track-number prefix (e.g. "05 - CINEMA" → "CINEMA").
                title = metadata.get('title') or _strip_track_number_prefix(os.path.splitext(filename)[0])
                album_artist = metadata.get('album_artist') or artist
                track_number = metadata.get('track_number')
                disc_number = metadata.get('disc_number')
                year = metadata.get('date') or metadata.get('year')
                duration_ms = metadata.get('duration_ms')
                duration = int(duration_ms / 1000) if duration_ms and duration_ms > 0 else None
                release_group = _build_release_import_group(album_artist or artist, album)
                
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
                placeholder = "%s" if is_pg else "?"
                cursor.execute(f"""
                    SELECT id, status FROM download_queue 
                    WHERE file_path = {placeholder}
                       OR found_filename = {placeholder}
                       OR found_filename = {placeholder}
                       OR found_filename = {placeholder}
                """, (full_path, filename, file_info['rel_path'], full_path))
                
                existing = cursor.fetchone()
                if existing:
                    existing_id = _row_get(existing, 'id', 0, None)
                    existing_status = _row_get(existing, 'status', 1, None)
                    collection_track_id = None
                    cursor.execute(f"""
                        SELECT id FROM tracks
                        WHERE LOWER(artist) = LOWER({placeholder}) 
                        AND LOWER(album) = LOWER({placeholder}) 
                        AND LOWER(title) = LOWER({placeholder})
                    """, (artist, album, title))
                    in_library = cursor.fetchone()
                    if in_library:
                        stats['already_in_library'] += 1
                        collection_track_id = _row_get(in_library, 'id', 0, None)
                        execute_write_with_retry(
                            cursor,
                            conn,
                            f"""
                            UPDATE download_queue
                            SET status = 'in_collection',
                                in_collection = {placeholder},
                                collection_track_id = {placeholder},
                                collection_matched_at = CURRENT_TIMESTAMP,
                                found_filename = COALESCE(found_filename, {placeholder}),
                                file_path = COALESCE(file_path, {placeholder}),
                                updated_at = CURRENT_TIMESTAMP
                            WHERE id = {placeholder}
                        """,
                            (1, collection_track_id, filename, full_path, existing_id),
                            context="auto_discover existing queue in-library update"
                        )
                        logger.info(
                            f"[AUTO-DISCOVER] Cleared existing queue item {existing_id} as in_collection: "
                            f"{artist} - {title}"
                        )
                        continue

                    stats['already_in_queue'] += 1
                    logger.debug(f"File already in queue (ID {existing_id}, status {existing_status}): {filename}")
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
                    collection_track_id = _row_get(in_library, 'id', 0, None)
                    logger.debug(f"Track already in library: {artist} - {title}")

                    execute_write_with_retry(
                        cursor,
                        conn,
                        f"""
                        UPDATE download_queue
                        SET status = 'in_collection',
                            in_collection = {placeholder},
                            collection_track_id = {placeholder},
                            collection_matched_at = CURRENT_TIMESTAMP,
                            found_filename = COALESCE(found_filename, {placeholder}),
                            file_path = COALESCE(file_path, {placeholder}),
                            updated_at = CURRENT_TIMESTAMP
                        WHERE LOWER(artist) = LOWER({placeholder})
                          AND LOWER(album) = LOWER({placeholder})
                          AND LOWER(title) = LOWER({placeholder})
                          AND status NOT IN ('completed', 'removed', 'cancelled', 'deleted', 'in_collection')
                    """,
                        (1, collection_track_id, filename, full_path, artist, album, title),
                        context="auto_discover in-library update"
                    )

                    logger.info(
                        f"[AUTO-DISCOVER] Skipped queueing library track and cleared matching queue rows: "
                        f"{artist} - {title}"
                    )
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
                pending_rows = cursor.fetchall()
                pending_items = _rows_to_dicts(pending_rows, cursor.description)

                matched_pending = None
                for pending in pending_items:
                    if _metadata_matches_queue_item(file_meta, pending, file_path=full_path):
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
                                f"[AUTO-DISCOVER] Matched & copied: {artist} - {title} "
                                f"→ {move_result['target_path']}"
                            )
                        else:
                            logger.warning(
                                f"[AUTO-DISCOVER] Matched but could not move {filename}: "
                                f"{move_result.get('error')}"
                            )
                    stats['queued'] += 1
                    continue

                # Check whether this discovered file is a duplicate of an existing queue entry.
                cursor.execute(f"""
                    SELECT id
                    FROM download_queue
                    WHERE LOWER(artist) = LOWER({placeholder})
                      AND LOWER(title) = LOWER({placeholder})
                      AND LOWER(COALESCE(album, '')) = LOWER(COALESCE({placeholder}, ''))
                      AND status NOT IN ('removed', 'cancelled')
                      AND (file_path IS NULL OR file_path != {placeholder})
                    LIMIT 1
                """, (artist, title, album, full_path))
                duplicate_existing = cursor.fetchone()

                if duplicate_existing:
                    execute_write_with_retry(
                        cursor,
                        conn,
                        f"""
                        INSERT INTO download_queue 
                        (artist, title, album, album_artist, track_number, disc_number, year, duration, found_filename, file_path,
                         status, source, import_group, import_type, failure_reason, created_at, updated_at)
                        VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder},
                                'possible_duplicate', 'discovered', {placeholder}, 'album', 'Duplicate discovered during scan', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """,
                        (artist, title, album, album_artist, track_number, disc_number, year, duration, filename, full_path, release_group),
                        context="auto_discover duplicate insert"
                    )

                    stats['queued'] += 1
                    logger.info(f"⚠️  Duplicate [scan]: {artist} - {title} ({album})")
                    continue

                # Before creating a new entry, check if this file belongs to an existing album group
                # in the queue. If so, update the existing item instead of creating a duplicate.
                cursor.execute(f"""
                    SELECT id, status FROM download_queue
                    WHERE import_group = {placeholder}
                      AND status IN ('queued', 'searching', 'downloading', 'matched')
                      AND (file_path IS NULL OR file_path = '')
                    LIMIT 1
                """, (release_group,))
                existing_group = cursor.fetchone()
                
                if existing_group:
                    # File belongs to an existing album group - just update that item with the file path
                    existing_id = _row_get(existing_group, 'id', 0, None)
                    execute_write_with_retry(
                        cursor,
                        conn,
                        f"""
                        UPDATE download_queue 
                        SET file_path = {placeholder},
                            found_filename = {placeholder},
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = {placeholder}
                        """,
                        (full_path, filename, existing_id),
                        context="auto_discover match to existing album group"
                    )
                    stats['queued'] += 1
                    logger.info(f"✅ Matched to existing album group {existing_id}: {filename}")
                    continue

                # No pending queue item or album group matches → add as 'unmatched'
                execute_write_with_retry(
                    cursor,
                    conn,
                    f"""
                    INSERT INTO download_queue 
                    (artist, title, album, album_artist, track_number, disc_number, year, duration, found_filename, file_path, 
                     status, source, import_group, import_type, created_at, updated_at)
                    VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder}, 'unmatched', 'discovered', {placeholder}, 'album', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                    (artist, title, album, album_artist, track_number, disc_number, year, duration, filename, full_path, release_group),
                    context="auto_discover unmatched insert"
                )

                # Immediately attempt MusicBrainz enrichment for unmatched discoveries.
                # This can populate release MBID metadata and add sibling album tracks as 'queried'.
                try:
                    inserted_queue_id = None
                    cursor.execute(
                        f"""
                        SELECT id
                        FROM download_queue
                        WHERE file_path = {placeholder}
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        (full_path,)
                    )
                    inserted_row = cursor.fetchone()
                    if inserted_row:
                        inserted_queue_id = _row_get(inserted_row, 'id', 0, None)

                    if inserted_queue_id and album and album.strip() and album.strip().lower() != 'unknown':
                        from download_monitor_enhancements import search_and_update_musicbrainz
                        search_and_update_musicbrainz(inserted_queue_id, artist, title, album)
                except Exception as mb_err:
                    logger.debug(f"MusicBrainz auto-enrichment skipped for unmatched track: {mb_err}")

                stats['queued'] += 1

                # Log discovery with metadata and format info
                metadata_status = "✓ metadata" if metadata else "✗ fallback"
                if file_ext == '.flac':
                    logger.info(f"⚠️  Unmatched [FLAC] [{metadata_status}]: {artist} - {title}")
                else:
                    logger.info(f"⚠️  Unmatched [{metadata_status}]: {artist} - {title} from {os.path.basename(os.path.dirname(full_path))}/{filename}")
                
            except psycopg2.IntegrityError as e:
                # A duplicate row was inserted concurrently (race condition between parallel scans).
                # This is non-fatal: the file is already tracked in the queue.  Roll back, log at
                # INFO level and count as already-queued rather than as an error.
                try:
                    conn.rollback()
                except Exception:
                    pass
                logger.info(
                    f"Duplicate skipped during auto-discover for {file_info['filename']}: {e}"
                )
                stats['already_in_queue'] += 1
            except Exception as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
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
        from app import _is_postgres_connection as app_is_postgres_connection
        is_pg = bool(app_is_postgres_connection(conn))
        placeholder = "%s" if is_pg else "?"
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) if is_pg else conn.cursor()
        
        cursor.execute(f"""
            SELECT * FROM download_queue 
            WHERE status = 'queued' 
            AND (next_retry_at IS NULL OR next_retry_at <= CURRENT_TIMESTAMP)
            AND retry_count < max_retries
            ORDER BY priority ASC, next_retry_at ASC
            LIMIT {placeholder}
        """, (limit,))
        
        rows = cursor.fetchall()
        if rows and not isinstance(rows[0], dict):
            col_names = [d[0] for d in cursor.description]
            items = [dict(zip(col_names, r)) for r in rows]
        else:
            items = [dict(row) for row in rows]
        conn.close()
        
        return items
        
    except Exception as e:
        logger.error(f"Error getting retry queue: {e}")
        return []


def get_completed_queue(limit=50):
    """
    Get completed downloads (and unmatched files) waiting for organization.

    Includes items with status 'completed', 'unmatched', or 'possible_duplicate'.

    Returns:
        List of completed/unmatched queue items
    """
    try:
        conn = get_db()

        from app import _is_postgres_connection as app_is_postgres_connection
        is_pg = bool(app_is_postgres_connection(conn))
        placeholder = "%s" if is_pg else "?"
        cursor = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) if is_pg else conn.cursor()

        cursor.execute(f"""
            SELECT * FROM download_queue 
            WHERE status IN ('completed', 'unmatched', 'possible_duplicate')
            ORDER BY updated_at DESC
            LIMIT {placeholder}
        """, (limit,))

        rows = cursor.fetchall()
        # Convert rows to dicts
        if is_pg:
            # RealDictCursor returns dict-like objects, convert to plain dicts
            items = [{k: v for k, v in row.items()} for row in rows] if rows else []
        else:
            # SQLite cursor returns tuples, need to zip with column names
            col_names = [d[0] for d in cursor.description]
            items = [dict(zip(col_names, r)) for r in rows] if rows else []
        conn.close()

        return items

    except Exception as e:
        logger.error(f"Error getting completed queue: {e}")
        return []


def cleanup_missing_files():
    """
    Soft-clean queue items where the source file no longer exists in /downloads.
    
        This cleanup function:
        - Checks queue items with file_path set
        - Never touches active/moving lifecycle states
            (queued/searching/downloading/completed/imported)
        - For safe stale states, marks item as 'unmatched' and clears file_path
            instead of deleting rows, so items remain visible/recoverable
    
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

            # Only stale/non-active states are eligible for cleanup.
            cleanup_allowed_statuses = {
                'discovered',
                'pending_match',
                'unmatched',
                'failed',
                'cancelled',
                'removed',
            }
            protected_statuses = {
                'queued',
                'searching',
                'downloading',
                'completed',
                'imported',
            }

            unmatched_ids = []

            for item in items:
                queue_id = item['id']
                file_path = item['file_path']
                status = item['status']
                if status in protected_statuses:
                    continue

                if status not in cleanup_allowed_statuses:
                    continue

                # Check if file exists
                if not os.path.exists(file_path):
                    logger.info(
                        f"Queue item {queue_id} source missing (status: {status}): {file_path} | marking unmatched"
                    )
                    unmatched_ids.append(queue_id)

            # Soft-update stale missing-source items in batch (do not delete).
            if unmatched_ids:
                placeholder = "%s" if is_pg else "?"
                placeholders = ','.join([placeholder] * len(unmatched_ids))

                cursor.execute(
                    f"""
                    UPDATE download_queue
                    SET status = 'unmatched',
                        failure_reason = COALESCE(failure_reason, 'Source file missing before organization'),
                        file_path = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id IN ({placeholders})
                    """,
                    unmatched_ids,
                )
                conn.commit()
                stats['removed'] = len(unmatched_ids)
                logger.info(
                    f"Cleanup marked {len(unmatched_ids)} queue items as unmatched due to missing source files"
                )

            return stats

        except Exception as e:
            # PostgreSQL doesn't have 'database is locked' - handle connection errors instead
            if 'deadlock' in str(e).lower() or 'concurrent' in str(e).lower():
                if attempt < max_retries - 1:
                    delay = initial_delay * (2 ** attempt)
                    logger.warning(
                        f"Database conflict during cleanup_missing_files(), retrying in {delay:.2f}s "
                        f"(attempt {attempt + 1}/{max_retries})"
                    )
                    time.sleep(delay)
                    continue
                error_msg = f"Error during cleanup: database conflict after {max_retries} retries"
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
        
        # Get all downloads from slskd (flat list with correct nested parsing)
        downloads = client.get_active_downloads()
        stats["total_active"] = len(downloads)
        
        logger.info(f"Checking {len(downloads)} download entries for failures")
        
        conn = get_db()
        cursor = conn.cursor()
        placeholder = "%s" if isinstance(conn, psycopg2.extensions.connection) else "?"
        
        for download in downloads:
            try:
                transfer_id = download.get("id", "")
                username = download.get("username", "")
                filename = download.get("filename", "")
                state = download.get("state", "")
                bytes_transferred = download.get("bytesTransferred", 0)
                size = download.get("size", 0)
                progress = download.get("progress", 0)
                
                # Use exact slskd terminal-failure states
                is_failed_state = state in client.FAILED_STATES
                # Also catch zero-progress transfers stuck in a non-active state
                is_stalled = (
                    bytes_transferred == 0
                    and state not in client.ACTIVE_STATES
                    and state != client.STATE_SUCCEEDED
                    and state != ""
                )
                
                if not (is_failed_state or is_stalled):
                    continue

                logger.warning(
                    f"Failed download detected: {filename!r} from {username} "
                    f"(state={state!r}, bytes={bytes_transferred}/{size})"
                )
                
                # Cancel and remove the transfer in slskd using the correct endpoint
                if transfer_id and username:
                    client.cancel_download(username, transfer_id, remove=True)
                else:
                    logger.debug(f"No transfer_id/username — cannot cancel slskd entry for {filename!r}")
                
                # Find matching queue item with strict-first lookup to avoid basename collisions.
                basename = filename.rsplit('/', 1)[-1].rsplit('\\', 1)[-1]
                normalized_filename = filename.replace('\\', '/').strip()

                cursor.execute(
                    f"""
                    SELECT id FROM download_queue
                    WHERE source = 'soulseek'
                      AND status IN ('downloading', 'searching')
                      AND (
                            file_path = {placeholder}
                         OR found_filename = {placeholder}
                         OR found_filename = {placeholder}
                      )
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    (normalized_filename, normalized_filename, basename),
                )

                queue_item = cursor.fetchone()

                # Fallback: parse common "Artist - Title.ext" patterns and match artist/title exactly.
                if not queue_item:
                    stem = os.path.splitext(basename)[0]
                    parts = [p.strip() for p in stem.split(' - ', 1)]
                    if len(parts) == 2 and parts[0] and parts[1]:
                        cursor.execute(
                            f"""
                            SELECT id FROM download_queue
                            WHERE source = 'soulseek'
                              AND status IN ('downloading', 'searching')
                              AND LOWER(artist) = LOWER({placeholder})
                              AND LOWER(title) = LOWER({placeholder})
                            ORDER BY updated_at DESC
                            LIMIT 1
                            """,
                            (parts[0], parts[1]),
                        )
                        queue_item = cursor.fetchone()
                if queue_item:
                    queue_id = queue_item['id'] if hasattr(queue_item, 'keys') else queue_item[0]
                    logger.info(f"Marking queue item {queue_id} for retry (failed download: {state!r})")
                    mark_as_failed(
                        queue_id,
                        f"Download failed: {state}",
                        retry_delay_minutes=5,
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
        is_pg = isinstance(conn, psycopg2.extensions.connection)
        placeholder = "%s" if is_pg else "?"
        
        cutoff_date = (datetime.now() - timedelta(days=days)).isoformat()
        
        execute_write_with_retry(
            cursor,
            conn,
            f"""
            DELETE FROM download_queue 
            WHERE status = 'imported' 
            AND imported_at < {placeholder}
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
    # Strip a leading track-number prefix (e.g. "05 - cinema" -> "cinema",
    # "05. cinema" -> "cinema") so filenames with embedded track numbers can
    # still be matched against plain track titles.
    normalized = _strip_track_number_prefix(normalized)
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
        "User-Agent": MUSICBRAINZ_USER_AGENT
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
    cursor.execute("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = 'download_queue' AND table_schema = 'public'
    """)
    columns = [row['column_name'] if hasattr(row, 'keys') else row[0] for row in cursor.fetchall()]

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
            "User-Agent": MUSICBRAINZ_USER_AGENT
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
                    from app import _is_postgres_connection as app_is_postgres_connection
                    is_pg = bool(app_is_postgres_connection(conn))
                    placeholder = "%s" if is_pg else "?"
                    _ensure_matching_columns(cursor)
                    candidates_json = json.dumps(scored_candidates[:5]) if scored_candidates else "[]"
                    best_score = scored_candidates[0].get('confidence') if scored_candidates else 0

                    for track in completion['tracks']:
                        cursor.execute(
                            f"""
                            UPDATE download_queue
                            SET status = 'pending_match',
                                mb_match_status = 'needs_review',
                                mb_match_score = {placeholder},
                                mb_match_candidates = {placeholder},
                                mb_release_group_id = NULL,
                                mb_matched_title = NULL,
                                mb_matched_artist = NULL,
                                mb_matched_year = NULL,
                                mb_last_match_at = CURRENT_TIMESTAMP,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE id = {placeholder}
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
        dest_year = _most_common(years) or ''
        
        # Clean up year (extract just the year if it's a full date)
        if dest_year and len(str(dest_year)) >= 4:
            dest_year = str(dest_year)[:4]
        elif not dest_year:
            dest_year = 'Unknown'

        dest_dir = os.path.join(music_root, dest_album_artist, f"{dest_year} - {dest_album}")

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
            
            # Build proper filename: [track_number]. [artist] - [title].[ext]
            track_artist = track.get('artist', 'Unknown Artist')
            track_title = track.get('title', 'Unknown Title')
            track_num = track.get('track_number', '00')
            disc_num = track.get('disc_number', 1)
            
            # Format track number with disc prefix if needed
            try:
                track_num = int(str(track_num).split('/')[0]) if track_num else 0
                disc_num = int(str(disc_num).split('/')[0]) if disc_num else 1
                
                if disc_num > 1:
                    track_num = f"{disc_num}{track_num:02d}"
                else:
                    track_num = f"{track_num:02d}"
            except:
                track_num = "00"
            
            # Get file extension
            ext = os.path.splitext(src)[1].lower()
            
            # Build filename with proper format
            filename = _sanitize_path_component(f"{track_num}. {track_artist} - {track_title}{ext}")
            dest = os.path.join(dest_dir, filename)

            # Avoid overwriting
            if os.path.exists(dest):
                base, ext_only = os.path.splitext(filename)
                counter = 1
                while os.path.exists(os.path.join(dest_dir, f"{base}_{counter}{ext_only}")):
                    counter += 1
                dest = os.path.join(dest_dir, f"{base}_{counter}{ext_only}")

            # Update embedded file tags before moving so the library reflects
            # the album context rather than the original single/release tags.
            try:
                from post_download_processor import update_file_metadata
                tag_metadata = {
                    'title': track_title,
                    'artist': track_artist,
                    'album_artist': dest_album_artist,
                    'album': dest_album,
                    'year': dest_year,
                    'track_number': track.get('track_number'),
                    'disc_number': track.get('disc_number'),
                }
                update_file_metadata(src, tag_metadata)
            except Exception as tag_err:
                logger.warning(
                    f"[AUTO_MOVE] Could not update file tags for queue {track['id']} "
                    f"(non-fatal): {tag_err}"
                )

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
