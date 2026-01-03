#!/usr/bin/env python3
import sqlite3

DB_PATH = '/database/sptnr.db'
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
cursor = conn.cursor()

# Just check if there are any 5-star tracks
cursor.execute('SELECT COUNT(*) FROM tracks WHERE stars = 5')
count = cursor.fetchone()[0]
print(f'Total 5-star tracks in DB: {count}')

# Try a sample artist
cursor.execute('SELECT artist FROM tracks WHERE stars = 5 LIMIT 1')
result = cursor.fetchone()
if result:
    artist = result[0]
    print(f'Sample artist with 5-star tracks: {artist}')
    
    # Now get stats for that artist like the app does
    cursor.execute('''
        SELECT 
            COUNT(*) as track_count,
            COUNT(DISTINCT album) as album_count,
            AVG(stars) as avg_stars,
            SUM(CASE WHEN stars = 5 THEN 1 ELSE 0 END) as five_star_count,
            SUM(COALESCE(duration, 0)) as total_duration,
            MIN(year) as earliest_year,
            MAX(year) as latest_year
        FROM tracks
        WHERE artist = ?
    ''', (artist,))
    stats = cursor.fetchone()
    print(f'Stats type: {type(stats)}')
    print(f'Stats value: {stats}')
    print(f'five_star_count via index [3]: {stats[3]}')
    print(f'five_star_count via attribute: {stats["five_star_count"]}')
else:
    print('No artists found with 5-star tracks')

conn.close()
