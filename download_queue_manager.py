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
        
        search_query = f"{artist} - {title}"
        if album:
            search_query = f"{artist} {album} {title}"
        
        try:
            cursor.execute("""
                INSERT INTO download_queue 
                (artist, title, album, search_query, source, status, priority, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'queued', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """, (artist, title, album, search_query, source, priority))
            
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
        
        # First ensure source column exists
        cursor.execute("PRAGMA table_info(download_queue);")
        columns = [row[1] for row in cursor.fetchall()]
        
        if 'source' not in columns:
            logger.warning("'source' column missing from download_queue, attempting to add it")
            try:
                cursor.execute("ALTER TABLE download_queue ADD COLUMN source TEXT DEFAULT 'soulseek';")
                conn.commit()
            except Exception as e:
                logger.warning(f"Could not add source column: {e}")
        
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
        
        query += " ORDER BY priority ASC, created_at DESC LIMIT ?"
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
            return None
        
        updates.append("updated_at = CURRENT_TIMESTAMP")
        params.append(queue_id)
        
        query = f"UPDATE download_queue SET {', '.join(updates)} WHERE id = ?"
        cursor.execute(query, params)
        conn.commit()
        
        # Return updated item
        cursor.execute("SELECT * FROM download_queue WHERE id = ?", (queue_id,))
        item = cursor.fetchone()
        conn.close()
        
        logger.info(f"Updated queue item {queue_id}: {kwargs}")
        return dict(item) if item else None
        
    except Exception as e:
        logger.error(f"Error updating queue item: {e}")
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
        
        # Get list of files in downloads folder
        downloads_files = []
        if os.path.isdir(DOWNLOADS_DIR):
            try:
                downloads_files = [f for f in os.listdir(DOWNLOADS_DIR) 
                                 if f.endswith(('.mp3', '.flac', '.m4a'))]
            except Exception as e:
                logger.error(f"Error scanning downloads folder: {e}")
        
        logger.info(f"Found {len(downloads_files)} files in {DOWNLOADS_DIR}, checking {len(queue_items)} queue items")
        
        # Try to match files to queue items
        for queue_item in queue_items:
            match_found = None
            
            # Try exact filename match first
            if queue_item['found_filename'] and queue_item['found_filename'] in downloads_files:
                match_found = queue_item['found_filename']
            else:
                # Try fuzzy matching based on artist/title
                for filename in downloads_files:
                    if is_match(filename, queue_item):
                        match_found = filename
                        break
            
            if match_found:
                file_path = os.path.join(DOWNLOADS_DIR, match_found)
                logger.info(f"Matched queue {queue_item['id']} to file: {match_found}")
                
                # Update queue item
                update_queue_item(
                    queue_item['id'],
                    status='completed',
                    found_filename=match_found,
                    file_path=file_path,
                    imported_at=datetime.now().isoformat()
                )
                
                completed_items.append({
                    'queue_id': queue_item['id'],
                    'filename': match_found,
                    'file_path': file_path,
                    'artist': queue_item['artist'],
                    'title': queue_item['title'],
                    'album': queue_item['album']
                })
        
        conn.close()
        
        if completed_items:
            logger.info(f"Found {len(completed_items)} completed downloads")
        
        return completed_items
        
    except Exception as e:
        logger.error(f"Error checking downloads folder: {e}")
        return []


def is_match(filename, queue_item):
    """
    Check if a filename matches a queue item
    Uses fuzzy matching on artist, album, and title
    
    Args:
        filename: Filename to check
        queue_item: Queue item dict
    
    Returns:
        bool - True if filename likely matches queue item
    """
    try:
        filename_lower = filename.lower()
        artist = (queue_item['artist'] or '').lower()
        title = (queue_item['title'] or '').lower()
        album = (queue_item['album'] or '').lower()
        
        # Count how many search terms are in the filename
        matches = 0
        total_terms = 0
        
        if artist:
            total_terms += 1
            if artist in filename_lower:
                matches += 1
        
        if title:
            total_terms += 1
            if title in filename_lower:
                matches += 1
        
        if album:
            total_terms += 1
            if album.replace(' ', '') in filename_lower.replace(' ', ''):
                matches += 1
        
        # Require at least 50% of terms to match
        if total_terms > 0 and matches / total_terms >= 0.5:
            return True
        
        # Also try matching against search_query if set
        if queue_item['search_query']:
            search_terms = queue_item['search_query'].lower().split()
            term_matches = sum(1 for term in search_terms if term in filename_lower)
            if term_matches >= 2:  # At least 2 terms from search query
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
