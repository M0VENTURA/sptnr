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
import traceback
import yaml
from datetime import datetime, timedelta
from pathlib import Path

# Use unified logging system - all logs go to debug.log
from helpers.logging_config import (
    setup_logging,
    log_unified,
    log_info,
    log_debug
)

# Set up logging with Queue Processor service name
setup_logging("QueueProcessor")

# Create logger reference for compatibility with existing code
import logging
logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "/database/sptnr.db")


def resolve_downloads_dir():
    """Resolve downloads directory from env/config with safe fallback."""
    def _prefer_music_subfolder(path: str) -> str:
        if not path:
            return path
        normalized = os.path.normpath(path)
        if os.path.basename(normalized).lower() == "downloads":
            music_subdir = os.path.join(normalized, "Music")
            if os.path.isdir(music_subdir):
                return music_subdir
        return path

    env_dir = os.environ.get("DOWNLOADS_DIR")
    if env_dir:
        return _prefer_music_subfolder(env_dir)

    config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")
    try:
        if os.path.exists(config_path):
            with open(config_path, 'r', encoding='utf-8') as f:
                cfg = yaml.safe_load(f) or {}
            configured = (cfg.get('downloads') or {}).get('folder')
            if configured:
                return _prefer_music_subfolder(configured)
    except Exception as e:
        logger.warning(f"Could not read downloads folder from config: {e}")

    return "/downloads/Music"


DOWNLOADS_DIR = resolve_downloads_dir()

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

def cleanup_stuck_searching_items():
    """Detect and mark as failed any items stuck in 'searching' for too long"""
    try:
        conn = get_db()
        cursor = conn.cursor()
        
        # Items stuck in 'searching' for more than 90 seconds are likely hung
        stuck_threshold = (datetime.now() - timedelta(seconds=90)).isoformat()
        
        cursor.execute("""
            SELECT id, artist, title, updated_at FROM download_queue
            WHERE status = 'searching'
            AND updated_at < ?
        """, (stuck_threshold,))
        
        stuck_items = cursor.fetchall()
        
        if stuck_items:
            logger.warning(f"Found {len(stuck_items)} items stuck in 'searching' status, marking for retry...")
            
            for item in stuck_items:
                item_id = item['id']
                logger.warning(
                    f"Queue {item_id}: Detected stuck search ({item['artist']} - {item['title']}, "
                    f"updated at {item['updated_at']}), marking for retry..."
                )
                mark_failed(
                    item_id,
                    "Stuck in searching state (likely slskd unresponsive)",
                    schedule_retry=True,
                    retry_delay_minutes=15
                )
        
        conn.close()
        return len(stuck_items)
        
    except Exception as e:
        logger.error(f"Error cleaning up stuck searching items: {e}")
        return 0

