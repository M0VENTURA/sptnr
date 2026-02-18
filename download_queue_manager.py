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
from datetime import datetime, timedelta
from pathlib import Path
from metadata_reader import read_mp3_metadata

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


def get_db():
    """Get database connection"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def add_to_queue(artist, title, album=None, source='soulseek', priority=5):
    """
    Add a song to the download queue
    
    Args:
        artist: Artist name
        title: Song title
        album: Album name (optional)
        source: 'soulseek' or 'qbittorrent'
        priority: Priority level (1-10, lower = higher priority)
    
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
            'priority': "INTEGER DEFAULT 5"
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
                (artist, title, album, search_query, source, status, priority, file_path, filename, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'queued', ?, NULL, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """, (artist, title, album, search_query, source, priority, search_query))
            
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
            'search_query': "TEXT"
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
    Update a queue item
    
    Args:
        queue_id: Queue item ID
        **kwargs: Fields to update (status, found_filename, file_path, failure_reason, etc.)
    
    Returns:
        Updated item dict or None
    """
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Build update query
        updates = []
        params = []
        
        for key, value in kwargs.items():
            if key in ['status', 'source_id', 'found_filename', 'file_path', 'failure_reason', 
                       'retry_count', 'last_failure_time', 'imported_at', 'metadata']:
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
        
    except sqlite3.IntegrityError as e:
        logger.error(f"Database integrity error updating queue item {queue_id}: {e}")
        return None
    except Exception as e:
        logger.error(f"Error updating queue item {queue_id}: {type(e).__name__}: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return None


def mark_as_failed(queue_id, reason, retry_delay_minutes=30):
    """
    Mark queue item as failed and schedule retry
    
    Args:
        queue_id: Queue item ID
        reason: Failure reason
        retry_delay_minutes: Minutes until next retry
    
    Returns:
        Updated item or None
    """
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Get current retry count
        cursor.execute("SELECT retry_count, max_retries FROM download_queue WHERE id = ?", (queue_id,))
        row = cursor.fetchone()
        
        if not row:
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
        
    except Exception as e:
        logger.error(f"Error marking queue item as failed: {e}")
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
