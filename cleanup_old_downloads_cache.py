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

def _is_postgres_connection(conn):
    """Detect if connection is PostgreSQL."""
    try:
        import psycopg2
        underlying = getattr(conn, "_conn", conn)
        return isinstance(underlying, psycopg2.extensions.connection)
    except (ImportError, AttributeError):
        return False

def cleanup_old_downloads_paths():
    """Remove download_queue entries that point to old /downloads path instead of /downloads/Music or non-existent files"""
    
    db_path = "sptnr.db"
    if not os.path.exists(db_path):
        logger.error(f"Database not found at {db_path}")
        return
    
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Detect database type
        is_pg = _is_postgres_connection(conn)
        placeholder = "%s" if is_pg else "?"
        
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
        
        # Count entries where files don't exist
        cursor.execute("SELECT id, filename, filepath FROM download_queue")
        all_entries = cursor.fetchall()
        
        nonexistent_ids = []
        for row in all_entries:
            filepath = row['filepath']
            if filepath and not os.path.exists(filepath):
                nonexistent_ids.append(row['id'])
        
        nonexistent_count = len(nonexistent_ids)
        
        if old_count == 0 and nonexistent_count == 0:
            logger.info("✓ No stale download_queue entries found")
            return
        
        if old_count > 0:
            logger.info(f"Found {old_count} entries with old /downloads path (not /downloads/Music/)")
            
            # Show samples of what will be deleted
            cursor.execute("""
                SELECT id, filename, filepath FROM download_queue 
                WHERE filepath LIKE '/downloads/%' 
                AND filepath NOT LIKE '/downloads/Music/%'
                LIMIT 5
            """)
            samples = cursor.fetchall()
            logger.info("\nSample entries with old paths:")
            for row in samples:
                logger.info(f"  - {row['filename']} ({row['filepath']})")
            
            # Delete entries with old paths
            cursor.execute("""
                DELETE FROM download_queue 
                WHERE filepath LIKE '/downloads/%' 
                AND filepath NOT LIKE '/downloads/Music/%'
            """)
            
            deleted_old = cursor.rowcount
            logger.info(f"✓ Removed {deleted_old} entries with old paths")
        
        if nonexistent_count > 0:
            logger.info(f"\nFound {nonexistent_count} entries where files no longer exist")
            
            # Show samples
            logger.info("\nSample entries with missing files:")
            for i, entry_id in enumerate(nonexistent_ids[:5]):
                cursor.execute(f"SELECT filename, filepath FROM download_queue WHERE id = {placeholder}", (entry_id,))
                row = cursor.fetchone()
                if row:
                    logger.info(f"  - {row['filename']} ({row['filepath']})")
            
            # Delete non-existent entries
            if nonexistent_ids:
                placeholders = ','.join([placeholder] * len(nonexistent_ids))
                cursor.execute(f"DELETE FROM download_queue WHERE id IN ({placeholders})", nonexistent_ids)
                deleted_nonexistent = cursor.rowcount
                logger.info(f"✓ Removed {deleted_nonexistent} entries with non-existent files")
        
        conn.commit()
        
        logger.info("\n✓ Cleanup complete!")
        logger.info("The downloads monitor will now show only valid files.")
        
    except Exception as e:
        logger.error(f"Error cleaning up database: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()

if __name__ == "__main__":
    cleanup_old_downloads_paths()
