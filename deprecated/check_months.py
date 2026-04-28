#!/usr/bin/env python3
import sqlite3

# Check date distribution across months
db_path = r'C:\database\sptnr.db'
conn = sqlite3.connect(db_path)
cur = conn.cursor()

print("Date distribution by month for 2026 releases from General 2026 Albums:\n")
cur.execute("""
    SELECT 
        substr(release_date, 6, 2) as month,
        COUNT(*) as count,
        MIN(release_date) as first_date,
        MAX(release_date) as last_date
    FROM upcoming_releases 
    WHERE release_year=2026 AND source='General 2026 Albums'
    GROUP BY month
    ORDER BY month
""")

for row in cur.fetchall():
    month_num = int(row[0])
    month_names = {1:'January', 2:'February', 3:'March', 4:'April', 5:'May', 6:'June', 
                   7:'July', 8:'August', 9:'September', 10:'October', 11:'November', 12:'December'}
    month_name = month_names.get(month_num, '?')
    print(f"{month_name:10} ({row[0]}): {row[1]:3} releases | {row[2]} to {row[3]}")

print("\nSample releases by month:")
for month in range(1, 8):  # Check January through July
    cur.execute("""
        SELECT artist_name, album_name, release_date
        FROM upcoming_releases 
        WHERE release_year=2026 AND source='General 2026 Albums' AND substr(release_date, 6, 2)=?
        LIMIT 1
    """, (f"{month:02d}",))
    result = cur.fetchone()
    if result:
        print(f"  {result[0]:20} | {result[1]:40} | {result[2]}")

conn.close()
