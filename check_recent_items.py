#!/usr/bin/env python3
import sys
sys.path.insert(0, ".")
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta

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
            print(f"Connected to: {path}")
            break
    except Exception as e:
        print(f"Failed to connect to {path}: {e}")

if not conn:
    print("Could not find database")
    sys.exit(1)

try:
    cursor = conn.cursor()
    
    # Get all items ordered by created_at
    cursor.execute("""
        SELECT id, artist, title, source, status, created_at, search_query 
        FROM download_queue 
        ORDER BY created_at DESC 
        LIMIT 20
    """)
    
    print("\nMost recent queue items:")
    rows = cursor.fetchall()
    for row in rows:
        id_, artist, title, source, status, created_at, search_query = row
        print(f"  ID={id_}, source={source}, status={status}, created_at={created_at}")
        print(f"    {artist} - {title}")
        if not search_query:
            print(f"    WARNING: No search_query!")
        print()
    
finally:
    conn.close()
