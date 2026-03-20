#!/usr/bin/env python3
"""
Migration: Add individual-copy tracking columns to download_queue table

Adds:
  copied_individually      INTEGER DEFAULT 0
  copied_individually_at   TEXT

These columns let the download monitor track which queue items were
manually copied to the music library before the rest of the album
completed, so the auto-move logic can skip them and the UI can show
an "Already Copied" badge.

PostgreSQL-only migration.
"""
import os
import sys

DB_PATH = os.environ.get("DB_PATH", "/database/sptnr.db")


def migrate():
    """Add copied_individually and copied_individually_at columns."""
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

        added_columns = []

        if 'copied_individually' not in columns:
            print("Adding copied_individually column...")
            cursor.execute(
                "ALTER TABLE download_queue ADD COLUMN copied_individually INTEGER DEFAULT 0"
            )
            added_columns.append('copied_individually')

        if 'copied_individually_at' not in columns:
            print("Adding copied_individually_at column...")
            cursor.execute(
                "ALTER TABLE download_queue ADD COLUMN copied_individually_at TEXT"
            )
            added_columns.append('copied_individually_at')

        conn.commit()
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
