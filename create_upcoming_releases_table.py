#!/usr/bin/env python3
"""
Migration: Create upcoming releases tracking schema
"""
import sqlite3
import os
from datetime import datetime

def create_upcoming_releases_schema():
    """Create schema for tracking upcoming album releases"""
    # Use the same database path as the app
    db_path = os.environ.get("DB_PATH", "/database/sptnr.db")
    
    # Ensure database directory exists
    db_dir = os.path.dirname(db_path)
    if db_dir and not os.path.exists(db_dir):
        try:
            os.makedirs(db_dir, exist_ok=True)
        except (PermissionError, OSError) as e:
            print(f"Warning: Could not create database directory {db_dir}: {e}")
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    print(f"Running migrations on {os.path.abspath(db_path)}...\n")
    
    # Create upcoming_releases table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS upcoming_releases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            artist_name TEXT NOT NULL,
            album_name TEXT NOT NULL,
            release_date TEXT,
            release_year INTEGER,
            source TEXT,
            artist_in_collection BOOLEAN DEFAULT FALSE,
            album_in_collection BOOLEAN DEFAULT FALSE,
            is_new_release BOOLEAN DEFAULT FALSE,
            notes TEXT,
            url TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(artist_name, album_name)
        )
    """)
    print("✓ Created/verified upcoming_releases table successfully")
    
    # Create indexes for performance
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_upcoming_artist_collection 
        ON upcoming_releases(artist_in_collection, release_date DESC)
    """)
    print("✓ Created index on artist_in_collection and release_date")
    
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_upcoming_release_date 
        ON upcoming_releases(release_date)
    """)
    print("✓ Created index on release_date")
    
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_upcoming_year 
        ON upcoming_releases(release_year)
    """)
    print("✓ Created index on release_year")
    
    # Create release_scrape_history table to track when we last scraped
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS release_scrape_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_url TEXT,
            source_name TEXT,
            items_found INTEGER,
            items_added INTEGER,
            items_updated INTEGER,
            scrape_status TEXT,
            error_message TEXT,
            scrape_start TIMESTAMP,
            scrape_end TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    print("✓ Created/verified release_scrape_history table successfully")
    
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_scrape_history_source 
        ON release_scrape_history(source_name, created_at DESC)
    """)
    print("✓ Created index on scrape_history")
    
    conn.commit()
    conn.close()
    
    print("\n✓ All migrations completed successfully!")

if __name__ == "__main__":
    create_upcoming_releases_schema()
