#!/usr/bin/env python3
"""
Migration: Convert tracks.track_number column from INTEGER/BIGINT to TEXT
Track numbers can be "1/12" format (track 1 of 12), not just integers.
Supports both PostgreSQL and SQLite.
"""
import os
import sys

DB_PATH = os.environ.get("DB_PATH", "/database/sptnr.db")


def migrate():
    """Change tracks.track_number column type to TEXT"""
    try:
        try:
            from app import get_db, _is_postgres_connection
            conn = get_db()
            is_pg = _is_postgres_connection(conn)
        except (ImportError, Exception):
            import sqlite3
            conn = sqlite3.connect(DB_PATH)
            is_pg = False

        cursor = conn.cursor()

        if is_pg:
            # Check current column type
            cursor.execute("""
                SELECT data_type FROM information_schema.columns
                WHERE table_name = 'tracks' AND column_name = 'track_number'
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

        else:
            # SQLite: check current type via PRAGMA
            cursor.execute("PRAGMA table_info(tracks)")
            col_info = {row[1]: row[2].upper() for row in cursor.fetchall()}
            current_type = col_info.get('track_number', '')

            if 'TEXT' in current_type or 'VARCHAR' in current_type or 'CHAR' in current_type:
                print("✅ tracks.track_number is already TEXT, no migration needed")
                conn.close()
                return True

            print("Converting tracks.track_number to TEXT (SQLite rename pattern)...")
            # SQLite doesn't support ALTER COLUMN TYPE; use rename pattern
            cursor.execute("ALTER TABLE tracks RENAME COLUMN track_number TO track_number_old")
            cursor.execute("ALTER TABLE tracks ADD COLUMN track_number TEXT")
            cursor.execute(
                "UPDATE tracks SET track_number = CAST(track_number_old AS TEXT) "
                "WHERE track_number_old IS NOT NULL"
            )
            # SQLite 3.35+ supports DROP COLUMN; fall back gracefully if not
            try:
                cursor.execute("ALTER TABLE tracks DROP COLUMN track_number_old")
            except Exception:
                print("Note: Could not drop track_number_old (SQLite < 3.35); column left in place")
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
