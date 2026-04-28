#!/usr/bin/env python3
"""
Migration: Convert tracks.track_number column from INTEGER/BIGINT to TEXT
Track numbers can be "1/12" format (track 1 of 12), not just integers.
PostgreSQL-only migration.
"""
import os
import sys

DB_PATH = os.environ.get("DB_PATH", "/database/sptnr.db")


def migrate():
    """Change tracks.track_number column type to TEXT"""
    try:
        from app import get_db
        conn = get_db()

        cursor = conn.cursor()

        cursor.execute("""
            SELECT data_type FROM information_schema.columns
            WHERE table_name = 'tracks' AND column_name = 'track_number'
              AND table_schema = 'public'
        """)
        row = cursor.fetchone()
        if row and row[0].lower() in ('text', 'character varying', 'varchar'):
            print("✅ tracks.track_number is already TEXT, no migration needed")
            conn.close()
            return True

        print("Converting tracks.track_number to TEXT...")
        cursor.execute(
            "ALTER TABLE tracks ALTER COLUMN track_number TYPE TEXT USING track_number::text"
        )
        conn.commit()
        print("✅ Migration complete: tracks.track_number is now TEXT")

        conn.close()
        return True

    except Exception as e:
        print(f"❌ Migration failed: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = migrate()
    sys.exit(0 if success else 1)
