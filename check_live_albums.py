#!/usr/bin/env python3
import sys
sys.path.insert(0, ".")
from helpers.db_utils import get_db_connection

conn = None
try:
    conn = get_db_connection()
    print("Connected to PostgreSQL")
except Exception as e:
    print(f"Could not connect to PostgreSQL: {e}")
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
        if isinstance(row, dict):
            album = row.get('album')
            album_type = row.get('album_type')
            track_count = row.get('track_count')
        else:
            album, album_type, track_count = row
        print(f"  Album: {album}")
        print(f"    Type: {album_type}, Tracks: {track_count}")
        print()
    
finally:
    conn.close()
