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

import time
import requests
import logging
from datetime import datetime, timedelta
from typing import Dict

from helpers.db_utils import get_db_connection, _is_postgres_connection, is_postgres_configured

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='[RETRY_MANAGER] %(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class DownloadRetryManager:
    """Manages automatic retries for persistent downloads with method fallback."""
    
    def __init__(self, db_path: str, navidrome_url: str | None = None, navidrome_token: str | None = None):
        """
        Initialize the retry manager.
        
        Args:
            db_path: Database path used only for legacy SQLite fallback
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

    def _open_db(self):
        """Open PostgreSQL connection."""
        try:
            conn = get_db_connection()
            return conn, bool(_is_postgres_connection(conn))
        except Exception:
            if is_postgres_configured():
                raise
            raise RuntimeError("PostgreSQL is required for DownloadRetryManager")

    @staticmethod
    def _placeholder(is_pg: bool) -> str:
        return "%s" if is_pg else "?"

    @staticmethod
    def _row_get(row, key, index=0, default=None):
        if row is None:
            return default
        if hasattr(row, 'keys'):
            return row.get(key, default)
        try:
            return row[index]
        except Exception:
            return default

    @staticmethod
    def _parse_ts(value):
        if not value:
            return None
        text = str(value).replace('Z', '+00:00')
        try:
            return datetime.fromisoformat(text).replace(tzinfo=None)
        except Exception:
            return None
    
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
            conn, is_pg = self._open_db()
            cursor = conn.cursor()
            placeholder = self._placeholder(is_pg)

            cursor.execute("""
                SELECT * FROM managed_downloads
                WHERE persistent_search = 1
                AND status IN ('error', 'waiting_retry')
                AND (retry_count < max_retries)
                ORDER BY priority DESC, created_at ASC
                LIMIT 50
            """)

            all_downloads = cursor.fetchall()
            now = datetime.now()
            downloads = []
            for row in all_downloads:
                retry_delay_seconds = self._row_get(row, 'retry_delay_seconds', 13, 60) or 60
                last_attempt = self._parse_ts(self._row_get(row, 'last_search_attempt', 12))
                if last_attempt is None or (now - last_attempt).total_seconds() >= int(retry_delay_seconds):
                    downloads.append(row)

            stats["total_checked"] = len(downloads)

            for download in downloads[:10]:
                try:
                    artist = self._row_get(download, 'artist', 2, '')
                    title = self._row_get(download, 'release_title', 1, '')
                    download_id = self._row_get(download, 'id', 0)
                    session_id = self._row_get(download, 'session_id', 20)
                    methods_tried = self._row_get(download, 'methods_tried', 17, '') or ''
                    current_method = self._row_get(download, 'current_method', 16) or self._row_get(download, 'method', 5, '')
                    retry_count = int(self._row_get(download, 'retry_count', 8, 0) or 0)
                    max_retries = int(self._row_get(download, 'max_retries', 9, 0) or 0)

                    logger.info(f"Processing download: {artist} - {title} (id: {download_id})")
                    
                    # Check if track is already in Navidrome
                    if self._verify_in_navidrome(artist, title):
                        logger.info(f"✓ Track verified in Navidrome: {artist} - {title}")
                        
                        cursor.execute(
                            f"""
                            UPDATE managed_downloads
                            SET status = 'completed', completion_verified = 1, completed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                            WHERE id = {placeholder}
                            """,
                            (download_id,),
                        )
                        conn.commit()
                        
                        # Update session if applicable
                        if session_id:
                            self._update_session_progress(cursor, session_id, 'complete', is_pg=is_pg)
                        
                        stats["completed"] += 1
                        continue
                    
                    # Check if we should switch methods on repeated failures
                    if self._should_switch_method(download, methods_tried):
                        next_method = self.fallback_methods.get(current_method)
                        if next_method and next_method not in methods_tried:
                            logger.info(f"  Switching method from {current_method} to {next_method} for {title}")
                            
                            # Record that we tried this method
                            tried_methods = f"{methods_tried},{current_method}" if methods_tried else current_method

                            cursor.execute(
                                f"""
                                UPDATE managed_downloads
                                SET current_method = {placeholder}, methods_tried = {placeholder}, last_method_failed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                                WHERE id = {placeholder}
                                """,
                                (next_method, tried_methods, download_id),
                            )
                            conn.commit()
                            stats["method_switched"] += 1
                            continue
                    
                    # Increment retry count
                    new_retry_count = retry_count + 1
                    if max_retries and new_retry_count >= max_retries:
                        next_status = 'error'
                    else:
                        next_status = 'waiting_retry'
                    
                    cursor.execute(
                        f"""
                        UPDATE managed_downloads
                        SET status = {placeholder},
                            retry_count = {placeholder},
                            last_search_attempt = CURRENT_TIMESTAMP,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = {placeholder}
                        """,
                        (next_status, new_retry_count, download_id),
                    )
                    conn.commit()
                    
                    # Log retry info
                    retry_info = f"Marked for retry ({new_retry_count}/{max_retries or 'inf'})"
                    logger.info(f"  {retry_info}: {artist} - {title}")
                    stats["retried"] += 1
                    
                except Exception as e:
                    download_id = self._row_get(download, 'id', 0, 'unknown')
                    logger.error(f"Error processing download {download_id}: {e}")
                    stats["errors"].append(f"Download {download_id}: {str(e)}")
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
            subsonic = data.get("subsonic-response", {})
            results = subsonic.get("searchResult3", {}).get("song", [])
            if not results:
                # Backward compatibility for older servers/legacy payloads
                results = subsonic.get("searchresults2", {}).get("song", [])
            
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
    
    def _update_session_progress(self, cursor, session_id: int, status: str, is_pg: bool = False):
        """
        Update playlist session progress.
        
        Args:
            cursor: Database cursor
            session_id: Playlist session ID
            status: 'complete', 'failed', or 'skip'
        """
        placeholder = self._placeholder(is_pg)

        if status == 'complete':
            cursor.execute("""
                UPDATE playlist_download_sessions
                SET completed_tracks = completed_tracks + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = {placeholder}
            """.format(placeholder=placeholder), (session_id,))
        elif status == 'failed':
            cursor.execute("""
                UPDATE playlist_download_sessions
                SET failed_tracks = failed_tracks + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = {placeholder}
            """.format(placeholder=placeholder), (session_id,))
        elif status == 'skip':
            cursor.execute("""
                UPDATE playlist_download_sessions
                SET skipped_tracks = skipped_tracks + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = {placeholder}
            """.format(placeholder=placeholder), (session_id,))
    
    def cleanup_old_retries(self, days: int = 30):
        """
        Clean up old downloads that have exhausted retries.
        Uses non-blocking deletes to avoid contention with active scans.
        
        Args:
            days: Delete downloads older than this many days with status='error'
        """
        try:
            conn, is_pg = self._open_db()
            cursor = conn.cursor()
            placeholder = self._placeholder(is_pg)

            cutoff_date = datetime.now() - timedelta(days=days)
            cursor.execute(
                f"""
                DELETE FROM managed_downloads
                WHERE status = 'error'
                AND retry_count >= max_retries
                AND created_at < {placeholder}
                """,
                (cutoff_date.isoformat(),),
            )
            rows_deleted = cursor.rowcount
            conn.commit()
            conn.close()

            if rows_deleted > 0:
                logger.info(f"Cleaned up {rows_deleted} old failed downloads total")

        except sqlite3.OperationalError as e:
            if "database is locked" in str(e):
                logger.debug("Cleanup skipped - database is locked (active scan in progress)")
            else:
                logger.error(f"Error in cleanup: {e}")
        except Exception as e:
            logger.error(f"Error in cleanup: {e}")


def run_retry_manager(db_path: str, navidrome_url: str | None = None, navidrome_token: str | None = None) -> Dict:
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
