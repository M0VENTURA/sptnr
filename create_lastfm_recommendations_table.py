#!/usr/bin/env python3
"""
Migration script to create the lastfm_recommendations table for caching
Last.fm recommendations and sync history.
"""

import sqlite3
import os
from datetime import datetime

DATABASE_PATH = os.path.join(os.path.dirname(__file__), 'database.db')

def create_lastfm_recommendations_table():
    """Create the lastfm_recommendations table if it doesn't exist."""
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS lastfm_recommendations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                recommendation_type TEXT NOT NULL,
                item_name TEXT NOT NULL,
                artist_name TEXT,
                image_url TEXT,
                playcount INTEGER DEFAULT 0,
                lastfm_url TEXT,
                mbid TEXT,
                metadata TEXT,
                synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(username, recommendation_type, item_name, artist_name)
            )
        """)
        
        # Create index for faster lookups
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_lastfm_username_type 
            ON lastfm_recommendations(username, recommendation_type)
        """)
        
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_lastfm_synced_at 
            ON lastfm_recommendations(synced_at)
        """)
        
        conn.commit()
        print("✓ Created lastfm_recommendations table successfully")
        return True
        
    except sqlite3.Error as e:
        print(f"✗ Error creating lastfm_recommendations table: {e}")
        return False
    finally:
        conn.close()

def create_lastfm_sync_history_table():
    """Create the lastfm_sync_history table to track sync operations."""
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS lastfm_sync_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                sync_type TEXT NOT NULL,
                artists_count INTEGER DEFAULT 0,
                albums_count INTEGER DEFAULT 0,
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
            CREATE INDEX IF NOT EXISTS idx_sync_history_username_time 
            ON lastfm_sync_history(username, created_at)
        """)
        
        conn.commit()
        print("✓ Created lastfm_sync_history table successfully")
        return True
        
    except sqlite3.Error as e:
        print(f"✗ Error creating lastfm_sync_history table: {e}")
        return False
    finally:
        conn.close()

def create_lastfm_scheduler_config_table():
    """Create the lastfm_scheduler_config table to track scheduler settings."""
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS lastfm_scheduler_config (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                enabled BOOLEAN DEFAULT 1,
                sync_time TEXT DEFAULT '01:00',
                last_sync TIMESTAMP,
                next_sync TIMESTAMP,
                filter_existing BOOLEAN DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        conn.commit()
        print("✓ Created lastfm_scheduler_config table successfully")
        return True
        
    except sqlite3.Error as e:
        print(f"✗ Error creating lastfm_scheduler_config table: {e}")
        return False
    finally:
        conn.close()

def main():
    """Run all migrations."""
    print(f"Running migrations on {DATABASE_PATH}...")
    print()
    
    success = True
    success &= create_lastfm_recommendations_table()
    success &= create_lastfm_sync_history_table()
    success &= create_lastfm_scheduler_config_table()
    
    print()
    if success:
        print("✓ All migrations completed successfully!")
    else:
        print("✗ Some migrations failed. Please check the errors above.")
    
    return success

if __name__ == '__main__':
    exit(0 if main() else 1)
