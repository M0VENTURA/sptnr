#!/usr/bin/env python3
"""
Find which database contains Creed tracks.
"""

import sqlite3
import os

db_files = [
    "app.db",
    "database.db", 
    "library.db",
    "music.db",
    "navidrome.db"
]

found_creed = False

for db_file in db_files:
    if not os.path.exists(db_file):
        continue
    
    try:
        conn = sqlite3.connect(db_file)
        cursor = conn.cursor()
        
        # Check if tracks table exists
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tracks'")
        if not cursor.fetchone():
            print(f"❌ {db_file}: No 'tracks' table")
            conn.close()
            continue
        
        # Check for Creed tracks
        cursor.execute("SELECT COUNT(*) FROM tracks WHERE LOWER(artist) LIKE '%creed%'")
        count = cursor.fetchone()[0]
        
        if count > 0:
            print(f"✅ {db_file}: Found {count} Creed tracks!")
            found_creed = True
            
            # Get sample
            cursor.execute("SELECT title, album FROM tracks WHERE LOWER(artist) LIKE '%creed%' LIMIT 5")
            samples = cursor.fetchall()
            for title, album in samples:
                print(f"   - {title} ({album})")
        else:
            # Get total track count
            cursor.execute("SELECT COUNT(*) FROM tracks")
            total = cursor.fetchone()[0]
            print(f"⚠️  {db_file}: No Creed tracks (total tracks: {total})")
        
        conn.close()
    except Exception as e:
        print(f"❌ {db_file}: Error - {e}")

if not found_creed:
    print("\n❌ No Creed tracks found in any database!")
