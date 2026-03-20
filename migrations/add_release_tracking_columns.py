#!/usr/bin/env python3
"""
Migration: Add MusicBrainz release tracking columns to download_queue table
This enables grouping of album tracks in the Download Monitor UI
PostgreSQL-only migration
"""
import os
import sys

DB_PATH = os.environ.get("DB_PATH", "/database/sptnr.db")

def migrate():
    """Add release_id, release_source, track_number columns to download_queue"""
    try:
        from app import get_db
        conn = get_db()
        
        cursor = conn.cursor()
        
        # Check if columns already exist
        cursor.execute("""
            SELECT column_name FROM information_schema.columns 
            WHERE table_name = 'download_queue'
              AND table_schema = 'public'
        """)
        columns = [row[0] for row in cursor.fetchall()]
        
        added_columns = []
        
        # Add release_id column if not exists
        if 'release_id' not in columns:
            print("Adding release_id column...")
            cursor.execute("ALTER TABLE download_queue ADD COLUMN release_id TEXT")
            added_columns.append('release_id')
        
        # Add release_source column if not exists  
        if 'release_source' not in columns:
            print("Adding release_source column...")
            cursor.execute("ALTER TABLE download_queue ADD COLUMN release_source TEXT")
            added_columns.append('release_source')
        
        # Add track_number column if not exists
        if 'track_number' not in columns:
            print("Adding track_number column...")
            cursor.execute("ALTER TABLE download_queue ADD COLUMN track_number INTEGER")
            added_columns.append('track_number')
        
        # Add album_artist column if not exists (also needed)
        if 'album_artist' not in columns:
            print("Adding album_artist column...")
            cursor.execute("ALTER TABLE download_queue ADD COLUMN album_artist TEXT")
            added_columns.append('album_artist')
        
        # Add year column if not exists
        if 'year' not in columns:
            print("Adding year column...")
            cursor.execute("ALTER TABLE download_queue ADD COLUMN year TEXT")
            added_columns.append('year')
        
        conn.commit()
        conn.close()
        
        if added_columns:
            print(f"✅ Migration complete! Added columns: {', '.join(added_columns)}")
        else:
            print("✅ All columns already exist, no migration needed")
        
        return True
        
    except Exception as e:
        print(f"❌ Migration failed: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == '__main__':
    print(f"Migrating database...")
    success = migrate()
    sys.exit(0 if success else 1)
