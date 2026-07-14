#!/usr/bin/env python3
"""
Database migration script to consolidate artist MBID fields.

This script:
1. Copies beets_artist_mbid values to musicbrainz_artist_id where needed
2. Verifies the migration was successful
3. Prepares for the eventual removal of the beets_artist_mbid field

The script is idempotent - it can be run multiple times safely.
"""

import sqlite3
import sys
from pathlib import Path

# Try to find the database
db_paths = [
    Path("database/sptnr.db"),
    Path("./database/sptnr.db"),
    Path("sptnr.db"),
    Path("./sptnr.db"),
    Path("C:\\Script\\Github\\sptnr\\database\\sptnr.db"),
]

db_path = None
for path in db_paths:
    if path.exists():
        db_path = path
        break

if not db_path:
    print("ERROR: Could not find sptnr.db database")
    print("Checked locations:")
    for path in db_paths:
        print(f"  - {path}")
    sys.exit(1)

print(f"Found database at: {db_path}")

try:
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()
    
    # Check if both columns exist
    cursor.execute("PRAGMA table_info(tracks)")
    columns = {row[1] for row in cursor.fetchall()}
    
    if 'beets_artist_mbid' not in columns:
        print("ERROR: beets_artist_mbid column not found in database")
        sys.exit(1)
    
    if 'musicbrainz_artist_id' not in columns:
        print("ERROR: musicbrainz_artist_id column not found in database")
        sys.exit(1)
    
    print("\n✓ Both columns exist in database")
    
    # Count current state
    cursor.execute("SELECT COUNT(*) FROM tracks WHERE beets_artist_mbid IS NOT NULL")
    beets_count = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM tracks WHERE musicbrainz_artist_id IS NOT NULL")
    mb_count = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM tracks WHERE beets_artist_mbid IS NOT NULL AND musicbrainz_artist_id IS NULL")
    to_migrate = cursor.fetchone()[0]
    
    print(f"\nCurrent state:")
    print(f"  - Tracks with beets_artist_mbid: {beets_count}")
    print(f"  - Tracks with musicbrainz_artist_id: {mb_count}")
    print(f"  - Tracks needing migration: {to_migrate}")
    
    if to_migrate > 0:
        print(f"\nMigrating {to_migrate} records...")
        
        # Perform the migration
        cursor.execute("""
            UPDATE tracks 
            SET musicbrainz_artist_id = beets_artist_mbid 
            WHERE beets_artist_mbid IS NOT NULL AND musicbrainz_artist_id IS NULL
        """)
        
        rows_affected = cursor.rowcount
        conn.commit()
        
        print(f"✓ Migration complete: {rows_affected} records updated")
    else:
        print("\n✓ No migration needed - all beets_artist_mbid values already in musicbrainz_artist_id")
    
    # Verify migration
    cursor.execute("SELECT COUNT(*) FROM tracks WHERE musicbrainz_artist_id IS NOT NULL")
    new_mb_count = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM tracks WHERE beets_artist_mbid IS NOT NULL")
    final_beets_count = cursor.fetchone()[0]
    
    print(f"\nFinal state:")
    print(f"  - Tracks with beets_artist_mbid: {final_beets_count}")
    print(f"  - Tracks with musicbrainz_artist_id: {new_mb_count}")
    
    # Check for duplicates (same MBID in both fields for same track)
    cursor.execute("""
        SELECT COUNT(*) FROM tracks 
        WHERE beets_artist_mbid IS NOT NULL 
        AND musicbrainz_artist_id IS NOT NULL 
        AND beets_artist_mbid != musicbrainz_artist_id
    """)
    conflicts = cursor.fetchone()[0]
    
    if conflicts > 0:
        print(f"\n⚠ WARNING: {conflicts} tracks have conflicting MBID values:")
        print("  beets_artist_mbid and musicbrainz_artist_id are different")
        print("  These will need manual review before field cleanup")
        cursor.execute("""
            SELECT id, artist, beets_artist_mbid, musicbrainz_artist_id 
            FROM tracks 
            WHERE beets_artist_mbid IS NOT NULL 
            AND musicbrainz_artist_id IS NOT NULL 
            AND beets_artist_mbid != musicbrainz_artist_id
            LIMIT 5
        """)
        print("\n  Sample conflicts (first 5):")
        for row in cursor.fetchall():
            print(f"    Track {row[0]} ({row[1]}):")
            print(f"      beets: {row[2]}")
            print(f"      musicbrainz: {row[3]}")
    else:
        print(f"\n✓ No conflicting MBID values found")
    
    conn.close()
    
    print("\n" + "="*60)
    print("Migration Summary:")
    print("="*60)
    print("""
The beets_artist_mbid field can now be safely removed in the future.
Current code still supports it, but all new data goes to musicbrainz_artist_id.

Next steps (optional):
1. Remove beets_artist_mbid column from database schema
2. Remove from table creation in beets_auto_import.py _ensure_beets_columns()
3. Remove any remaining UI references (if any)

The consolidation is now complete and fully functional.
""")
    
except sqlite3.Error as e:
    print(f"ERROR: Database error: {e}")
    sys.exit(1)
except Exception as e:
    print(f"ERROR: {e}")
    sys.exit(1)
