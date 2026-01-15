#!/usr/bin/env python3
"""
Scan History Tracker
Tracks individual album scans across different scan types (Navidrome, Popularity, Beets)
"""

import sqlite3
import logging
from datetime import datetime
import os
import time

DB_PATH = os.environ.get("DB_PATH", "/database/sptnr.db")


def log_album_scan(artist: str, album: str, scan_type: str, tracks_processed: int = 0, status: str = "completed", source: str = ""):
    """
    Log an album scan to the scan_history table with retry logic for database locks.
    
    Args:
        artist: Artist name
        album: Album name
        scan_type: Type of scan ('navidrome', 'popularity', 'singles', 'unified', or 'beets')
        tracks_processed: Number of tracks processed
        status: Status of the scan ('completed', 'error', 'skipped')
        source: Optional source information (e.g., which APIs were used for detection)
    """
    logging.info(f"log_album_scan called: artist='{artist}', album='{album}', type={scan_type}, tracks={tracks_processed}, status={status}")
    
    max_retries = 3
    retry_delay = 0.5  # Start with 500ms delay
    
    for attempt in range(max_retries):
        try:
            # Use higher timeout and WAL mode for better concurrency
            conn = sqlite3.connect(DB_PATH, timeout=120.0)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")  # 5 second busy timeout
            
            # Create table if it doesn't exist
            conn.execute("""
                CREATE TABLE IF NOT EXISTS scan_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    artist TEXT NOT NULL,
                    album TEXT NOT NULL,
                    scan_type TEXT NOT NULL,
                    scan_timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    tracks_processed INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'completed',
                    source TEXT DEFAULT ''
                )
            """)
            
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_scan_history_timestamp 
                ON scan_history(scan_timestamp DESC)
            """)
            
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_scan_history_artist_album 
                ON scan_history(artist, album)
            """)
            
            # Insert scan record
            conn.execute("""
                INSERT INTO scan_history (artist, album, scan_type, tracks_processed, status, source)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (artist, album, scan_type, tracks_processed, status, source))
            
            conn.commit()
            conn.close()
            
            logging.info(f"Successfully logged {scan_type} scan for '{artist}' - '{album}' to scan_history")
            return  # Success, exit function
            
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e) and attempt < max_retries - 1:
                # Transient lock, retry with exponential backoff
                wait_time = retry_delay * (2 ** attempt)  # 0.5s, 1s, 2s
                logging.warning(f"Database locked when logging {scan_type} scan, retrying in {wait_time}s (attempt {attempt + 1}/{max_retries}): {e}")
                time.sleep(wait_time)
                continue
            else:
                logging.error(f"Error logging album scan for '{artist}' - '{album}' after {attempt + 1} attempts: {e}")
                logging.error(f"DB_PATH={DB_PATH}")
                return  # Return gracefully on final failure instead of raising
        except Exception as e:
            logging.error(f"Error logging album scan for '{artist}' - '{album}': {e}")
            logging.error(f"DB_PATH={DB_PATH}")
            import traceback
            logging.error(traceback.format_exc())
            return

def was_album_scanned(artist: str, album: str, scan_type: str) -> bool:
    """
    Check if an album was already successfully scanned by a specific scan type.
    
    Args:
        artist: Artist name
        album: Album name
        scan_type: Type of scan to check ('navidrome', 'popularity', 'singles', 'unified', 'beets')
        
    Returns:
        True if album was already successfully scanned, False otherwise
    """
    try:
        conn = sqlite3.connect(DB_PATH, timeout=120.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")  # 5 second busy timeout
        cursor = conn.cursor()
        
        # Check if scan_history table exists
        cursor.execute("""
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name='scan_history'
        """)
        
        if not cursor.fetchone():
            # Table doesn't exist yet, assume not scanned
            conn.close()
            return False
        
        # Check for successful scans of this album with this scan type
        # Using LIMIT 1 for efficiency - we only need to know if any record exists
        cursor.execute("""
            SELECT 1 FROM scan_history
            WHERE artist = ? AND album = ? AND scan_type = ? AND status = 'completed'
            LIMIT 1
        """, (artist, album, scan_type))
        
        result = cursor.fetchone()
        conn.close()
        
        return result is not None
    except Exception as e:
        logging.error(f"Error checking album scan history: {e}")
        logging.error(f"DB_PATH={DB_PATH}")
        # Return False on error to ensure albums will be scanned even if there's a database error,
        # preventing data loss at the cost of potential duplicate scans
        return False

def get_recent_album_scans(limit: int = 10):
    """
    Get recent album scans with scan type information.
    
    Args:
        limit: Maximum number of scans to return
        
    Returns:
        List of dicts with scan information
    """
    try:
        conn = sqlite3.connect(DB_PATH, timeout=120.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")  # 5 second busy timeout
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Check if scan_history table exists
        cursor.execute("""
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name='scan_history'
        """)
        
        if not cursor.fetchone():
            # Table doesn't exist yet, return empty list
            conn.close()
            return []
        
        cursor.execute("""
            SELECT artist, album, scan_type, scan_timestamp, tracks_processed, status, source
            FROM scan_history
            ORDER BY scan_timestamp DESC
            LIMIT ?
        """, (limit,))
        
        scans = []
        for row in cursor.fetchall():
            scans.append({
                'artist': row['artist'],
                'album': row['album'],
                'scan_type': row['scan_type'],
                'scan_timestamp': row['scan_timestamp'],
                'tracks_processed': row['tracks_processed'],
                'status': row['status'],
                'source': row['source'] if 'source' in row.keys() else ''
            })
        
        conn.close()
        return scans
    except Exception as e:
        logging.error(f"Error getting recent scans: {e}")
        logging.error(f"DB_PATH={DB_PATH}")
        return []
