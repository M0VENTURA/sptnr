#!/usr/bin/env python3
"""
Download Retry Manager
======================

Manages persistent search for downloads with automatic retries.
Runs periodically to check for failed downloads and retry them.

Features:
- Automatic retry of failed downloads
- Persistent search until track is found
- Verification in Navidrome after completion
- Fallback between qBittorrent and Soulseek
"""

import sqlite3
import time
import requests
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, List

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='[RETRY_MANAGER] %(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class DownloadRetryManager:
    """Manages automatic retries for persistent downloads with method fallback."""
    
    def __init__(self, db_path: str, navidrome_url: str = None, navidrome_token: str = None):
        """
        Initialize the retry manager.
        
        Args:
            db_path: Path to SQLite database
            navidrome_url: Base URL for Navidrome API
            navidrome_token: Authentication token for Navidrome
        """
        self.db_path = db_path
        self.navidrome_url = navidrome_url or "http://localhost:4533"
        self.navidrome_token = navidrome_token or ""
        self.session = requests.Session()
        if self.navidrome_token:
            self.session.headers.update({"X-Auth-Token": self.navidrome_token})
        
        # Fallback mapping: if one method fails, try the other
        self.fallback_methods = {
            'slskd': 'qbittorrent',
            'qbittorrent': 'slskd'
        }
    
    def check_and_retry(self):
        """
        Check for downloads that need retrying and attempt to retry them.
        
        Returns:
            Dict with retry statistics
        """
        stats = {
            "total_checked": 0,
            "retried": 0,
            "completed": 0,
            "failed": 0,
            "method_switched": 0,
            "errors": []
        }
        
        try:
            conn = sqlite3.connect(self.db_path, timeout=120.0)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            # Set WAL mode for better concurrency
            cursor.execute("PRAGMA query_only = OFF")
            cursor.execute("PRAGMA journal_mode = WAL")
            
            # Get downloads pending retry
            cursor.execute("""
                SELECT * FROM managed_downloads
                WHERE persistent_search = 1
                AND status IN ('error', 'waiting_retry')
                AND (retry_count < max_retries)
                AND (
                    last_search_attempt IS NULL
                    OR datetime(last_search_attempt) <= datetime('now', '-' || retry_delay_seconds || ' seconds')
                )
                ORDER BY priority DESC, created_at ASC
                LIMIT 10
            """)
            
            downloads = cursor.fetchall()
            stats["total_checked"] = len(downloads)
            
            for download in downloads:
                try:
                    logger.info(f"Processing download: {download['artist']} - {download['release_title']} (id: {download['id']})")
                    
                    # Check if track is already in Navidrome
                    if self._verify_in_navidrome(download['artist'], download['release_title']):
                        logger.info(f"✓ Track verified in Navidrome: {download['artist']} - {download['release_title']}")
                        
                        # Retry update with exponential backoff on lock
                        update_attempts = 3
                        for attempt in range(update_attempts):
                            try:
                                cursor.execute("""
                                    UPDATE managed_downloads
                                    SET status = 'completed', completion_verified = 1, completed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                                    WHERE id = ?
                                """, (download['id'],))
                                conn.commit()
                                break
                            except sqlite3.OperationalError as e:
                                if "database is locked" in str(e) and attempt < update_attempts - 1:
                                    time.sleep(0.5 * (2 ** attempt))
                                else:
                                    raise
                        
                        # Update session if applicable
                        if download['session_id']:
                            self._update_session_progress(cursor, download['session_id'], 'complete')
                        
                        stats["completed"] += 1
                        continue
                    
                    # Check if we should switch methods on repeated failures
                    methods_tried = download['methods_tried'] or ""
                    current_method = download['current_method'] or download['method']
                    
                    if self._should_switch_method(download, methods_tried):
                        next_method = self.fallback_methods.get(current_method)
                        if next_method and next_method not in methods_tried:
                            logger.info(f"  Switching method from {current_method} to {next_method} for {download['release_title']}")
                            
                            # Record that we tried this method
                            tried_methods = f"{methods_tried},{current_method}" if methods_tried else current_method
                            
                            # Retry update with exponential backoff on lock
                            update_attempts = 3
                            for attempt in range(update_attempts):
                                try:
                                    cursor.execute("""
                                        UPDATE managed_downloads
                                        SET current_method = ?, methods_tried = ?, last_method_failed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                                        WHERE id = ?
                                    """, (next_method, tried_methods, download['id']))
                                    conn.commit()
                                    break
                                except sqlite3.OperationalError as e:
                                    if "database is locked" in str(e) and attempt < update_attempts - 1:
                                        time.sleep(0.5 * (2 ** attempt))
                                    else:
                                        raise
                            stats["method_switched"] += 1
                            continue
                    
                    # Increment retry count
                    new_retry_count = download['retry_count'] + 1
                    
                    # Mark as waiting_retry with updated timestamp
                    # Retry update with exponential backoff on lock
                    update_attempts = 3
                    for attempt in range(update_attempts):
                        try:
                            cursor.execute("""
                                UPDATE managed_downloads
                                SET status = 'waiting_retry', 
                                    retry_count = ?,
                                    last_search_attempt = CURRENT_TIMESTAMP,
                                    updated_at = CURRENT_TIMESTAMP
                                WHERE id = ?
                            """, (new_retry_count, download['id']))
                            conn.commit()
                            break
                        except sqlite3.OperationalError as e:
                            if "database is locked" in str(e) and attempt < update_attempts - 1:
                                time.sleep(0.5 * (2 ** attempt))
                            else:
                                raise
                    
                    # Log retry info
                    retry_info = f"Marked for retry ({new_retry_count}/{download['max_retries']})"
                    logger.info(f"  {retry_info}: {download['artist']} - {download['release_title']}")
                    stats["retried"] += 1
                    
                except Exception as e:
                    logger.error(f"Error processing download {download['id']}: {e}")
                    stats["errors"].append(f"Download {download['id']}: {str(e)}")
                    stats["failed"] += 1
            
            conn.close()
            
        except Exception as e:
            logger.error(f"Fatal error in retry manager: {e}")
            stats["errors"].append(f"Fatal error: {str(e)}")
        
        return stats
    
    def _verify_in_navidrome(self, artist: str, title: str) -> bool:
        """
        Check if a track exists in Navidrome with matching metadata.
        
        Args:
            artist: Artist name
            title: Track/release title
            
        Returns:
            True if track found and playable in Navidrome
        """
        try:
            # Search Navidrome for the track
            search_url = f"{self.navidrome_url}/rest/search3.view"
            params = {
                "query": f"{artist} {title}",
                "songCount": 1
            }
            
            response = self.session.get(search_url, params=params, timeout=10)
            if response.status_code != 200:
                logger.debug(f"Navidrome search failed: {response.status_code}")
                return False
            
            data = response.json()
            results = data.get("subsonic-response", {}).get("searchresults2", {}).get("song", [])
            
            if isinstance(results, dict):
                results = [results]
            
            # Check if any result matches artist
            for result in results:
                if isinstance(result, dict):
                    result_artist = result.get("artist", "").lower()
                    result_title = result.get("title", "").lower()
                    
                    # Simple matching
                    if artist.lower() in result_artist or result_artist in artist.lower():
                        logger.debug(f"Found match: {result_artist} - {result_title}")
                        return True
            
            return False
            
        except Exception as e:
            logger.debug(f"Error verifying in Navidrome: {e}")
            return False
    
    def _should_switch_method(self, download: dict, methods_tried: str) -> bool:
        """
        Determine if we should switch to a fallback method.
        
        Switch if:
        - Retry count >= 2 (failed twice with same method)
        - Current method not yet tried with all retries
        - Fallback method available and not yet tried
        
        Args:
            download: Download record
            methods_tried: Comma-separated list of tried methods
            
        Returns:
            True if should switch methods
        """
        # Switch after 2 consecutive failures
        if download['retry_count'] < 2:
            return False
        
        current_method = download['current_method'] or download['method']
        fallback = self.fallback_methods.get(current_method)
        
        # Can only switch if fallback exists
        if not fallback:
            return False
        
        # Don't switch if already tried both methods
        if fallback in (methods_tried or ""):
            return False
        
        return True
    
    def _update_session_progress(self, cursor, session_id: int, status: str):
        """
        Update playlist session progress.
        
        Args:
            cursor: Database cursor
            session_id: Playlist session ID
            status: 'complete', 'failed', or 'skip'
        """
        if status == 'complete':
            cursor.execute("""
                UPDATE playlist_download_sessions
                SET completed_tracks = completed_tracks + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (session_id,))
        elif status == 'failed':
            cursor.execute("""
                UPDATE playlist_download_sessions
                SET failed_tracks = failed_tracks + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (session_id,))
        elif status == 'skip':
            cursor.execute("""
                UPDATE playlist_download_sessions
                SET skipped_tracks = skipped_tracks + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (session_id,))
    
    def cleanup_old_retries(self, days: int = 30):
        """
        Clean up old downloads that have exhausted retries.
        
        Args:
            days: Delete downloads older than this many days with status='error'
        """
        max_retries = 3
        for attempt in range(max_retries):
            try:
                conn = sqlite3.connect(self.db_path, timeout=120.0)
                cursor = conn.cursor()
                
                # Set WAL mode for better concurrency
                cursor.execute("PRAGMA query_only = OFF")
                cursor.execute("PRAGMA journal_mode = WAL")
                
                cutoff_date = datetime.now() - timedelta(days=days)
                
                cursor.execute("""
                    DELETE FROM managed_downloads
                    WHERE status = 'error'
                    AND retry_count >= max_retries
                    AND created_at < ?
                """, (cutoff_date.isoformat(),))
                
                deleted = cursor.rowcount
                if deleted > 0:
                    logger.info(f"Cleaned up {deleted} old failed downloads")
                
                conn.commit()
                conn.close()
                return
                
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e):
                    if attempt < max_retries - 1:
                        wait_time = 0.5 * (2 ** attempt)  # Exponential backoff
                        logger.debug(f"Database locked during cleanup, retrying in {wait_time}s (attempt {attempt + 1}/{max_retries})")
                        time.sleep(wait_time)
                    else:
                        logger.error(f"Error in cleanup: database is locked after {max_retries} retries")
                else:
                    logger.error(f"Error in cleanup: {e}")
                    return
            except Exception as e:
                logger.error(f"Error in cleanup: {e}")
                return


def run_retry_manager(db_path: str, navidrome_url: str = None, navidrome_token: str = None) -> Dict:
    """
    Standalone function to run the retry manager once.
    
    Args:
        db_path: Path to SQLite database
        navidrome_url: Base URL for Navidrome
        navidrome_token: Auth token for Navidrome
        
    Returns:
        Statistics dictionary
    """
    manager = DownloadRetryManager(db_path, navidrome_url, navidrome_token)
    stats = manager.check_and_retry()
    manager.cleanup_old_retries()
    return stats


if __name__ == "__main__":
    # Test the retry manager
    import os
    
    db_path = os.environ.get("DATABASE_PATH", "music.db")
    navidrome_url = os.environ.get("NAVIDROME_URL", "http://localhost:4533")
    navidrome_token = os.environ.get("NAVIDROME_TOKEN", "")
    
    stats = run_retry_manager(db_path, navidrome_url, navidrome_token)
    
    print(f"\n📊 Retry Manager Results:")
    print(f"  Total checked: {stats['total_checked']}")
    print(f"  Retried: {stats['retried']}")
    print(f"  Completed: {stats['completed']}")
    print(f"  Failed: {stats['failed']}")
    
    if stats['errors']:
        print(f"  Errors: {len(stats['errors'])}")
        for error in stats['errors']:
            print(f"    - {error}")
