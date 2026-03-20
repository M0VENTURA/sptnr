#!/usr/bin/env python3
import sys
sys.path.insert(0, ".")
from datetime import datetime, timedelta
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
