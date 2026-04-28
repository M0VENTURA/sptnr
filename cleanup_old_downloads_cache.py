#!/usr/bin/env python3
"""
Cleanup script to remove stale download_queue entries with old downloads paths.
Run this after changing the downloads folder path to clear cached entries.
"""

import os
import logging
from helpers.db_utils import get_db_connection

logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

def cleanup_old_downloads_paths():
    """Remove download_queue entries that point to old /downloads path instead of /downloads/Music or non-existent files"""

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        placeholder = "%s"

        # Check table exists
        cursor.execute("""
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = current_schema() AND table_name = 'download_queue'
            ) AS exists
        """)
        exists_row = cursor.fetchone()
        table_exists = exists_row.get("exists") if isinstance(exists_row, dict) else bool(exists_row[0])
        if not table_exists:
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
