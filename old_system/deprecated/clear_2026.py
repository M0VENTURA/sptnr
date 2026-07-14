#!/usr/bin/env python3
import sqlite3

db_path = r'C:\database\sptnr.db'
conn = sqlite3.connect(db_path)
cur = conn.cursor()

# Delete all 2026 releases
cur.execute("DELETE FROM upcoming_releases WHERE release_year=2026;")
rows_deleted = cur.rowcount
conn.commit()

# Check what's left
cur.execute("SELECT COUNT(*) FROM upcoming_releases;")
count = cur.fetchone()[0]
print(f"Deleted {rows_deleted} rows")
print(f"Remaining upcoming_releases: {count}")

conn.close()
