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
    
    # Get the schema of download_queue table
    cursor.execute("PRAGMA table_info(download_queue)")
    columns = cursor.fetchall()
    
    print("\ndownload_queue table columns:")
    col_names = []
    for row in columns:
        cid, name, type_, notnull, default_val, pk = row
        col_names.append(name)
        print(f"  {name}: {type_}")
    
    # Check if search_query column exists
    if 'search_query' in col_names:
        print("\n✓ search_query column exists")
    else:
        print("\n✗ search_query column MISSING!")
    
    # Show a sample row to verify structure
    cursor.execute("SELECT * FROM download_queue LIMIT 1")
    sample = cursor.fetchone()
    if sample:
        print("\nSample row structure:")
        for col_name, value in zip(col_names, sample):
            print(f"  {col_name}: {value}")
    
finally:
    conn.close()
