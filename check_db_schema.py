#!/usr/bin/env python3
import sys
sys.path.insert(0, ".")
from helpers.db_utils import get_db_connection

conn = None
try:
    conn = get_db_connection()
    print("Connected to PostgreSQL")
except Exception as e:
    print(f"Could not connect to PostgreSQL: {e}")
    sys.exit(1)

try:
    cursor = conn.cursor()
    
    # Get the schema of download_queue table
    cursor.execute("""
        SELECT column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = 'download_queue'
        ORDER BY ordinal_position
    """)
    columns = cursor.fetchall()
    
    print("\ndownload_queue table columns:")
    col_names = []
    for row in columns:
        name = row.get("column_name") if isinstance(row, dict) else row[0]
        type_ = row.get("data_type") if isinstance(row, dict) else row[1]
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
        if isinstance(sample, dict):
            for col_name in col_names:
                print(f"  {col_name}: {sample.get(col_name)}")
        else:
            for col_name, value in zip(col_names, sample):
                print(f"  {col_name}: {value}")
    
finally:
    conn.close()
