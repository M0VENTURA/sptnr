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
            print(f"Connected to: {path}")
            break
    except Exception as e:
        print(f"Failed to connect to {path}: {e}")

if not conn:
    print("Could not find database")
    sys.exit(1)

try:
    cursor = conn.cursor()
    
    print("Before cleanup:")
    cursor.execute("SELECT COUNT(*) FROM download_queue")
    print(f"  Total queue items: {cursor.fetchone()[0]}")
    
    cursor.execute("SELECT COUNT(*) FROM download_queue WHERE source = 'test'")
    test_count = cursor.fetchone()[0]
    print(f"  Items with source='test': {test_count}")
    
    # Delete test items
    cursor.execute("DELETE FROM download_queue WHERE source = 'test'")
    conn.commit()
    
    print("\nAfter cleanup:")
    cursor.execute("SELECT COUNT(*) FROM download_queue")
    print(f"  Total queue items: {cursor.fetchone()[0]}")
    
    print(f"\n✓ Removed {test_count} test items from queue")
    
finally:
    conn.close()
