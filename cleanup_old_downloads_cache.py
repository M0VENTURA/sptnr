#!/usr/bin/env python3
"""
Cleanup script to remove stale download_queue entries with old downloads paths.
Run this after changing the downloads folder path to clear cached entries.
"""

import sqlite3
import os
import logging

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

def cleanup_old_downloads_paths():
    """Remove download_queue entries that point to old /downloads path instead of /downloads/Music"""
    
    db_path = "sptnr.db"
    if not os.path.exists(db_path):
        logger.error(f"Database not found at {db_path}")
        return
    
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Check table exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='download_queue'")
        if not cursor.fetchone():
            logger.error("download_queue table not found")
            return
        
        # Count entries with old /downloads path (not /downloads/Music)
        cursor.execute("""
            SELECT COUNT(*) as count FROM download_queue 
            WHERE filepath LIKE '/downloads/%' 
            AND filepath NOT LIKE '/downloads/Music/%'
        """)
        old_count = cursor.fetchone()['count']
        
        if old_count == 0:
            logger.info("✓ No stale download_queue entries found with old path")
            return
        
        logger.info(f"Found {old_count} entries with old /downloads path (not /downloads/Music/)")
        
        # Show samples of what will be deleted
        cursor.execute("""
            SELECT id, filename, filepath FROM download_queue 
            WHERE filepath LIKE '/downloads/%' 
            AND filepath NOT LIKE '/downloads/Music/%'
            LIMIT 5
        """)
        samples = cursor.fetchall()
        logger.info("\nSample entries to be removed:")
        for row in samples:
            logger.info(f"  - {row['filename']} ({row['filepath']})")
        
        # Delete stale entries
        cursor.execute("""
            DELETE FROM download_queue 
            WHERE filepath LIKE '/downloads/%' 
            AND filepath NOT LIKE '/downloads/Music/%'
        """)
        
        deleted = cursor.rowcount
        conn.commit()
        
        logger.info(f"\n✓ Removed {deleted} stale entries from download_queue")
        logger.info("\nNote: The downloads monitor will now rescan with the correct path.")
        logger.info("Next scan will discover files from /downloads/Music/")
        
    except Exception as e:
        logger.error(f"Error cleaning up database: {e}")
        if 'conn' in locals():
            conn.rollback()
    finally:
        if 'conn' in locals():
            conn.close()

if __name__ == "__main__":
    cleanup_old_downloads_paths()
