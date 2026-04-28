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
