#!/usr/bin/env python3
import sqlite3
import os

def check_sync_jobs_schema():
    """Check the current schema for sync jobs"""
    db_path = os.path.join(os.getcwd(), 'database.db')
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Check lastfm_scheduler_config columns
    print("=== lastfm_scheduler_config columns ===")
    cursor.execute("PRAGMA table_info(lastfm_scheduler_config)")
    for row in cursor.fetchall():
        print(f"{row[1]} ({row[2]})")
    
    # Check if sync_jobs_config exists
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sync_jobs_config'")
    if cursor.fetchone():
        print("\n=== sync_jobs_config columns ===")
        cursor.execute("PRAGMA table_info(sync_jobs_config)")
        for row in cursor.fetchall():
            print(f"{row[1]} ({row[2]})")
    
    conn.close()

if __name__ == "__main__":
    check_sync_jobs_schema()
