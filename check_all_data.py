#!/usr/bin/env python3
import sqlite3

db_path = 'database/sptnr.db'
conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# Check artists
print("=== All artists in database ===")
cursor.execute("SELECT COUNT(DISTINCT artist) FROM tracks")
artist_count = cursor.fetchone()[0]
print(f"Total unique artists: {artist_count}")

cursor.execute("SELECT DISTINCT artist FROM tracks ORDER BY artist LIMIT 20")
artists = cursor.fetchall()
print("\nFirst 20 artists:")
for (artist,) in artists:
    cursor.execute("SELECT COUNT(*) FROM tracks WHERE artist = ?", (artist,))
    count = cursor.fetchone()[0]
    print(f"  {artist}: {count} tracks")

# Check albums
print("\n=== Album count ===")
cursor.execute("SELECT COUNT(*) FROM albums")
album_count = cursor.fetchone()[0]
print(f"Total albums: {album_count}")

# Check total tracks
cursor.execute("SELECT COUNT(*) FROM tracks")
total_tracks = cursor.fetchone()[0]
print(f"Total tracks: {total_tracks}")

conn.close()
