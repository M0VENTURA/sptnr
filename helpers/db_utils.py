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
    """Ensure the album_artist column exists in the tracks table AND populate it with artist data.
    
    This is called on app startup to automatically migrate the database
    if needed, without requiring manual intervention.
    """
    import logging
    import sys
    
    try:
        db_path = os.environ.get("DB_PATH", "/database/sptnr.db")
        logging.info(f"Checking album_artist migration for database: {db_path}")
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Check if tracks table exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tracks'")
        if not cursor.fetchone():
            # Table doesn't exist yet, nothing to migrate
            logging.warning("Tracks table does not exist yet, skipping album_artist migration")
            conn.close()
            return False
        
        # Check if album_artist column exists
        cursor.execute("PRAGMA table_info(tracks)")
        columns = {row[1] for row in cursor.fetchall()}
        
        if 'album_artist' not in columns:
            # Add the album_artist column
            logging.info("Creating album_artist column...")
            try:
                cursor.execute("ALTER TABLE tracks ADD COLUMN album_artist TEXT")
                conn.commit()
                logging.info("✓ Successfully added album_artist column to tracks table")
            except sqlite3.OperationalError as e:
                if "duplicate column name" not in str(e).lower():
                    logging.error(f"✗ Failed to add album_artist column: {e}")
                    conn.close()
                    raise
        
        # Populate album_artist with artist data where it's NULL
        logging.info("Populating album_artist column from artist data...")
        try:
            cursor.execute("UPDATE tracks SET album_artist = artist WHERE album_artist IS NULL")
            rows_updated = cursor.rowcount
            conn.commit()
            logging.info(f"✓ Populated album_artist for {rows_updated} rows")
        except Exception as e:
            logging.error(f"✗ Failed to populate album_artist column: {e}")
            conn.close()
            raise
        
        logging.info("✓ album_artist migration complete")
        conn.close()
        return True
        
    except Exception as e:
        logging.error(f"✗ Error ensuring album_artist column exists: {e}", exc_info=True)
        # Don't fail app startup, but log the error
        return False


def verify_album_artist_column():
    """Verify that the album_artist column exists and is functional.
    
    Returns:
        dict: Status information with 'exists' boolean and 'message' string
    """
    import logging
    
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Check if tracks table exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tracks'")
        if not cursor.fetchone():
            return {"exists": False, "message": "Tracks table does not exist"}
        
        # Check if album_artist column exists
        cursor.execute("PRAGMA table_info(tracks)")
        columns = {row[1] for row in cursor.fetchall()}
        
        conn.close()
        
        if 'album_artist' in columns:
            return {"exists": True, "message": "album_artist column exists and is functional"}
        else:
            return {"exists": False, "message": "album_artist column does NOT exist - migration failed or not run"}
    except Exception as e:
        return {"exists": False, "message": f"Error verifying column: {e}"}
