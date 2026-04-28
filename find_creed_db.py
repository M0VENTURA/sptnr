#!/usr/bin/env python3
"""Probe PostgreSQL library data for Creed tracks."""

import sys

sys.path.insert(0, ".")

from helpers.db_utils import get_db_connection


def main() -> int:
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) AS count FROM tracks WHERE LOWER(artist) LIKE %s", ("%creed%",))
        count_row = cursor.fetchone()
        count = count_row.get("count") if isinstance(count_row, dict) else count_row[0]

        if count and int(count) > 0:
            print(f"Found {int(count)} Creed tracks in PostgreSQL.")
            cursor.execute(
                """
                SELECT title, album
                FROM tracks
                WHERE LOWER(artist) LIKE %s
                ORDER BY title
                LIMIT 5
                """,
                ("%creed%",),
            )
            for row in cursor.fetchall():
                if isinstance(row, dict):
                    title = row.get("title")
                    album = row.get("album")
                else:
                    title, album = row
                print(f"  - {title} ({album})")
            return 0

        cursor.execute("SELECT COUNT(*) AS total FROM tracks")
        total_row = cursor.fetchone()
        total = total_row.get("total") if isinstance(total_row, dict) else total_row[0]
        print(f"No Creed tracks found in PostgreSQL (total tracks: {int(total or 0)}).")
        return 1

    except Exception as exc:
        print(f"Error checking PostgreSQL library: {exc}")
        return 2
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
