#!/usr/bin/env python3
import sqlite3

# Check the actual data in the database
db_path = r'C:\database\sptnr.db'
conn = sqlite3.connect(db_path)
cur = conn.cursor()

print("Checking first 25 2026 releases from General 2026 Albums source:\n")
cur.execute("""
    SELECT id, artist_name, album_name, release_date, source 
    FROM upcoming_releases 
    WHERE release_year=2026 AND source='General 2026 Albums'
    ORDER BY id
    LIMIT 25
""")

for row in cur.fetchall():
    print(f"{row[0]:3d}: artist='{row[1]:35s}' | album='{row[2]}'")

conn.close()
