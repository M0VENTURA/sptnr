#!/usr/bin/env python3
import sys
sys.path.insert(0, ".")
import sqlite3
from pathlib import Path

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
            conn.row_factory = sqlite3.Row
            print(f"Connected to: {path}")
            break
    except Exception as e:
        print(f"Failed to connect to {path}: {e}")

if not conn:
    print("Could not find database")
    sys.exit(1)

try:
    cursor = conn.cursor()
    
    # Check for Soulseek queue items
    cursor.execute("SELECT id, artist, title, source, status, search_query FROM download_queue WHERE source = 'soulseek' ORDER BY created_at DESC LIMIT 5")
    rows = cursor.fetchall()
    if rows:
        print(f"\nFound {len(rows)} Soulseek queue items:")
        for row in rows:
            print(f"  ID={row[0]}, artist={row[1]}, title={row[2]}, source={row[3]}, status={row[4]}, search_query={row[5]}")
    else:
        print("\nNo Soulseek queue items found")
    
    # Count all queued items
    cursor.execute("SELECT COUNT(*) FROM download_queue WHERE status = 'queued'")
    count = cursor.fetchone()[0]
    print(f"\nTotal queued items (all sources): {count}")
    
    # Show queued items by source
    cursor.execute("SELECT source, COUNT(*) FROM download_queue WHERE status = 'queued' GROUP BY source")
    sources = cursor.fetchall()
    print("\nQueued items by source:")
    for source, cnt in sources:
        print(f"  {source}: {cnt}")
    
finally:
    conn.close()
