#!/usr/bin/env python3
"""
Scan History Tracker
Tracks individual album scans across different scan types (Navidrome, Popularity, Beets)
"""

import sqlite3
import logging
from datetime import datetime
import os

DB_PATH = os.environ.get("DB_PATH", "/database/sptnr.db")

def log_album_scan(artist: str, album: str, scan_type: str, tracks_processed: int = 0, status: str = "completed"):
    """
    Log an album scan to the scan_history table.
    
    Args:
        artist: Artist name
        album: Album name
        scan_type: Type of scan ('navidrome', 'popularity', or 'beets')
        tracks_processed: Number of tracks processed
        status: Status of the scan ('completed', 'error', 'skipped')
    """
    logging.info(f"log_album_scan called: artist='{artist}', album='{album}', type={scan_type}, tracks={tracks_processed}, status={status}")
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30.0)
        conn.execute("PRAGMA journal_mode=WAL")
        
        # Create table if it doesn't exist
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scan_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                artist TEXT NOT NULL,
                album TEXT NOT NULL,
                scan_type TEXT NOT NULL,
                scan_timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                tracks_processed INTEGER DEFAULT 0,
                status TEXT DEFAULT 'completed'
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
            INSERT INTO scan_history (artist, album, scan_type, tracks_processed, status)
            VALUES (?, ?, ?, ?, ?)
        """, (artist, album, scan_type, tracks_processed, status))
        
        conn.commit()
        conn.close()
        
        logging.info(f"Successfully logged {scan_type} scan for {artist} - {album} to scan_history with DB_PATH={DB_PATH}")
    except Exception as e:
        logging.error(f"Error logging album scan for '{artist}' - '{album}': {e}")
        logging.error(f"DB_PATH={DB_PATH}")
        import traceback
        logging.error(traceback.format_exc())

def get_recent_album_scans(limit: int = 10):
    """
    Get recent album scans with scan type information.
    
    Args:
        limit: Maximum number of scans to return
        
    Returns:
        List of dicts with scan information
    """
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30.0)
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
            SELECT artist, album, scan_type, scan_timestamp, tracks_processed, status
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
                'status': row['status']
            })
        
        conn.close()
        return scans
    except Exception as e:
        logging.error(f"Error getting recent scans: {e}")
        return []
