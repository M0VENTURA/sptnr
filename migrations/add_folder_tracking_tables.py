"""
Database migration: Add folder_album_matches and folder_track_matches tables
for tracking download folder organization and completion status.

Run this migration to enable:
- Tracking which folders are matched to which MusicBrainz/Discogs releases
- Showing completion progress (X of Y tracks downloaded)
- Auto-merging duplicate folders matching the same album
- Highlighting matched vs missing tracks in UI
"""

import sqlite3
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / "sptnr.db"


def run_migration():
    """Create folder tracking tables if they don't exist."""
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=120.0)
        cursor = conn.cursor()
        
        # Table: folder_album_matches
        # Tracks which download folders have been matched to specific albums
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS folder_album_matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                folder_path TEXT UNIQUE NOT NULL,
                mb_release_id TEXT NOT NULL,
                mb_source TEXT NOT NULL DEFAULT 'musicbrainz',
                artist TEXT NOT NULL,
                album TEXT NOT NULL,
                release_date TEXT,
                total_expected_tracks INTEGER DEFAULT 0,
                matched_tracks_count INTEGER DEFAULT 0,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        
        # Table: folder_track_matches  
        # Tracks individual files and their relationship to queue items and final destinations
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS folder_track_matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                folder_match_id INTEGER NOT NULL,
                file_path TEXT NOT NULL,
                organized_path TEXT,
                track_number INTEGER,
                track_title TEXT,
                track_artist TEXT,
                queue_item_id INTEGER,
                matched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                organized_at TIMESTAMP,
                FOREIGN KEY (folder_match_id) REFERENCES folder_album_matches(id) ON DELETE CASCADE,
                FOREIGN KEY (queue_item_id) REFERENCES download_queue(id) ON DELETE SET NULL
            );
        """)
        
        # Indexes for performance
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_folder_matches_path 
            ON folder_album_matches(folder_path);
        """)
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_folder_matches_release 
            ON folder_album_matches(mb_release_id, mb_source);
        """)
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_folder_matches_status 
            ON folder_album_matches(status);
        """)
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_track_matches_folder 
            ON folder_track_matches(folder_match_id);
        """)
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_track_matches_queue 
            ON folder_track_matches(queue_item_id);
        """)
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_track_matches_file_path 
            ON folder_track_matches(file_path);
        """)
        
        conn.commit()
        conn.close()
        
        logger.info("✅ Successfully created folder tracking tables")
        print("✅ Migration complete: folder tracking tables created")
        return True
        
    except Exception as e:
        logger.error(f"❌ Migration failed: {e}")
        print(f"❌ Migration failed: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    # Run migration when script is executed directly
    logging.basicConfig(level=logging.INFO)
    run_migration()
