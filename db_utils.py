import sqlite3
import os

DB_PATH = os.environ.get("DB_PATH", "/database/sptnr.db")

def get_db_connection():
    # Ensure database directory exists
    db_dir = os.path.dirname(DB_PATH)
    if db_dir and not os.path.exists(db_dir):
        try:
            os.makedirs(db_dir, exist_ok=True)
        except (PermissionError, OSError) as e:
            # If we can't create the directory, the sqlite3.connect will fail with a more specific error
            # Log the warning but let the connection attempt proceed
            import logging
            logging.warning(f"Could not create database directory {db_dir}: {e}")
    
    conn = sqlite3.connect(DB_PATH, timeout=120.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def ensure_album_artist_column():
    """Ensure the album_artist column exists in the tracks table. 
    
    This is called on app startup to automatically migrate the database
    if needed, without requiring manual intervention.
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Check if tracks table exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tracks'")
        if not cursor.fetchone():
            # Table doesn't exist yet, nothing to migrate
            conn.close()
            return False
        
        # Check if album_artist column exists
        cursor.execute("PRAGMA table_info(tracks)")
        columns = {row[1] for row in cursor.fetchall()}
        
        if 'album_artist' in columns:
            # Column already exists
            conn.close()
            return True
        
        # Add the album_artist column
        try:
            cursor.execute("ALTER TABLE tracks ADD COLUMN album_artist TEXT")
            conn.commit()
            import logging
            logging.info("Successfully added album_artist column to tracks table")
            conn.close()
            return True
        except sqlite3.OperationalError as e:
            if "duplicate column name" in str(e).lower():
                # Column was added by another process
                conn.close()
                return True
            raise
    except Exception as e:
        import logging
        logging.error(f"Error ensuring album_artist column exists: {e}")
        return False
