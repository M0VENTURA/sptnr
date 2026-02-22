#!/usr/bin/env python3
"""
Debug script to check if Last.fm tags are being saved to the database
and verify the complete data flow from fetch -> storage -> API -> frontend
"""

import sqlite3
import json
from config_loader import load_config
import sys

DB_TIMEOUT = 120.0

def get_db():
    """Get database connection"""
    config = load_config()
    db_path = config.get('navidrome', {}).get('database_path', './navidrome.db')
    conn = sqlite3.connect(db_path, timeout=DB_TIMEOUT)
    conn.row_factory = sqlite3.Row
    return conn

def check_lastfm_tags():
    """Check what's in the database for Last.fm tags"""
    conn = get_db()
    cursor = conn.cursor()
    
    print("=" * 80)
    print("LAST.FM TAGS DEBUG CHECK")
    print("=" * 80)
    
    # Count tracks with lastfm_tags populated
    cursor.execute("SELECT COUNT(*) as count FROM tracks WHERE lastfm_tags IS NOT NULL AND lastfm_tags != ''")
    count = cursor.fetchone()['count']
    print(f"\n✓ Tracks with lastfm_tags populated: {count}")
    
    # Sample a few tracks with tags
    cursor.execute("""
        SELECT id, artist, album, title, lastfm_tags 
        FROM tracks 
        WHERE lastfm_tags IS NOT NULL AND lastfm_tags != ''
        LIMIT 5
    """)
    rows = cursor.fetchall()
    
    if rows:
        print(f"\nSample tracks with Last.fm tags:")
        for row in rows:
            print(f"\n  Track: {row['title']} by {row['artist']}")
            print(f"  Album: {row['album']}")
            try:
                tags = json.loads(row['lastfm_tags'])
                print(f"  Tags (parsed): {tags}")
                print(f"  Tag count: {len(tags) if isinstance(tags, list) else 'Not a list'}")
            except json.JSONDecodeError:
                print(f"  Tags (raw): {row['lastfm_tags']}")
                print(f"  ⚠ WARNING: lastfm_tags is not valid JSON!")
    else:
        print("\n⚠ WARNING: No tracks with lastfm_tags found in database!")
    
    # Check if batch fetch is working by looking at stars and timestamps
    cursor.execute("""
        SELECT COUNT(*) as count FROM tracks 
        WHERE stars IS NOT NULL AND stars > 0 AND last_scanned IS NOT NULL
    """)
    scanned_count = cursor.fetchone()['count']
    print(f"\n✓ Tracks that have been popularity scanned: {scanned_count}")
    
    # Check for any errors in the track data
    cursor.execute("""
        SELECT 
            COUNT(CASE WHEN lastfm_tags IS NULL OR lastfm_tags = '' THEN 1 END) as no_tags,
            COUNT(CASE WHEN lastfm_tags IS NOT NULL AND lastfm_tags != '' THEN 1 END) as has_tags
        FROM tracks
    """)
    stats = cursor.fetchone()
    print(f"\n  Total tracks: {stats['no_tags'] + stats['has_tags']}")
    print(f"  - With Last.fm tags: {stats['has_tags']}")
    print(f"  - Without Last.fm tags: {stats['no_tags']}")
    
    # Check genre_tag_aggregator table if it exists
    try:
        cursor.execute("SELECT COUNT(*) as count FROM genre_tag_aggregator")
        results = cursor.fetchone()
        print(f"\n✓ Tracks in genre_tag_aggregator cache: {results['count']}")
    except sqlite3.OperationalError:
        print(f"\n✓ genre_tag_aggregator table doesn't exist yet (will be created on first use)")
    
    # Check artist_stats to ensure it exists
    try:
        cursor.execute("""
            SELECT COUNT(*) as count FROM artist_stats
            WHERE mean_popularity IS NOT NULL
        """)
        count = cursor.fetchone()['count']
        print(f"✓ Artists with computed mean_popularity: {count}")
    except sqlite3.OperationalError:
        print(f"✓ artist_stats table not created yet (normal if no artist context applied)")
    
    conn.close()
    
    print("\n" + "=" * 80)
    print("RECOMMENDATION:")
    print("=" * 80)
    
    if count == 0:
        print("""
If no Last.fm tags are found, the issue could be:

1. **Batch fetch not running**: The tag batch fetch happens during popularity scan
   - Run a popularity scan to fetch tags: python popularity.py <artist> <album> ...
   
2. **API client not initialized**: Check Last.fm config in navidrome.yml
   - Ensure 'api_integrations.last_fm.enabled = true'
   - Ensure 'api_integrations.last_fm.api_key' is set
   
3. **Rate limiting**: Last.fm API might be rate limited
   - Check logs for rate limiting messages
   
4. **API failures**: Last.fm service might be down or unreachable
   - Check logs for "Failed to fetch Last.fm tags" messages
   
Next step: Run a test popularity scan and check the logs for tag fetching messages.
        """)
    else:
        print(f"""
Good! Last.fm tags ARE being saved to the database ({count} tracks).

The issue is likely in the API endpoint or frontend:

1. **Check API endpoint**: Test /api/genres/track/<track_id> directly
   - Should return JSON with lastfm_tags array
   
2. **Check frontend**: Open browser dev tools (F12)
   - Check Network tab when album page loads
   - Look for /api/genres/track/ request
   - Verify response contains lastfm_tags
   
3. **Check UI code**: album.html expects data.genres.lastfm_tags array
   - Verify browser console for JavaScript errors
   
Next step: Check the API endpoint response directly.
        """)

if __name__ == "__main__":
    try:
        check_lastfm_tags()
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
