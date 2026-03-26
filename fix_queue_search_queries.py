#!/usr/bin/env python3
"""
Fix search_query format for existing download_queue items.
Changes from old format to new 'artist - title' format.
"""

import os
import sys

def fix_queue_search_queries():
    """Update search_query for all queued items to use 'artist - title' format"""
    try:
        # Import app's database connection (supports PostgreSQL)
        from app import get_db, _is_postgres_connection
        
        conn = get_db()
        cursor = conn.cursor()
        placeholder = "%s"
        
        # Get all queue items
        cursor.execute("""
            SELECT id, artist, title, album, search_query, status 
            FROM download_queue
            WHERE status IN ('queued', 'searching')
        """)
        
        items = cursor.fetchall()
        
        if not items:
            print("No queued/searching items found")
            conn.close()
            return
        
        print(f"Found {len(items)} items to check...")
        print()
        
        updated = 0
        for item in items:
            # Handle both dict and tuple row formats
            if isinstance(item, dict):
                item_id = item['id']
                artist = item['artist'] or ''
                title = item['title'] or ''
                old_query = item['search_query'] or ''
            else:
                item_id = item[0]
                artist = item[1] or ''
                title = item[2] or ''
                old_query = item[4] or ''
            
            # New format: "artist - title" (no album)
            new_query = f"{artist} - {title}".strip()
            
            if old_query != new_query:
                print(f"Queue {item_id}:")
                print(f"  OLD: {old_query}")
                print(f"  NEW: {new_query}")
                print()
                
                cursor.execute(f"""
                    UPDATE download_queue 
                    SET search_query = {placeholder}, updated_at = CURRENT_TIMESTAMP
                    WHERE id = {placeholder}
                """, (new_query, item_id))
                
                updated += 1
        
        if updated > 0:
            conn.commit()
            print(f"✓ Updated {updated} search queries")
        else:
            print("✓ All search queries already correct")
        
        conn.close()
        
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    print("=== Fixing Download Queue Search Queries ===")
    print()
    fix_queue_search_queries()
