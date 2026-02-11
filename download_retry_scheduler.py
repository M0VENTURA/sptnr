#!/usr/bin/env python3
"""
Download Retry Scheduler - Background service for automatically retrying failed downloads.
Runs on a configurable interval (default: 10 minutes).
"""

import threading
import time
import logging
from datetime import datetime, timedelta
from typing import Optional, Callable
import sqlite3

logger = logging.getLogger(__name__)

class DownloadRetryScheduler:
    """Background scheduler for automatic download retries"""
    
    def __init__(self, db_path: str = "/database/sptnr.db", interval_minutes: int = 10):
        """
        Initialize the retry scheduler.
        
        Args:
            db_path: Path to the database
            interval_minutes: How often to check for retries (in minutes)
        """
        self.db_path = db_path
        self.interval_seconds = interval_minutes * 60
        self.running = False
        self.thread: Optional[threading.Thread] = None
        self.on_retry_callback: Optional[Callable] = None
        self.on_complete_callback: Optional[Callable] = None
    
    def start(self):
        """Start the background scheduler"""
        if self.running:
            logger.warning("Retry scheduler already running")
            return
        
        self.running = True
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()
        logger.info(f"✓ Download retry scheduler started (interval: {self.interval_seconds}s)")
    
    def stop(self):
        """Stop the background scheduler"""
        self.running = False
        if self.thread:
            self.thread.join(timeout=5)
        logger.info("Download retry scheduler stopped")
    
    def set_retry_callback(self, callback: Callable):
        """Set callback to execute when retries are processed"""
        self.on_retry_callback = callback
    
    def set_complete_callback(self, callback: Callable):
        """Set callback to execute when retry run completes"""
        self.on_complete_callback = callback
    
    def _run_loop(self):
        """Main scheduler loop"""
        logger.info("Retry scheduler thread started")
        
        while self.running:
            try:
                # Check for files due for retry
                self._check_and_process_retries()
                
                # Sleep for the configured interval
                time.sleep(self.interval_seconds)
            except Exception as e:
                logger.error(f"Error in retry scheduler loop: {e}")
                time.sleep(60)  # Retry after 1 minute on error
    
    def _check_and_process_retries(self):
        """Check for and process files due for retry"""
        try:
            conn = sqlite3.connect(self.db_path, timeout=30.0)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            now = datetime.now().isoformat()
            
            # Get files due for retry
            cursor.execute("""
                SELECT id, file_path, filename, artist, album, title, 
                       retry_count, max_retries, next_retry_at
                FROM download_queue 
                WHERE status = 'incomplete'
                AND retry_count < max_retries
                AND (next_retry_at IS NULL OR next_retry_at <= ?)
                ORDER BY next_retry_at ASC, created_at ASC
                LIMIT 100
            """, (now,))
            
            items = cursor.fetchall()
            
            if items:
                logger.info(f"Found {len(items)} files due for retry")
                
                for item in items:
                    logger.info(f"Retry queued: {item['filename']} (attempt {item['retry_count']}/{item['max_retries']})")
                    
                    if self.on_retry_callback:
                        try:
                            self.on_retry_callback(dict(item))
                        except Exception as e:
                            logger.error(f"Error in retry callback: {e}")
            
            conn.close()
            
            if self.on_complete_callback:
                try:
                    self.on_complete_callback({
                        'timestamp': now,
                        'items_processed': len(items)
                    })
                except Exception as e:
                    logger.error(f"Error in complete callback: {e}")
        
        except Exception as e:
            logger.error(f"Error checking/processing retries: {e}")
    
    def get_status(self) -> dict:
        """Get scheduler status"""
        try:
            conn = sqlite3.connect(self.db_path, timeout=30.0)
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT COUNT(*) as incomplete_count FROM download_queue 
                WHERE status = 'incomplete'
            """)
            incomplete = cursor.fetchone()[0]
            
            cursor.execute("""
                SELECT COUNT(*) as ready_count FROM download_queue 
                WHERE status = 'incomplete'
                AND (next_retry_at IS NULL OR next_retry_at <= ?)
            """, (datetime.now().isoformat(),))
            ready = cursor.fetchone()[0]
            
            cursor.execute("""
                SELECT COUNT(*) as exists_count FROM download_queue 
                WHERE status = 'exists_in_library'
            """)
            exists = cursor.fetchone()[0]
            
            conn.close()
            
            return {
                'running': self.running,
                'interval_seconds': self.interval_seconds,
                'incomplete_count': incomplete,
                'ready_for_retry': ready,
                'exists_in_library': exists
            }
        except Exception as e:
            logger.error(f"Error getting scheduler status: {e}")
            return {
                'running': self.running,
                'error': str(e)
            }


# Global scheduler instance
_scheduler: Optional[DownloadRetryScheduler] = None

def get_scheduler() -> DownloadRetryScheduler:
    """Get or create the global scheduler instance"""
    global _scheduler
    if _scheduler is None:
        _scheduler = DownloadRetryScheduler()
    return _scheduler

def init_scheduler(db_path: str = "/database/sptnr.db", interval_minutes: int = 10):
    """Initialize and start the scheduler"""
    global _scheduler
    _scheduler = DownloadRetryScheduler(db_path=db_path, interval_minutes=interval_minutes)
    _scheduler.start()
    return _scheduler

def stop_scheduler():
    """Stop the scheduler"""
    global _scheduler
    if _scheduler:
        _scheduler.stop()