def get_queued_items(limit=10):
    """Get items ready to process (queued or scheduled for retry)"""
    try:
        # First, clean up any items stuck in 'searching' state
        cleanup_stuck_searching_items()
        
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
        cursor.execute(f"SELECT retry_count FROM download_queue WHERE id = {placeholder}", (queue_id,))
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
        
        cursor.execute(f"""
            UPDATE download_queue 
            SET status = {placeholder}, retry_count = {placeholder}, failure_reason = {placeholder}, last_failure_time = CURRENT_TIMESTAMP,
                next_retry_at = {placeholder}, updated_at = CURRENT_TIMESTAMP
            WHERE id = {placeholder}
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
        
        # Poll for results (up to MAX_POLL_ATTEMPTS seconds with 1 second intervals)
        # Increased timeout to 45 seconds to handle slow Soulseek peer responses
        MAX_POLL_ATTEMPTS = 45
        best_result = None
        poll_start_time = datetime.now()
        
        for poll_attempt in range(MAX_POLL_ATTEMPTS):
            time.sleep(1)
            
            try:
                responses, state, is_complete = client.get_search_results(search_id)
                
                logger.debug(f"Queue {queue_id}: Poll {poll_attempt+1}/{MAX_POLL_ATTEMPTS} - Got {len(responses)} responses, state={state}")
                
                if responses:
                    # Find best result (first file from first user with files)
                    for resp_idx, resp in enumerate(responses):
                        if hasattr(resp, 'files') and resp.files and len(resp.files) > 0:
                            logger.debug(f"Queue {queue_id}: Response {resp_idx} from {resp.username} has {len(resp.files)} files")
                            # Get file info - handle both object and dict formats
                            file_info = resp.files[0]
                            filename = getattr(file_info, 'filename', file_info.get('filename', '')) if isinstance(file_info, dict) else getattr(file_info, 'filename', '')
                            size = getattr(file_info, 'size', file_info.get('size', 0)) if isinstance(file_info, dict) else getattr(file_info, 'size', 0)
                            
                            best_result = {
                                "username": resp.username,
                                "filename": filename,
                                "size": size
                            }
                            logger.info(f"Queue {queue_id}: ✓ Found result after {poll_attempt+1}s from {resp.username}")
                            logger.info(f"Queue {queue_id}: File: {str(filename)[:80]}... ({size} bytes)")
                            break
                        else:
                            logger.debug(f"Queue {queue_id}: Response {resp_idx} from {getattr(resp, 'username', 'unknown')} has no files or empty files list")
                    
                    if best_result:
                        break
                
                # Exit early if search is complete and we have results
                if is_complete and best_result:
                    logger.info(f"Queue {queue_id}: Search complete with results, stopping polling")
                    break
                    
            except Exception as e:
                logger.warning(f"Queue {queue_id}: Error polling results (attempt {poll_attempt+1}): {e}")
                logger.debug(traceback.format_exc())
        
        if not best_result:
            elapsed = (datetime.now() - poll_start_time).total_seconds()
            logger.warning(f"Queue {queue_id}: ✗ No results found after {elapsed:.0f}s of polling")
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
        logger.debug(traceback.format_exc())
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
        
        # Get audio files in downloads folder recursively
        try:
            files = []
            for root, _, root_files in os.walk(DOWNLOADS_DIR):
                for f in root_files:
                    if f.lower().endswith(('.mp3', '.flac', '.m4a')):
                        files.append(os.path.relpath(os.path.join(root, f), DOWNLOADS_DIR))
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
            if item['found_filename']:
                for rel_file in files:
                    rel_file_norm = rel_file.replace('\\', '/')
                    found_norm = str(item['found_filename']).replace('\\', '/')
                    if rel_file_norm == found_norm or os.path.basename(rel_file_norm) == os.path.basename(found_norm):
                        match_found = rel_file
                        break

            if match_found:
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
        
        # Process completed downloads with MusicBrainz/Discogs metadata
        try:
            from post_download_processor import process_pending_completed_items
            post_stats = process_pending_completed_items(limit=5)
            if post_stats.get('processed', 0) > 0:
                logger.info(f"Post-download processing: {post_stats['processed']} items organized")
        except Exception as e:
            logger.error(f"Error in post-download processing: {e}")
        
        return processed
        
    except Exception as e:
        logger.error(f"Error in process_queue: {e}")
        return 0


def _load_auto_discovery_settings():
    """Load persistent auto-discovery settings from config/env with safe defaults."""
    enabled = True
    interval_seconds = 60

    # Optional env overrides for quick control.
    env_enabled = os.environ.get("DOWNLOADS_AUTO_DISCOVER_ENABLED")
    env_interval = os.environ.get("DOWNLOADS_AUTO_DISCOVER_INTERVAL_SECONDS")

    if env_enabled is not None:
        enabled = str(env_enabled).strip().lower() in {"1", "true", "yes", "on"}

    if env_interval:
        try:
            interval_seconds = int(env_interval)
        except ValueError:
            logger.warning("Invalid DOWNLOADS_AUTO_DISCOVER_INTERVAL_SECONDS='%s'", env_interval)

    # Config file settings override defaults when present.
    config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")
    try:
        if os.path.exists(config_path):
            with open(config_path, 'r', encoding='utf-8') as f:
                cfg = yaml.safe_load(f) or {}

            features = cfg.get('features') or {}
            discovery_cfg = features.get('downloads_auto_discover') or {}

            if 'enabled' in discovery_cfg:
                enabled = bool(discovery_cfg.get('enabled'))
            if 'interval_seconds' in discovery_cfg:
                interval_seconds = int(discovery_cfg.get('interval_seconds') or interval_seconds)
    except Exception as e:
        logger.warning(f"Could not read auto-discovery settings: {e}")

    if interval_seconds < 15:
        interval_seconds = 15

    return enabled, interval_seconds


def maybe_auto_discover_files(now_ts, last_run_ts):
    """Run background auto-discovery on interval and return updated last-run timestamp."""
    enabled, interval_seconds = _load_auto_discovery_settings()
    if not enabled:
        return last_run_ts

    if last_run_ts is not None and (now_ts - last_run_ts) < interval_seconds:
        return last_run_ts

    try:
        from download_queue_manager import auto_discover_and_queue_files

        stats = auto_discover_and_queue_files()
        queued = int(stats.get('queued', 0) or 0)
        scanned = int(stats.get('scanned', 0) or 0)
        if queued > 0:
            logger.info(
                "[AUTO-DISCOVER] Added %s new files to queue (scanned=%s)",
                queued,
                scanned,
            )
        else:
            logger.debug("[AUTO-DISCOVER] No new files found (scanned=%s)", scanned)
    except Exception as e:
        logger.error(f"[AUTO-DISCOVER] Error during background discovery: {e}")

    return now_ts


def maybe_check_musicbrainz_files(now_ts, last_run_ts, interval_seconds=30):
    """
    Run MusicBrainz file matching on interval and return updated last-run timestamp.
    Checks for new files matching active releases every 30 seconds.
    """
    if last_run_ts is not None and (now_ts - last_run_ts) < interval_seconds:
        return last_run_ts

    try:
        from musicbrainz_file_matcher import get_matcher
        
        matcher = get_matcher()
        result = matcher.monitor_and_match()
        matched = result.get("matched", 0)
        
        if matched > 0:
            logger.info(f"[MB_FILE_MATCHER] Matched {matched} files to releases")
        else:
            logger.debug("[MB_FILE_MATCHER] No new matches found")
            
    except Exception as e:
        logger.error(f"[MB_FILE_MATCHER] Error during file matching: {e}")

    return now_ts


def maybe_finalize_musicbrainz_releases(now_ts, last_run_ts, interval_seconds=60):
    """
    Run MusicBrainz release finalization on interval and return updated last-run timestamp.
    Finalizes releases when all tracks are discovered (every 60 seconds).
    """
    if last_run_ts is not None and (now_ts - last_run_ts) < interval_seconds:
        return last_run_ts

    try:
        from musicbrainz_finalizer import get_finalizer
        
        finalizer = get_finalizer()
        result = finalizer.check_and_finalize_releases()
        finalized = result.get("finalized", 0)
        
        if finalized > 0:
            logger.info(f"[MB_FINALIZER] Finalized {finalized} releases")
        else:
            logger.debug("[MB_FINALIZER] No releases ready for finalization")
            
    except Exception as e:
        logger.error(f"[MB_FINALIZER] Error during release finalization: {e}")

    return now_ts

def run_processor(interval=30):
    """Run queue processor loop"""
    logger.info("=== Queue Processor Started ===")
    logger.info(f"Processing interval: {interval}s")
    
    client = get_slskd_client()
    if not client:
        logger.error("Cannot initialize SlskdClient - exiting")
        sys.exit(1)
    
    loop_count = 0
    last_auto_discover_ts = None
    last_mb_check_ts = None
    last_mb_finalize_ts = None
    
    try:
        while True:
            try:
                loop_count += 1
                logger.debug(f"--- Loop {loop_count} ---")

                now_ts = time.time()
                last_auto_discover_ts = maybe_auto_discover_files(now_ts, last_auto_discover_ts)
                last_mb_check_ts = maybe_check_musicbrainz_files(now_ts, last_mb_check_ts)
                last_mb_finalize_ts = maybe_finalize_musicbrainz_releases(now_ts, last_mb_finalize_ts)
                
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
