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
from datetime import datetime, timedelta
from pathlib import Path
from helpers.metadata_reader import read_mp3_metadata

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
DOWNLOADS_DIR = os.environ.get("DOWNLOADS_DIR", "/downloads")
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
    return conn


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
        conn = get_db()
        cursor = conn.cursor()
        
        # Validate inputs
        if not artist or not title:
            logger.error("Artist and title are required")
            conn.close()
            return None
        
        # Ensure all required columns exist
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
        
        search_query = f"{artist} - {title}"
        if album:
            search_query = f"{artist} {album} {title}"
        
        try:
            cursor.execute("""
                INSERT INTO download_queue 
                (artist, title, album, search_query, source, status, priority, file_path, import_group, import_type, 
                 track_number, album_artist, year, release_id, release_source, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'queued', ?, NULL, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """, (artist, title, album, search_query, source, priority, import_group, import_type,
                  track_number, album_artist, year, release_id, release_source))
            
            conn.commit()
            queue_id = cursor.lastrowid
            
            logger.info(f"Added to queue: {search_query} (ID: {queue_id}, source: {source})")
            
            # Return the item
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


def check_downloads_folder():
    """
    Monitor /downloads folder for completed files.
    Match files to queue items and update their status.
    
    Returns:
        List of newly completed items
    """
    try:
        if not os.path.isdir(DOWNLOADS_DIR):
            logger.warning(f"Downloads folder not found: {DOWNLOADS_DIR}")
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
        if os.path.isdir(DOWNLOADS_DIR):
            try:
                for root, dirs, files in os.walk(DOWNLOADS_DIR):
                    for f in files:
                        if f.endswith(('.mp3', '.flac', '.m4a', '.ogg', '.wav')):
                            # Store both filename and full path
                            downloads_files.append({
                                'filename': f,
                                'full_path': os.path.join(root, f),
                                'rel_path': os.path.relpath(os.path.join(root, f), DOWNLOADS_DIR)
                            })
            except Exception as e:
                logger.error(f"Error scanning downloads folder: {e}")
        
        logger.info(f"Found {len(downloads_files)} audio files in {DOWNLOADS_DIR}, checking {len(queue_items)} queue items")
        
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
        if not os.path.isdir(DOWNLOADS_DIR):
            logger.warning(f"Downloads folder not found: {DOWNLOADS_DIR}")
            return stats
        
        # Clean up queue items for files that no longer exist
        cleanup_stats = cleanup_missing_files()
        if cleanup_stats['removed'] > 0:
            logger.info(f"Cleanup: Removed {cleanup_stats['removed']} queue items with missing files")
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
                logger.info(f"Adding missing column '{col}' to download_queue")
                try:
                    cursor.execute(f"ALTER TABLE download_queue ADD COLUMN {col} {col_type};")
                    conn.commit()
                except Exception as e:
                    logger.warning(f"Could not add {col} column: {e}")
        
        # Get all audio files from downloads folder and subdirectories
        audio_extensions = {'.mp3', '.flac', '.m4a', '.ogg', '.wav'}
        discovered_files = []
        
        for root, dirs, files in os.walk(DOWNLOADS_DIR):
            for filename in files:
                file_ext = os.path.splitext(filename)[1].lower()
                if file_ext in audio_extensions:
                    full_path = os.path.join(root, filename)
                    rel_path = os.path.relpath(full_path, DOWNLOADS_DIR)
                    discovered_files.append({
                        'filename': filename,
                        'full_path': full_path,
                        'rel_path': rel_path
                    })
        
        stats['scanned'] = len(discovered_files)
        logger.info(f"Scanning {len(discovered_files)} audio files in {DOWNLOADS_DIR}")
        
        for file_info in discovered_files:
            try:
                full_path = file_info['full_path']
                filename = file_info['filename']
                file_ext = os.path.splitext(filename)[1].lower()
                
                # Extract metadata from file
                try:
                    metadata = read_mp3_metadata(full_path)
                except Exception as e:
                    logger.warning(f"Could not read metadata from {filename}: {e}")
                    metadata = {}
                
                artist = metadata.get('artist', 'Unknown Artist')
                album = metadata.get('album', 'Unknown Album')
                title = metadata.get('title', os.path.splitext(filename)[0])
                album_artist = metadata.get('album_artist') or artist
                track_number = metadata.get('track_number')
                disc_number = metadata.get('disc_number')
                year = metadata.get('date') or metadata.get('year')
                
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
                    
                    # Still add to queue with status 'discovered' so user can see it
                    cursor.execute("""
                        INSERT INTO download_queue 
                        (artist, title, album, album_artist, track_number, disc_number, year, found_filename, file_path, 
                         status, source, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'discovered', 'discovered', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """, (artist, title, album, album_artist, track_number, disc_number, year, filename, full_path))
                    conn.commit()
                    stats['queued'] += 1
                    continue
                
                # Add to queue with 'discovered' status
                cursor.execute("""
                    INSERT INTO download_queue 
                    (artist, title, album, album_artist, track_number, disc_number, year, found_filename, file_path, 
                     status, source, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'discovered', 'discovered', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """, (artist, title, album, album_artist, track_number, disc_number, year, filename, full_path))
                
                conn.commit()
                stats['queued'] += 1
                
                # Log discovery with format info
                if file_ext == '.flac':
                    logger.info(f"Discovered and queued (FLAC→MP3): {artist} - {title} ({filename})")
                else:
                    logger.info(f"Discovered and queued: {artist} - {title} ({filename})")
                
            except Exception as e:
                error_msg = f"Error processing {file_info['filename']}: {str(e)}"
                logger.error(error_msg)
                stats['errors'].append(error_msg)
        
        conn.close()
        
        logger.info(f"Auto-discovery complete: {stats['queued']} files added to queue, "
                   f"{stats['already_in_queue']} already queued, "
                   f"{stats['already_in_library']} in library")
        
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
            conn.close()
            return stats
        
        removed_ids = []
        
        for item in items:
            queue_id = item['id']
            file_path = item['file_path']
            artist = item['artist']
            title = item['title']
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
        
        conn.close()
        return stats
        
    except Exception as e:
        error_msg = f"Error during cleanup: {str(e)}"
        logger.error(error_msg)
        stats['errors'].append(error_msg)
        return stats


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
        
        cursor.execute("""
            DELETE FROM download_queue 
            WHERE status = 'imported' 
            AND imported_at < ?
        """, (cutoff_date,))
        
        removed = cursor.rowcount
        conn.commit()
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
            AND status IN ('discovered', 'completed')
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


