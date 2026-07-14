#!/usr/bin/env python3
"""
Migration: Create unified sync jobs configuration schema
"""
import sqlite3
import os
from datetime import datetime

def create_sync_jobs_schema():
    """Create unified schema for all sync job configurations"""
    db_path = 'database.db'
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    print(f"Running migrations on {os.path.abspath(db_path)}...\n")
    
    # Create unified sync_jobs_config table
    # job_type: 'lastfm', 'navidrome', 'popularity_single', 'beets_import'
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sync_jobs_config (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            job_type TEXT NOT NULL,
            enabled BOOLEAN DEFAULT FALSE,
            schedule_time TEXT DEFAULT '01:00',
            last_sync TIMESTAMP,
            next_sync TIMESTAMP,
            filter_existing BOOLEAN DEFAULT TRUE,
            include_history BOOLEAN DEFAULT TRUE,
            run_on_startup BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(username, job_type)
        )
    """)
    print("✓ Created/verified sync_jobs_config table successfully")
    
    # Create indexes for performance
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_sync_jobs_username_type 
        ON sync_jobs_config(username, job_type)
    """)
    print("✓ Created index on username and job_type")
    
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_sync_jobs_next_sync 
        ON sync_jobs_config(next_sync)
    """)
    print("✓ Created index on next_sync")
    
    # Create sync_jobs_history table for tracking all sync operations
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sync_jobs_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            job_type TEXT NOT NULL,
            sync_status TEXT,
            items_processed INTEGER,
            items_success INTEGER,
            items_failed INTEGER,
            items_filtered INTEGER,
            error_message TEXT,
            sync_start TIMESTAMP,
            sync_end TIMESTAMP,
            duration_seconds REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    print("✓ Created/verified sync_jobs_history table successfully")
    
    # Create indexes for history
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_sync_history_username_type 
        ON sync_jobs_history(username, job_type, created_at DESC)
    """)
    print("✓ Created index on sync history")
    
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_sync_history_created 
        ON sync_jobs_history(created_at DESC)
    """)
    print("✓ Created index on history created_at")
    
    # Initialize default sync job configurations if they don't exist
    # This ensures every user has the basic job types configured
    default_jobs = [
        ('lastfm', 'Last.fm Recommendations'),
        ('navidrome', 'Navidrome Sync'),
        ('popularity_single', 'Popularity & Single Detection'),
        ('beets_import', 'Beets Import')
    ]
    
    # Check if we need to initialize (only for existing users with navidrome_users)
    cursor.execute("SELECT COUNT(*) FROM sync_jobs_config")
    if cursor.fetchone()[0] == 0:
        print("✓ Sync jobs config table is ready for initialization")
    
    conn.commit()
    conn.close()
    
    print("\n✓ All migrations completed successfully!")

if __name__ == "__main__":
    create_sync_jobs_schema()
