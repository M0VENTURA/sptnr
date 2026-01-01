
#!/usr/bin/env python3
import sqlite3

# ✅ Define the full schema for the tracks table
required_columns = {
    "id": "TEXT",                       # Primary key
    "artist": "TEXT",
    "album": "TEXT",
    "title": "TEXT",
    "spotify_score": "REAL",
    "lastfm_score": "REAL",
    "listenbrainz_score": "REAL",
    "age_score": "REAL",
    "final_score": "REAL",              # ✅ Added for weighted score
    "stars": "INTEGER",
    "genres": "TEXT",
    "navidrome_genres": "TEXT",
    "spotify_genres": "TEXT",
    "lastfm_tags": "TEXT",
    "discogs_genres": "TEXT",
    "audiodb_genres": "TEXT",
    "musicbrainz_genres": "TEXT",
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
    "last_scanned": "TEXT",
    "mbid": "TEXT",
    "suggested_mbid": "TEXT",
    "suggested_mbid_confidence": "REAL",
    "single_sources": "TEXT",           # ✅ JSON or comma-delimited
    "is_spotify_single": "INTEGER",
    "spotify_total_tracks": "INTEGER",
    "spotify_album_type": "TEXT",
    "lastfm_ratio": "REAL"              # ✅ Added for Last.fm ratio
}

def update_schema(db_path):
    """
    Ensure the 'tracks' and 'artist_stats' tables exist and have all required columns.
    Adds missing columns dynamically and creates indexes for performance.
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    print(f"🔍 Updating schema for database: {db_path}")

    # ✅ Ensure tracks table exists
    cursor.execute("CREATE TABLE IF NOT EXISTS tracks (id TEXT PRIMARY KEY);")

    # ✅ Get existing columns for tracks
    cursor.execute("PRAGMA table_info(tracks);")
    existing_columns = [row[1] for row in cursor.fetchall()]

    # ✅ Add missing columns
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
    indexes = [
        ("idx_artist_stats_name", "artist_stats(artist_name)"),
        ("idx_artist_stats_updated", "artist_stats(last_updated)"),
        ("idx_tracks_artist", "tracks(artist)"),
        ("idx_tracks_album", "tracks(album)"),
        ("idx_tracks_last_scanned", "tracks(last_scanned)"),
        ("idx_tracks_is_single", "tracks(is_single)"),
        ("idx_tracks_mbid", "tracks(mbid)"),
        ("idx_tracks_suggested_mbid", "tracks(suggested_mbid)")
    ]
    for idx_name, idx_target in indexes:
        cursor.execute(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {idx_target};")
    print("✔ Indexes verified.")

    conn.commit()
    conn.close()
    print("\n✅ Database schema update complete with indexes.")

# ✅ Standalone usage
if __name__ == "__main__":
    print("⚠️ Please call update_schema(db_path) from main.py with the correct DB path.")
