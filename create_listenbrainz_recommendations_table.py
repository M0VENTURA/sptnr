#!/usr/bin/env python3
"""
Migration script to create the listenbrainz_recommendations table for caching
ListenBrainz recommendations and sync history.
"""

import sqlite3
import os
from datetime import datetime

# Use the same database path as the app
DATABASE_PATH = os.environ.get("DB_PATH", "/database/sptnr.db")

def create_listenbrainz_recommendations_table():
    """Create the listenbrainz_recommendations table if it doesn't exist."""
    # Ensure database directory exists
    db_dir = os.path.dirname(DATABASE_PATH)
    if db_dir and not os.path.exists(db_dir):
        try:
            os.makedirs(db_dir, exist_ok=True)
        except (PermissionError, OSError) as e:
            print(f"Warning: Could not create database directory {db_dir}: {e}")
    
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS listenbrainz_recommendations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                recommendation_type TEXT NOT NULL,
                track_name TEXT NOT NULL,
                artist_name TEXT NOT NULL,
                release_name TEXT,
                confidence FLOAT DEFAULT 0.5,
                source TEXT,
                metadata TEXT,
                synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(username, track_name, artist_name, source)
            )
        """)
        
        # Create index for faster lookups
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_listenbrainz_username_type 
            ON listenbrainz_recommendations(username, recommendation_type)
        """)
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_listenbrainz_synced_at 
            ON listenbrainz_recommendations(synced_at)
        """)
        
        conn.commit()
        print("✓ Created listenbrainz_recommendations table successfully")
        return True
        
    except sqlite3.Error as e:
        print(f"✗ Error creating listenbrainz_recommendations table: {e}")
        return False
    finally:
        conn.close()

def create_listenbrainz_sync_history_table():
    """Create the listenbrainz_sync_history table to track sync operations."""
    # Ensure database directory exists
    db_dir = os.path.dirname(DATABASE_PATH)
    if db_dir and not os.path.exists(db_dir):
        try:
            os.makedirs(db_dir, exist_ok=True)
        except (PermissionError, OSError) as e:
            print(f"Warning: Could not create database directory {db_dir}: {e}")
    
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS listenbrainz_sync_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                sync_type TEXT NOT NULL,
                source TEXT,
                tracks_count INTEGER DEFAULT 0,
                filtered_count INTEGER DEFAULT 0,
                sync_status TEXT DEFAULT 'success',
                error_message TEXT,
                sync_start TIMESTAMP,
                sync_end TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Create index for faster lookups
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_lb_sync_history_username_time 
            ON listenbrainz_sync_history(username, created_at)
        """)
        
        conn.commit()
        print("✓ Created listenbrainz_sync_history table successfully")
        return True
        
    except sqlite3.Error as e:
        print(f"✗ Error creating listenbrainz_sync_history table: {e}")
        return False
    finally:
        conn.close()

def create_listenbrainz_config_table():
    """Create the listenbrainz_config table to store user configuration."""
    # Ensure database directory exists
    db_dir = os.path.dirname(DATABASE_PATH)
    if db_dir and not os.path.exists(db_dir):
        try:
            os.makedirs(db_dir, exist_ok=True)
        except (PermissionError, OSError) as e:
            print(f"Warning: Could not create database directory {db_dir}: {e}")
    
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS listenbrainz_config (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                listenbrainz_username TEXT,
                user_token TEXT,
                enabled BOOLEAN DEFAULT 1,
                sync_enabled BOOLEAN DEFAULT 1,
                recommendation_type TEXT DEFAULT 'weekly-exploration',
                last_sync TIMESTAMP,
                filter_existing BOOLEAN DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        conn.commit()
        print("✓ Created listenbrainz_config table successfully")
        return True
        
    except sqlite3.Error as e:
        print(f"✗ Error creating listenbrainz_config table: {e}")
        return False
    finally:
        conn.close()

def main():
    """Run all migrations."""
    print(f"Running ListenBrainz migrations on {DATABASE_PATH}...")
    print()
    
    success = True
    success &= create_listenbrainz_recommendations_table()
    success &= create_listenbrainz_sync_history_table()
    success &= create_listenbrainz_config_table()
    
    print()
    if success:
        print("✓ All ListenBrainz migrations completed successfully!")
    else:
        print("✗ Some migrations failed. Please check the errors above.")
    
    return success

if __name__ == '__main__':
    exit(0 if main() else 1)
