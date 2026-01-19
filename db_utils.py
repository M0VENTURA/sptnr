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
