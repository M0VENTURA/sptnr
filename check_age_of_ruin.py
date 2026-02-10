#!/usr/bin/env python3
import sqlite3
import os

db_path = 'database/music.db'
if not os.path.exists(db_path):
    print(f"Database not found at {db_path}")
    exit(1)

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# Get all tables
print("=== Tables in database ===")
cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = cursor.fetchall()
for table in tables:
    print(f"  - {table[0]}")

# If tracks table exists, query it
cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tracks'")
if cursor.fetchone():
    print("\n=== Age Of Ruin albums in tracks table ===")
    cursor.execute('''
        SELECT DISTINCT album, COUNT(*) as track_count 
        FROM tracks 
        WHERE artist LIKE '%Age%Ruin%'
        GROUP BY album
        ORDER BY album
    ''')
    albums = cursor.fetchall()
    if albums:
        for album, count in albums:
            print(f"  {album}: {count} tracks")
    else:
        print("  No Age of Ruin albums found")
    
    # Check total
    cursor.execute('SELECT COUNT(*) FROM tracks WHERE artist LIKE "%Age%Ruin%"')
    total = cursor.fetchone()[0]
    print(f"\nTotal Age of Ruin tracks in database: {total}")

conn.close()
