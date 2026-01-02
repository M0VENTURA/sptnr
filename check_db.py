
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
    "score": "REAL",                    # Composite popularity score
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
    "navidrome_rating": "INTEGER",
    "spotify_total_tracks": "INTEGER",
    "spotify_album_type": "TEXT",
    "lastfm_ratio": "REAL",              # ✅ Added for Last.fm ratio
    # ✅ Audit/Evidence fields for single detection
    "discogs_single_confirmed": "INTEGER",  # 1 if Discogs API returned explicit single
    "discogs_video_found": "INTEGER",       # 1 if official video found on Discogs
    "is_canonical_title": "INTEGER",        # 1 if no remix/live/edit suffix
    "title_similarity_to_base": "REAL",     # Similarity score (0–1) to canonical form
    "album_context_live": "INTEGER",        # 1 if album marked as live/unplugged
    # ✅ Scoring context fields for reproducibility
    "adaptive_weight_spotify": "REAL",      # Adaptive weight used for this album
    "adaptive_weight_lastfm": "REAL",       # Adaptive Last.fm weight
    "adaptive_weight_listenbrainz": "REAL", # Adaptive ListenBrainz weight
    "album_median_score": "REAL",           # Median score for the album
    "spotify_release_age_days": "INTEGER",   # Days since release
    "popularity_score": "REAL",             # Calculated popularity from external sources
    "single_source": "TEXT",                # Source that confirmed single status
    # ✅ Audio file metadata
    "duration": "REAL",                     # Track duration in seconds
    "track_number": "INTEGER",              # Track number on album
    "disc_number": "INTEGER",               # Disc number for multi-disc albums
    "year": "INTEGER",                      # Release year
    "album_artist": "TEXT",                 # Album artist (may differ from track artist)
    "bpm": "INTEGER",                       # Beats per minute
    "bitrate": "INTEGER",                   # Audio bitrate in kbps
    "sample_rate": "INTEGER",               # Sample rate in Hz
    "isrc": "TEXT",                         # International Standard Recording Code
    "composer": "TEXT",                     # Composer/songwriter
    "comment": "TEXT",                      # Comment field from file
    "lyrics": "TEXT"                        # Song lyrics if embedded
}

def update_schema(db_path):
    """
    Ensure the 'tracks' and 'artist_stats' tables exist and have all required columns.
    Adds missing columns dynamically and creates indexes for performance.
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # ✅ Ensure tracks table exists
    cursor.execute("CREATE TABLE IF NOT EXISTS tracks (id TEXT PRIMARY KEY);")

    # ✅ Get existing columns for tracks
    cursor.execute("PRAGMA table_info(tracks);")
    existing_columns = [row[1] for row in cursor.fetchall()]

    # ✅ Add missing columns
    columns_added = []
    for col, col_type in required_columns.items():
        if col not in existing_columns:
            cursor.execute(f"ALTER TABLE tracks ADD COLUMN {col} {col_type};")
            columns_added.append(col)
    
    if columns_added:
        print(f"✅ Added {len(columns_added)} missing column(s): {', '.join(columns_added)}")

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

    conn.commit()
    conn.close()
    
    if columns_added:
        print("✅ Database schema updated successfully")

# ✅ Standalone usage
if __name__ == "__main__":
    print("⚠️ Please call update_schema(db_path) from main.py with the correct DB path.")
