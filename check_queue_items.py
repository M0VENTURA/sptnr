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
    
    # Show breakdown by source and status
    cursor.execute("""
        SELECT source, status, COUNT(*) as count
        FROM download_queue
        GROUP BY source, status
        ORDER BY source, status
    """)
    
    print("\nQueue items by source and status:")
    results = cursor.fetchall()
    for source, status, count in results:
        print(f"  {source}: {status} = {count}")
    
    # Show all queued items  "
    print("\nAll queued items:")
    cursor.execute("SELECT id, artist, title, source, search_query FROM download_queue WHERE status = 'queued'")
    rows = cursor.fetchall()
    if rows:
        for row in rows:
            print(f"  ID={row[0]}, source={row[3]}, artist={row[1]}, title={row[2]}, search_query={row[4]}")
    else:
        print("  (none)")
        
    # Show all soulseek items
    print("\nAll soulseek source items:")
    cursor.execute("SELECT id, artist, title, status, search_query FROM download_queue WHERE source = 'soulseek' ORDER BY created_at DESC LIMIT 10")
    rows = cursor.fetchall()
    if rows:
        for row in rows:
            print(f"  ID={row[0]}, status={row[3]}, artist={row[1]}, title={row[2]}, search_query={row[4]}")
    else:
        print("  (none)")
    
finally:
    conn.close()
