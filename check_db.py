
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
    "discogs_album_id": "TEXT",
    "musicbrainz_genres": "TEXT",
    "spotify_album": "TEXT",
    "spotify_album_id": "TEXT",               # Spotify album/release ID (manually editable)
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
    "spotify_id": "TEXT",               # Spotify track ID
    "spotify_total_tracks": "INTEGER",
    "spotify_album_type": "TEXT",
    "lastfm_ratio": "REAL",              # ✅ Added for Last.fm ratio
    # ✅ Audit/Evidence fields for single detection
    "discogs_single_confirmed": "INTEGER",  # 1 if Discogs API returned explicit single
    "discogs_video_found": "INTEGER",       # 1 if official video found on Discogs
    "is_canonical_title": "INTEGER",        # 1 if no remix/live/edit suffix
    "title_similarity_to_base": "REAL",     # Similarity score (0–1) to canonical form
    "album_context_live": "INTEGER",        # 1 if album marked as live
    "album_context_unplugged": "INTEGER",   # 1 if album marked as unplugged
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
    "lyrics": "TEXT",                       # Song lyrics if embedded
    "cover_art_url": "TEXT",                # Album cover art URL from MusicBrainz
    # ✅ Genre fields from multiple sources
    "beets_genre": "TEXT",                  # Genre from beets metadata
    "navidrome_genre": "TEXT",              # Genre from Navidrome (replaces navidrome_genres)
    "listenbrainz_genre_tags": "TEXT",      # JSON array of genre tags from ListenBrainz
    "genre_display": "TEXT",                # Primary display genre (aggregated)
    "album_folder": "TEXT",                 # Album folder path for beets updates
    # ✅ Beets metadata columns
    "beets_mbid": "TEXT",                     # MusicBrainz ID from beets
    "beets_similarity": "REAL",               # Beets match similarity (0-1)
    "beets_album_mbid": "TEXT",               # Album MBID from beets
    "beets_artist_mbid": "TEXT",              # Artist MBID from beets
    "beets_album_artist": "TEXT",             # Album artist from beets
    # ✅ Per-source single detection results (cached to avoid repeated API calls)
    "source_discogs_single": "INTEGER",       # 1 if Discogs API returned explicit single
    "source_discogs_video": "INTEGER",        # 1 if Discogs official video found
    "source_spotify_single": "INTEGER",       # 1 if Spotify marked as single
    "source_musicbrainz_single": "INTEGER",   # 1 if MusicBrainz reports single
    "source_lastfm_single": "INTEGER",        # 1 if Last.fm reports single
    "source_short_release": "INTEGER",        # 1 if album has 2 or fewer tracks
    "source_detection_date": "TEXT",          # When these source detections were last checked
    # ✅ Artist ID caching columns (to reduce redundant API calls)
    "spotify_artist_id": "TEXT",              # Spotify artist ID for this track's artist
    "lastfm_artist_mbid": "TEXT",             # Last.fm artist MBID (if available)
    "discogs_artist_id": "TEXT",              # Discogs artist ID
    "musicbrainz_artist_id": "TEXT",          # MusicBrainz artist ID
    # ✅ Advanced single detection fields
    "global_popularity": "REAL",              # Global popularity across all track versions
    "zscore": "REAL",                         # Z-score within album for single detection
    "metadata_single": "INTEGER",             # 1 if marked as single in metadata (Spotify/MB)
    "is_compilation": "INTEGER",              # 1 if album is compilation/greatest hits
    # ✅ Spotify Audio Features (from /audio-features endpoint)
    "spotify_tempo": "REAL",                  # BPM (tempo)
    "spotify_energy": "REAL",                 # Energy (0.0-1.0)
    "spotify_danceability": "REAL",           # Danceability (0.0-1.0)
    "spotify_valence": "REAL",                # Valence/positivity (0.0-1.0)
    "spotify_acousticness": "REAL",           # Acousticness (0.0-1.0)
    "spotify_instrumentalness": "REAL",       # Instrumentalness (0.0-1.0)
    "spotify_liveness": "REAL",               # Liveness (0.0-1.0)
    "spotify_speechiness": "REAL",            # Speechiness (0.0-1.0)
    "spotify_loudness": "REAL",               # Loudness in dB
    "spotify_key": "INTEGER",                 # Key (0-11, C=0, C#=1, etc.)
    "spotify_mode": "INTEGER",                # Mode (0=minor, 1=major)
    "spotify_time_signature": "INTEGER",      # Time signature (beats per measure)
    # ✅ Artist Metadata (from /artists endpoint)
    "spotify_artist_genres": "TEXT",          # JSON array of artist genres
    "spotify_artist_popularity": "INTEGER",   # Artist popularity (0-100)
    # ✅ Album Metadata (from /albums endpoint)
    "spotify_album_label": "TEXT",            # Record label
    "spotify_explicit": "INTEGER",            # Explicit flag (0 or 1)
    # ✅ Derived Genre Tags (custom logic)
    "special_tags": "TEXT",                   # JSON array: Christmas, Cover, Live, Acoustic, Orchestral, Instrumental
    "normalized_genres": "TEXT",              # JSON array of broad/normalized genres
    "merged_version_tags": "TEXT",            # JSON array of tags inherited from other versions
    "raw_spotify_genres": "TEXT",             # JSON array from artist metadata (raw, unprocessed)
    # ✅ Metadata refresh tracking
    "metadata_last_updated": "TEXT"           # Timestamp when metadata was last fetched
}

