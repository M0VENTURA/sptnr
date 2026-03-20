#!/usr/bin/env python3
"""
Migration: Add MusicBrainz MBID and extended metadata columns to download_queue table

Adds:
  release_mbid       TEXT       -- MusicBrainz release ID
  recording_mbid     TEXT       -- MusicBrainz recording ID
  release_year       INTEGER    -- Parsed release year (integer)
  duration           INTEGER    -- Track duration in seconds
  matched_file_path  TEXT       -- File path if already matched
  in_collection      INTEGER DEFAULT 0   -- Whether track exists in local library
  collection_track_id  TEXT     -- ID of matching track in collection
  collection_matched_at TEXT    -- Timestamp of collection match

These columns are required by download_monitor_enhancements.py and the
/api/queue/<id>/apply-mbid-match endpoint in app.py.

PostgreSQL-only migration.
"""
import os
import sys

DB_PATH = os.environ.get("DB_PATH", "/database/sptnr.db")


def migrate():
    """Add MBID and extended metadata columns to download_queue."""
    try:
        from app import get_db
        conn = get_db()

        cursor = conn.cursor()

        # Discover existing columns
        cursor.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'download_queue'
              AND table_schema = 'public'
        """)
        columns = [row[0] for row in cursor.fetchall()]

        new_columns = {
            'release_mbid': "TEXT",
            'recording_mbid': "TEXT",
            'release_year': "INTEGER",
            'duration': "INTEGER",
            'matched_file_path': "TEXT",
            'in_collection': "INTEGER DEFAULT 0",
            'collection_track_id': "TEXT",
            'collection_matched_at': "TEXT",
        }

        added_columns = []

        for col, col_type in new_columns.items():
            if col not in columns:
                print(f"Adding {col} column...")
                # col and col_type are sourced exclusively from the hardcoded
                # new_columns dict above; no user-supplied values are used.
                try:
                    cursor.execute(
                        f"ALTER TABLE download_queue ADD COLUMN {col} {col_type}"
                    )
                    conn.commit()
                    added_columns.append(col)
                except Exception as e:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    print(f"  Warning: could not add {col}: {e}")

        conn.close()

        if added_columns:
            print(f"✅ Migration complete! Added columns: {', '.join(added_columns)}")
        else:
            print("✅ All columns already exist, no migration needed")

        return True

    except Exception as e:
        print(f"❌ Migration failed: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == '__main__':
    print("Migrating database...")
    success = migrate()
    sys.exit(0 if success else 1)
