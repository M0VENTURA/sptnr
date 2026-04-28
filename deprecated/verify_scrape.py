#!/usr/bin/env python3
import sqlite3

db_path = r'C:\database\sptnr.db'
conn = sqlite3.connect(db_path)
cur = conn.cursor()

print('--- Table Schema for upcoming_releases ---')
cur.execute("PRAGMA table_info(upcoming_releases);")
columns = cur.fetchall()
for col in columns:
    print(f'  {col[1]}: {col[2]}')

print('\n--- 2026 Releases ---')
cur.execute("SELECT COUNT(*) FROM upcoming_releases WHERE release_year=2026;")
count_2026 = cur.fetchone()[0]
print(f'Total: {count_2026}')

cur.execute("SELECT source, COUNT(*) FROM upcoming_releases WHERE release_year=2026 GROUP BY source;")
print('\nBy source:')
for source, count in cur.fetchall():
    print(f'  {source}: {count}')

print('\nSample from 2026:')
cur.execute("""
    SELECT artist_name, album_name, release_date, source 
    FROM upcoming_releases 
    WHERE release_year=2026
    ORDER BY release_date
    LIMIT 10
""")
for row in cur.fetchall():
    print(f'  {row}')

conn.close()
