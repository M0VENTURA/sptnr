#!/usr/bin/env python3
import sys
sys.path.insert(0, ".")
import sqlite3
from pathlib import Path

# Try to find the database
db_paths = [
    "/database/sptnr.db",
    "./database/sptnr.db",
    str(Path.home() / "AppData/Local/sptnr/data/sptnr.db"),
]

conn = None
for path in db_paths:
    try:
        if Path(path).exists():
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            print(f"Connected to: {path}")
            break
    except Exception as e:
        print(f"Failed to connect to {path}: {e}")

if not conn:
    print("Could not find database")
    sys.exit(1)

try:
    cursor = conn.cursor()
    
    # Find any albums with "live" in the name
    cursor.execute("""
        SELECT DISTINCT album, MAX(spotify_album_type) as album_type, 
               COUNT(*) as track_count
        FROM tracks 
        WHERE album LIKE '%live%' OR album LIKE '%unplugged%' OR album LIKE '%Live%' OR album LIKE '%Unplugged%'
        GROUP BY album
        ORDER BY album
    """)
    
    print("Albums with 'live' or 'unplugged' in the name:")
    rows = cursor.fetchall()
    for row in rows:
        album = row['album']
        album_type = row['album_type']
        track_count = row['track_count']
        print(f"  Album: {album}")
        print(f"    Type: {album_type}, Tracks: {track_count}")
        print()
    
finally:
    conn.close()