def process_complete_albums():
    """
    Check for complete discovered albums and either:
    1. Auto-process them to /music if they don't exist in library
    2. Mark as 'possible_duplicate' if they already exist
    
    This function should be called after auto_discover_and_queue_files()
    or periodically to check for complete albums.
    
    Returns:
        dict: Statistics about processed albums
    """
    stats = {
        'checked': 0,
        'processed': 0,
        'duplicates_found': 0,
        'errors': []
    }
    
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Get all unique album/artist combinations from discovered tracks
        cursor.execute("""
            SELECT DISTINCT album, artist 
            FROM download_queue 
            WHERE status = 'discovered' 
            AND album IS NOT NULL 
            AND artist IS NOT NULL
            AND file_path IS NOT NULL
        """)
        
        albums = [dict(row) for row in cursor.fetchall()]
        conn.close()
        
        logger.info(f"Checking {len(albums)} discovered albums for completeness")
        
        for album_info in albums:
            try:
                album = album_info['album']
                artist = album_info['artist']
                
                stats['checked'] += 1
                
                # Check if album is complete
                completion = check_album_complete(album, artist)
                
                if not completion['is_complete']:
                    logger.debug(f"Album not complete: {artist} - {album} ({completion['discovered_tracks']} tracks)")
                    continue
                
                logger.info(f"Complete album found: {artist} - {album} ({completion['total_tracks']} tracks)")
                
                # Check if album already exists in library
                exists_in_library = check_album_exists_in_library(album, artist)
                
                if exists_in_library:
                    # Mark all tracks as possible_duplicate
                    conn = get_db()
                    cursor = conn.cursor()
                    
                    for track in completion['tracks']:
                        cursor.execute("""
                            UPDATE download_queue 
                            SET status = 'possible_duplicate',
                                updated_at = CURRENT_TIMESTAMP
                            WHERE id = ?
                        """, (track['id'],))
                    
                    conn.commit()
                    conn.close()
                    
                    stats['duplicates_found'] += 1
                    logger.warning(f"Album already exists in library (marked as possible_duplicate): {artist} - {album}")
                    
                else:
                    # Auto-process tracks to /music
                    logger.info(f"Auto-processing album to /music: {artist} - {album}")
                    
                    try:
                        from post_download_processor import update_file_metadata, rename_and_move_file
                        
                        conn = get_db()
                        cursor = conn.cursor()
                        
                        # Determine consistent album_artist for all tracks
                        # Use the most common album_artist value, or fallback to artist
                        album_artists = [t.get('album_artist') or t.get('artist') for t in completion['tracks']]
                        album_artist_counts = {}
                        for aa in album_artists:
                            if aa:
                                album_artist_counts[aa] = album_artist_counts.get(aa, 0) + 1
                        
                        # Get the most common album_artist
                        consistent_album_artist = max(album_artist_counts.items(), key=lambda x: x[1])[0] if album_artist_counts else artist
                        logger.info(f"Using consistent album_artist for all tracks: {consistent_album_artist}")
                        
                        success_count = 0
                        for track in completion['tracks']:
                            try:
                                file_path = track['file_path']
                                
                                if not os.path.exists(file_path):
                                    logger.warning(f"File not found: {file_path}")
                                    continue
                                
                                # Prepare metadata with consistent album_artist
                                metadata = {
                                    'track_number': track.get('track_number'),
                                    'disc_number': track.get('disc_number'),
                                    'artist': track.get('artist'),
                                    'album_artist': consistent_album_artist,
                                    'album': track.get('album'),
                                    'year': track.get('year'),
                                    'title': track.get('title')
                                }
                                
                                # Update file metadata tags
                                update_file_metadata(file_path, metadata)
                                
                                # Rename and move file
                                result = rename_and_move_file(file_path, metadata)
                                
                                if result.get('success'):
                                    # Mark as completed
                                    cursor.execute("""
                                        UPDATE download_queue 
                                        SET status = 'imported',
                                            imported_at = CURRENT_TIMESTAMP,
                                            updated_at = CURRENT_TIMESTAMP
                                        WHERE id = ?
                                    """, (track['id'],))
                                    
                                    success_count += 1
                                    logger.info(f"✓ Processed: {track['artist']} - {track['title']}")
                                else:
                                    logger.error(f"Failed to process {track['title']}: {result.get('error')}")
                                
                            except Exception as track_error:
                                logger.error(f"Error processing track {track['title']}: {track_error}")
                        
                        conn.commit()
                        conn.close()
                        
                        if success_count == len(completion['tracks']):
                            stats['processed'] += 1
                            logger.info(f"✓ Successfully processed complete album: {artist} - {album} ({success_count} tracks)")
                        else:
                            logger.warning(f"Partially processed album: {artist} - {album} ({success_count}/{len(completion['tracks'])} tracks)")
                        
                    except Exception as process_error:
                        error_msg = f"Error auto-processing album {artist} - {album}: {str(process_error)}"
                        logger.error(error_msg)
                        stats['errors'].append(error_msg)
                
            except Exception as album_error:
                error_msg = f"Error checking album {album_info.get('artist')} - {album_info.get('album')}: {str(album_error)}"
                logger.error(error_msg)
                stats['errors'].append(error_msg)
        
        logger.info(f"Album processing complete: {stats['processed']} albums auto-processed, "
                   f"{stats['duplicates_found']} marked as duplicates")
        
        # Trigger Navidrome library scan if albums were processed
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
