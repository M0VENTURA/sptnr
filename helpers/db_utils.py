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


def ensure_musicbrainz_album_mbid_column():
    """Ensure tracks table uses `musicbrainz_album_mbid` instead of legacy `beets_album_mbid`.

    Migration behavior:
    - If only `beets_album_mbid` exists: rename it to `musicbrainz_album_mbid`.
    - If both exist: backfill missing values in the new column from the legacy column.
    - If neither exists: add `musicbrainz_album_mbid`.
    """
    import logging

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tracks'")
        if not cursor.fetchone():
            logging.warning("Tracks table does not exist yet, skipping MBID column migration")
            conn.close()
            return False

        cursor.execute("PRAGMA table_info(tracks)")
        columns = {row[1] for row in cursor.fetchall()}

        has_legacy = "beets_album_mbid" in columns
        has_new = "musicbrainz_album_mbid" in columns

        if has_legacy and not has_new:
            logging.info("Renaming tracks.beets_album_mbid -> tracks.musicbrainz_album_mbid")
            cursor.execute(
                "ALTER TABLE tracks RENAME COLUMN beets_album_mbid TO musicbrainz_album_mbid"
            )
            conn.commit()
            logging.info("✓ Renamed beets_album_mbid to musicbrainz_album_mbid")
        elif has_legacy and has_new:
            cursor.execute(
                """
                UPDATE tracks
                SET musicbrainz_album_mbid = beets_album_mbid
                WHERE (musicbrainz_album_mbid IS NULL OR musicbrainz_album_mbid = '')
                  AND beets_album_mbid IS NOT NULL
                  AND beets_album_mbid != ''
                """
            )
            conn.commit()
            logging.info("✓ Backfilled musicbrainz_album_mbid from legacy beets_album_mbid")
        elif not has_new:
            cursor.execute("ALTER TABLE tracks ADD COLUMN musicbrainz_album_mbid TEXT")
            conn.commit()
            logging.info("✓ Added missing musicbrainz_album_mbid column")

        conn.close()
        return True
    except Exception as e:
        logging.error(f"Error ensuring musicbrainz_album_mbid column exists: {e}", exc_info=True)
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


def get_current_track_rating(track_id: str) -> int:
    """
    Query the current rating for a track from the database.
    
    Args:
        track_id: Track ID to query
        
    Returns:
        Star rating (0-5), or 0 if not found
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT stars FROM tracks WHERE id = ?", (track_id,))
        row = cursor.fetchone()
        conn.close()
        return int(row[0]) if row else 0
    except Exception as e:
        import logging
        logging.debug(f"Failed to get current rating for track {track_id}: {e}")
        return 0
