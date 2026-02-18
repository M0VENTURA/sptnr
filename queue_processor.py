#!/usr/bin/env python3
"""
Download Queue Processor
Background worker that processes items in the download queue.
- Searches Soulseek for queued items
- Auto-downloads matching results
- Retries failed items with backoff
- Updates queue status and tracks file completion
"""

import os
import sys
import time
import sqlite3
import logging
import traceback
from datetime import datetime, timedelta
from pathlib import Path

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [Queue Processor] %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("/config/queue_processor.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "/database/sptnr.db")
DOWNLOADS_DIR = os.environ.get("DOWNLOADS_DIR", "/downloads")

def get_db():
    """Get database connection"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def get_slskd_client():
    """Get configured SlskdClient instance"""
    try:
        import yaml
        
        # Try both .yml and .yaml extensions
        config_path = "/config/config.yml"
        if not os.path.exists(config_path):
            config_path = "/config/config.yaml"
        
        if not os.path.exists(config_path):
            logger.error(f"Config file not found (tried config.yml and config.yaml)")
            return None
        
        with open(config_path, 'r') as f:
            config = yaml.safe_load(f) or {}
        
        slskd_config = config.get("slskd", {})
        
        if not slskd_config.get("enabled"):
            logger.warning("Soulseek (slskd) is not enabled in config")
            return None
        
        from api_clients.slskd import SlskdClient
        
        web_url = slskd_config.get("web_url", "http://localhost:5030")
        api_key = slskd_config.get("api_key", "")
        
        return SlskdClient(web_url, api_key, enabled=True)
        
    except Exception as e:
        logger.error(f"Error getting SlskdClient: {e}")
        return None

def get_queued_items(limit=10):
    """Get items ready to process (queued or scheduled for retry)"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        now = datetime.now().isoformat()
        
        # Get queued items and items scheduled for retry
        cursor.execute("""
            SELECT * FROM download_queue 
            WHERE status = 'queued'
            AND (next_retry_at IS NULL OR next_retry_at <= ?)
            AND source = 'soulseek'
            ORDER BY priority ASC, retry_count ASC, next_retry_at ASC, created_at ASC
            LIMIT ?
        """, (now, limit))
        
        items = [dict(row) for row in cursor.fetchall()]
        conn.close()
        
        return items
        
    except Exception as e:
        logger.error(f"Error getting queued items: {e}")
        return []

def update_queue_status(queue_id, status, **kwargs):
    """Update queue item status"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        updates = ["status = ?"]
        params = [status]
        
        # Add any additional fields to update
        for key, value in kwargs.items():
            if key in ['found_filename', 'file_path', 'failure_reason', 'retry_count', 
                       'last_failure_time', 'source_id']:
                updates.append(f"{key} = ?")
                params.append(value)
        
        updates.append("updated_at = CURRENT_TIMESTAMP")
        params.append(queue_id)
        
        query = f"UPDATE download_queue SET {', '.join(updates)} WHERE id = ?"
        cursor.execute(query, params)
        conn.commit()
        conn.close()
        
        logger.info(f"Updated queue {queue_id} to status: {status}")
        return True
        
    except Exception as e:
        logger.error(f"Error updating queue status: {e}")
        return False

def increment_retry_count(queue_id, retry_delay_minutes=30):
    """Increment retry count and schedule next retry"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Get current retry count
        cursor.execute("""
            SELECT retry_count FROM download_queue WHERE id = ?
        """, (queue_id,))
        
        row = cursor.fetchone()
        if not row:
            conn.close()
            return False
        
        retry_count = (row['retry_count'] or 0) + 1
        
        next_retry = datetime.now() + timedelta(minutes=retry_delay_minutes)
        
        cursor.execute("""
            UPDATE download_queue 
            SET retry_count = ?, next_retry_at = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (retry_count, next_retry.isoformat(), queue_id))
        
        conn.commit()
        conn.close()
        
        logger.info(f"Queue {queue_id}: retry count now {retry_count}, next retry at {next_retry}")
        return True
        
    except Exception as e:
        logger.error(f"Error incrementing retry count: {e}")
        return False

def mark_failed(queue_id, reason, schedule_retry=True, retry_delay_minutes=30):
    """Mark queue item as failed, optionally scheduling retry"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Get current retry count
        cursor.execute("SELECT retry_count FROM download_queue WHERE id = ?", (queue_id,))
        row = cursor.fetchone()
        
        if not row:
            conn.close()
            return False
        
        retry_count = (row['retry_count'] or 0) + 1
        
        # Always schedule retry if requested - no max retry limit for Soulseek searches
        if schedule_retry:
            next_retry = datetime.now() + timedelta(minutes=retry_delay_minutes)
            new_status = 'queued'
            logger.warning(f"Queue {queue_id}: Failed ({reason}), scheduling retry #{retry_count} at {next_retry}")
        else:
            next_retry = None
            new_status = 'failed'
            logger.error(f"Queue {queue_id}: Failed permanently ({reason}) - retry not requested")
        
        cursor.execute("""
            UPDATE download_queue 
            SET status = ?, retry_count = ?, failure_reason = ?, last_failure_time = CURRENT_TIMESTAMP,
                next_retry_at = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (new_status, retry_count, reason, next_retry.isoformat() if next_retry else None, queue_id))
        
        conn.commit()
        conn.close()
        
        return schedule_retry  # Return whether retry was scheduled
        
    except Exception as e:
        logger.error(f"Error marking queue item as failed: {e}")
        return False

def search_and_download(queue_id, queue_item, client):
    """Search Soulseek for queue item and download top result"""
    try:
        search_query = queue_item['search_query']
        
        logger.info(f"Queue {queue_id}: Searching for '{search_query}'...")
        update_queue_status(queue_id, 'searching')
        
        # Start search
        search_id = client.start_search(search_query)
        if not search_id:
            logger.warning(f"Queue {queue_id}: Failed to start search")
            mark_failed(queue_id, "Failed to start Soulseek search", schedule_retry=True)
            return False
        
        # Poll for results (up to 15 seconds with 1 second intervals)
        best_result = None
        for poll_attempt in range(15):
            time.sleep(1)
            
            try:
                responses, state, is_complete = client.get_search_results(search_id)
                
                logger.debug(f"Queue {queue_id}: Poll {poll_attempt+1}/15 - Got {len(responses)} responses, state={state}")
                
                if responses:
                    # Find best result (first file from first user)
                    for resp_idx, resp in enumerate(responses):
                        if hasattr(resp, 'files') and resp.files:
                            logger.debug(f"Queue {queue_id}: Response {resp_idx} from {resp.username} has {len(resp.files)} files")
                            best_result = {
                                "username": resp.username,
                                "filename": resp.files[0].filename,
                                "size": getattr(resp.files[0], 'size', 0)
                            }
                            logger.info(f"Queue {queue_id}: ✓ Found result after {poll_attempt+1}s from {resp.username}")
                            logger.info(f"Queue {queue_id}: File: {best_result['filename'][:80]}... ({best_result['size']} bytes)")
                            break
                        else:
                            logger.debug(f"Queue {queue_id}: Response {resp_idx} from {getattr(resp, 'username', 'unknown')} has no files")
                    
                    if best_result:
                        break
            except Exception as e:
                logger.warning(f"Queue {queue_id}: Error polling results (attempt {poll_attempt+1}): {e}")
                logger.debug(traceback.format_exc())
        
        if not best_result:
            logger.warning(f"Queue {queue_id}: ✗ No results found after 15 seconds of polling")
            mark_failed(queue_id, f"No results found for '{search_query}'", schedule_retry=True, retry_delay_minutes=60)
            return False
        
        # Download the result
        logger.info(f"Queue {queue_id}: Downloading '{best_result['filename']}' from {best_result['username']}...")
        update_queue_status(queue_id, 'downloading', found_filename=best_result['filename'])
        
        success = client.download_file(best_result['username'], best_result['filename'], best_result['size'])
        
        if success:
            logger.info(f"Queue {queue_id}: Download queued successfully in slskd")
            logger.info(f"Queue {queue_id}: File will appear in {DOWNLOADS_DIR} when download completes")
            # Status already set to 'downloading' above
            return True
        else:
            logger.error(f"Queue {queue_id}: Failed to queue download in slskd")
            mark_failed(queue_id, "Failed to queue Soulseek download", schedule_retry=True, retry_delay_minutes=15)
            return False
            
    except Exception as e:
        logger.error(f"Queue {queue_id}: Error in search_and_download: {e}")
        mark_failed(queue_id, f"Search error: {str(e)}", schedule_retry=True)
        return False

def check_completed_downloads():
    """Check /downloads folder for completed files and match to queue items"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        if not os.path.isdir(DOWNLOADS_DIR):
            logger.warning(f"Downloads directory does not exist: {DOWNLOADS_DIR}")
            return
        
        # Get all downloading queue items
        cursor.execute("""
            SELECT id, artist, title, album, search_query, found_filename FROM download_queue
            WHERE status = 'downloading'
        """)
        
        downloading = [dict(row) for row in cursor.fetchall()]
        
        if downloading:
            logger.debug(f"Checking {len(downloading)} items in 'downloading' status")
        
        # Get files in downloads folder
        try:
            files = [f for f in os.listdir(DOWNLOADS_DIR) 
                    if f.lower().endswith(('.mp3', '.flac', '.m4a'))]
            if files:
                logger.debug(f"Found {len(files)} audio files in {DOWNLOADS_DIR}")
        except Exception as e:
            logger.error(f"Error scanning downloads folder: {e}")
            conn.close()
            return
        
        # Try to match files to queue items
        for item in downloading:
            match_found = None
            
            # Try exact filename match first
            if item['found_filename'] and item['found_filename'] in files:
                match_found = item['found_filename']
                logger.debug(f"Queue {item['id']}: Exact filename match found")
            else:
                # Try fuzzy matching
                for filename in files:
                    if matches_queue_item(filename, item):
                        match_found = filename
                        logger.debug(f"Queue {item['id']}: Fuzzy match found: {filename}")
                        break
            
            if match_found:
                file_path = os.path.join(DOWNLOADS_DIR, match_found)
                logger.info(f"Queue {item['id']}: Matched file '{match_found}' - marking as completed")
                update_queue_status(item['id'], 'completed', file_path=file_path, found_filename=match_found)
        
        conn.close()
        
    except Exception as e:
        logger.error(f"Error checking completed downloads: {e}")

def matches_queue_item(filename, queue_item):
    """Check if filename matches queue item (fuzzy matching)"""
    try:
        filename_lower = filename.lower()
        artist = (queue_item['artist'] or '').lower()
        title = (queue_item['title'] or '').lower()
        album = (queue_item['album'] or '').lower()
        
        # Count matching terms
        matches = 0
        total = 0
        
        for term in [artist, title, album]:
            if term:
                total += 1
                if term in filename_lower:
                    matches += 1
        
        # Need at least 50% match
        if total > 0 and matches / total >= 0.5:
            return True
        
        # Also check search query
        if queue_item['search_query']:
            terms = queue_item['search_query'].lower().split()
            term_matches = sum(1 for t in terms if t in filename_lower)
            if term_matches >= 2:
                return True
        
        return False
        
    except Exception as e:
        logger.error(f"Error matching filename: {e}")
        return False

def process_queue(client):
    """Process one batch of queued items"""
    try:
        items = get_queued_items(limit=5)
        
        if not items:
            logger.debug("No queued items to process")
        else:
            logger.info(f"Processing {len(items)} queue items...")
        
        processed = 0
        for item in items:
            if not client:
                logger.error("SlskdClient not available, skipping")
                break
            
            try:
                if search_and_download(item['id'], item, client):
                    processed += 1
            except Exception as e:
                logger.error(f"Error processing queue {item['id']}: {e}")
                mark_failed(item['id'], f"Processing error: {str(e)}", schedule_retry=True)
        
        # Always check for completed downloads, even if no new items were processed
        # This ensures downloads that complete between processing cycles are detected
        check_completed_downloads()
        
        return processed
        
    except Exception as e:
        logger.error(f"Error in process_queue: {e}")
        return 0

def run_processor(interval=30):
    """Run queue processor loop"""
    logger.info("=== Queue Processor Started ===")
    logger.info(f"Processing interval: {interval}s")
    
    client = get_slskd_client()
    if not client:
        logger.error("Cannot initialize SlskdClient - exiting")
        sys.exit(1)
    
    loop_count = 0
    
    try:
        while True:
            try:
                loop_count += 1
                logger.debug(f"--- Loop {loop_count} ---")
                
                processed = process_queue(client)
                
                if processed > 0:
                    logger.info(f"Processed {processed} queue items")
                
                time.sleep(interval)
                
            except KeyboardInterrupt:
                logger.info("Queue processor stopped by user")
                break
            except Exception as e:
                logger.error(f"Error in processor loop: {e}")
                logger.error(traceback.format_exc())
                time.sleep(interval)
                
    except KeyboardInterrupt:
        logger.info("Queue processor interrupted")
    finally:
        logger.info("=== Queue Processor Stopped ===")

if __name__ == "__main__":
    # Default interval is 30 seconds
    interval = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    run_processor(interval)
