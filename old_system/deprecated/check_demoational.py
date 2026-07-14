#!/usr/bin/env python3
import sqlite3

db_path = 'database/sptnr.db'
try:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Search for deMotional
    cursor.execute("""
        SELECT DISTINCT artist, album, COUNT(*) as track_count 
        FROM tracks 
        WHERE artist LIKE '%deMotional%' OR artist LIKE '%de Motional%'
        GROUP BY artist, album
    """)
    
    results = cursor.fetchall()
    if results:
        print(f'Found {len(results)} album(s) for deMotional:\n')
        for artist, album, count in results:
            print(f'Artist: {artist}')
            print(f'Album: {album}')
            print(f'Track count: {count}')
            # Get more details
            cursor.execute("""
                SELECT title, is_single, popularity, single_confidence, duration
                FROM tracks 
                WHERE artist = ?
                AND album = ?
                ORDER BY title
            """, (artist, album))
            tracks = cursor.fetchall()
            for title, is_single, popularity, confidence, duration in tracks:
                status = "SINGLE" if is_single else "ALBUM TRACK"
                print(f'  - {title}')
                print(f'    Type: {status} | Popularity: {popularity} | Confidence: {confidence} | Duration: {duration}s')
            print()
    else:
        print('No deMotional artist found in database')
        # Try wider search
        cursor.execute("SELECT DISTINCT artist FROM tracks WHERE artist LIKE '%Emotion%' LIMIT 10")
        others = cursor.fetchall()
        if others:
            print("Similar artists found:")
            for row in others:
                print(f"  - {row[0]}")
    
    conn.close()
except Exception as e:
    print(f'Error: {e}')
    import traceback
    traceback.print_exc()
