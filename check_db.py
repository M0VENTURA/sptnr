
#!/usr/bin/env python3
import sqlite3

# Define the full schema for the tracks table
required_columns = {
    "id": "TEXT",
    "artist": "TEXT",
    "album": "TEXT",
    "title": "TEXT",
    "spotify_score": "REAL",
    "lastfm_score": "REAL",
    "listenbrainz_score": "REAL",
    "age_score": "REAL",
    "final_score": "REAL",
    "stars": "INTEGER",
    "genres": "TEXT",
    "navidrome_genres": "TEXT",
    "spotify_genres": "TEXT",
    "lastfm_tags": "TEXT",
    "spotify_album": "TEXT",
    "spotify_artist": "TEXT",
    "spotify_popularity": "INTEGER",
    "spotify_release_date": "TEXT",
    "spotify_album_art_url": "TEXT",
    "lastfm_track_playcount": "INTEGER",
    "lastfm_artist_playcount": "INTEGER",
    "file_path": "TEXT",
    "is_single": "BOOLEAN",
    "single_confidence": "TEXT",
    "last_scanned": "TEXT"
}

def update_schema(db_path):
    """
    Ensure the 'tracks' table exists and has all required columns.
    Creates missing columns if necessary.
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # ✅ Ensure table exists first (with only primary key if new)
    cursor.execute("CREATE TABLE IF NOT EXISTS tracks (id TEXT PRIMARY KEY);")

    # ✅ Get existing columns
    cursor.execute("PRAGMA table_info(tracks);")
    existing_columns = [row[1] for row in cursor.fetchall()]

    # ✅ Add missing columns
    for col, col_type in required_columns.items():
        if col not in existing_columns:
            print(f"✅ Adding missing column: {col} ({col_type})")
            cursor.execute(f"ALTER TABLE tracks ADD COLUMN {col} {col_type};")
        else:
            print(f"✔ Column exists: {col}")

    conn.commit()
    conn.close()
    print("\n✅ Database schema update complete.")

# ✅ Standalone usage
if __name__ == "__main__":
    print("⚠️ Please call update_schema(db_path) from main.py with the correct DB path.")
