
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
    Ensure the 'tracks' and 'artist_stats' tables exist and have all required columns.
    Creates missing columns if necessary and adds indexes for performance.
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # ✅ Ensure tracks table exists
    cursor.execute("CREATE TABLE IF NOT EXISTS tracks (id TEXT PRIMARY KEY);")

    # ✅ Get existing columns for tracks
    cursor.execute("PRAGMA table_info(tracks);")
    existing_columns = [row[1] for row in cursor.fetchall()]

    # ✅ Add missing columns to tracks
    for col, col_type in required_columns.items():
        if col not in existing_columns:
            print(f"✅ Adding missing column: {col} ({col_type})")
            cursor.execute(f"ALTER TABLE tracks ADD COLUMN {col} {col_type};")
        else:
            print(f"✔ Column exists: {col}")

    # ✅ Ensure artist_stats table exists
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS artist_stats (
            artist_id TEXT PRIMARY KEY,
            artist_name TEXT,
            album_count INTEGER,
            track_count INTEGER,
            last_updated TEXT
        );
    """)
    print("✔ artist_stats table verified.")

    # ✅ Create indexes for faster lookups
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_artist_stats_name ON artist_stats(artist_name);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_artist_stats_updated ON artist_stats(last_updated);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tracks_artist ON tracks(artist);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tracks_album ON tracks(album);")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_tracks_last_scanned ON tracks(last_scanned);")

    conn.commit()
    conn.close()
    print("\n✅ Database schema update complete with indexes.")

# ✅ Standalone usage
if __name__ == "__main__":
    print("⚠️ Please call update_schema(db_path) from main.py with the correct DB path.")