# ✅ Define columns for the artists table
required_artist_columns = {
    "id": "TEXT",                           # Primary key
    "name": "TEXT",                         # Artist name
    "beets_genre": "TEXT",                  # Genre from beets metadata
    "navidrome_genre": "TEXT",              # Genre from Navidrome
    "listenbrainz_genre_tags": "TEXT",      # JSON array of genre tags from ListenBrainz
    "genre_display": "TEXT"                 # Primary display genre (aggregated)
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
            try:
                cursor.execute(f"ALTER TABLE tracks ADD COLUMN {col} {col_type};")
                columns_added.append(col)
            except sqlite3.OperationalError as e:
                # Column might already exist due to race condition or previous partial run
                if "duplicate column name" in str(e).lower():
                    print(f"⚠️ Column {col} already exists, skipping")
                else:
                    raise
    
    if columns_added:
        print(f"✅ Added {len(columns_added)} missing column(s): {', '.join(columns_added)}")

    # ✅ Ensure artists table exists and add genre columns
    cursor.execute("CREATE TABLE IF NOT EXISTS artists (id TEXT PRIMARY KEY, name TEXT NOT NULL);")
    
    # Get existing columns for artists
    cursor.execute("PRAGMA table_info(artists);")
    existing_artist_columns = [row[1] for row in cursor.fetchall()]
    
    # Add missing artist columns
    artist_columns_added = []
    for col, col_type in required_artist_columns.items():
        if col not in existing_artist_columns:
            try:
                cursor.execute(f"ALTER TABLE artists ADD COLUMN {col} {col_type};")
                artist_columns_added.append(col)
            except sqlite3.OperationalError as e:
                # Column might already exist due to race condition or previous partial run
                if "duplicate column name" in str(e).lower():
                    print(f"⚠️ Column {col} already exists in artists table, skipping")
                else:
                    raise
    
    if artist_columns_added:
        print(f"✅ Added {len(artist_columns_added)} missing artist column(s): {', '.join(artist_columns_added)}")

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
    
    # ✅ Ensure navidrome_users table exists (for per-user features)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS navidrome_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            display_name TEXT,
            navidrome_base_url TEXT,
            navidrome_password TEXT,
            listenbrainz_token TEXT,
            spotify_client_id TEXT,
            spotify_client_secret TEXT,
            is_active BOOLEAN DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    
    # ✅ Ensure user_loved_tracks table exists
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_loved_tracks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            track_id TEXT NOT NULL,
            is_loved BOOLEAN DEFAULT 0,
            loved_at TIMESTAMP,
            synced_to_listenbrainz BOOLEAN DEFAULT 0,
            last_sync_attempt TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES navidrome_users(id) ON DELETE CASCADE,
            FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE,
            UNIQUE(user_id, track_id)
        );
    """)
    
    # ✅ Ensure user_loved_albums table exists
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_loved_albums (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            artist TEXT NOT NULL,
            album TEXT NOT NULL,
            is_loved BOOLEAN DEFAULT 0,
            loved_at TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES navidrome_users(id) ON DELETE CASCADE,
            UNIQUE(user_id, artist, album)
        );
    """)
    
    # ✅ Ensure user_loved_artists table exists
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_loved_artists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            artist TEXT NOT NULL,
            is_loved BOOLEAN DEFAULT 0,
            loved_at TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES navidrome_users(id) ON DELETE CASCADE,
            UNIQUE(user_id, artist)
        );
    """)
    
    # ✅ Ensure albums table exists (for album-level metadata)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS albums (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            artist TEXT NOT NULL,
            album TEXT NOT NULL,
            beets_genre TEXT,
            navidrome_genre TEXT,
            listenbrainz_genre_tags TEXT,
            genre_display TEXT,
            UNIQUE(artist, album)
        );
    """)

    # ✅ Ensure bookmarks table exists
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bookmarks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            name TEXT NOT NULL,
            artist TEXT,
            album TEXT,
            track_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(type, name, artist, album, track_id)
        );
    """)
    
    # ✅ Ensure scan_history table exists
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS scan_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            artist TEXT NOT NULL,
            album TEXT NOT NULL,
            scan_type TEXT NOT NULL,
            scan_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            tracks_processed INTEGER DEFAULT 0,
            status TEXT DEFAULT 'completed'
        );
    """)
    # ✅ Ensure 'source' column exists in scan_history
    cursor.execute("PRAGMA table_info(scan_history);")
    scan_history_columns = [row[1] for row in cursor.fetchall()]
    if 'source' not in scan_history_columns:
        try:
            cursor.execute("ALTER TABLE scan_history ADD COLUMN source TEXT DEFAULT '';")
            print("✅ Added missing 'source' column to scan_history table.")
        except sqlite3.OperationalError as e:
            # Column might already exist due to race condition or previous partial run
            if "duplicate column name" in str(e).lower():
                print("⚠️ Column 'source' already exists in scan_history table, skipping")
            else:
                raise
    
    # ✅ Ensure missing_releases table exists
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS missing_releases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            artist TEXT NOT NULL,
            release_id TEXT NOT NULL,
            title TEXT NOT NULL,
            primary_type TEXT,
            first_release_date TEXT,
            cover_art_url TEXT,
            category TEXT,
            last_checked TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(artist, release_id)
        );
    """)

    # ✅ Ensure managed_downloads table exists
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS managed_downloads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            release_id TEXT NOT NULL,
            release_title TEXT NOT NULL,
            artist TEXT NOT NULL,
            method TEXT NOT NULL,
            status TEXT DEFAULT 'queued',
            download_query TEXT,
            external_id TEXT,
            error_message TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP
        );
    """)

    # ✅ Ensure slskd_search_results table exists (for user-selectable Soulseek results)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS slskd_search_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            download_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            filename TEXT NOT NULL,
            size INTEGER,
            match_score REAL,
            selected BOOLEAN DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (download_id) REFERENCES managed_downloads(id) ON DELETE CASCADE
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
        ("idx_tracks_suggested_mbid", "tracks(suggested_mbid)"),
        ("idx_bookmarks_type", "bookmarks(type)"),
        ("idx_bookmarks_created", "bookmarks(created_at)"),
        ("idx_scan_history_timestamp", "scan_history(scan_timestamp DESC)"),
        ("idx_scan_history_type", "scan_history(scan_type)"),
        ("idx_missing_releases_artist", "missing_releases(artist)"),
        ("idx_missing_releases_checked", "missing_releases(last_checked DESC)"),
        ("idx_managed_downloads_status", "managed_downloads(status)"),
        ("idx_managed_downloads_created", "managed_downloads(created_at DESC)"),
        ("idx_slskd_search_results_download", "slskd_search_results(download_id)"),
        ("idx_slskd_search_results_selected", "slskd_search_results(selected)"),
        # Per-user love indexes
        ("idx_user_loved_tracks_user", "user_loved_tracks(user_id)"),
        ("idx_user_loved_tracks_track", "user_loved_tracks(track_id)"),
        ("idx_user_loved_tracks_status", "user_loved_tracks(is_loved)"),
        ("idx_user_loved_albums_user", "user_loved_albums(user_id)"),
        ("idx_user_loved_albums_name", "user_loved_albums(artist, album)"),
        ("idx_user_loved_artists_user", "user_loved_artists(user_id)"),
        ("idx_user_loved_artists_name", "user_loved_artists(artist)"),
        ("idx_albums_artist_album", "albums(artist, album)"),
        ("idx_navidrome_users_username", "navidrome_users(username)"),
        # Artist ID indexes for fast cache lookups
        ("idx_tracks_spotify_artist_id", "tracks(spotify_artist_id)"),
        ("idx_tracks_musicbrainz_artist_id", "tracks(musicbrainz_artist_id)"),
        ("idx_tracks_discogs_artist_id", "tracks(discogs_artist_id)"),
        # Advanced single detection indexes
        ("idx_tracks_isrc", "tracks(isrc)"),
        ("idx_tracks_duration", "tracks(duration)"),
        ("idx_tracks_global_popularity", "tracks(global_popularity)"),
        ("idx_tracks_zscore", "tracks(zscore)")
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
