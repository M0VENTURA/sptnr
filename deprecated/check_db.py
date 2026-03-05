#!/usr/bin/env python3
import sqlite3

# Database connection constants
# Note: This timeout value (120.0) matches the timeout used throughout the codebase
# (see app.py, db_utils.py, scan_helpers.py, etc.)
DB_TIMEOUT = 120.0  # Timeout for database connections in seconds

# ✅ Define the full schema for the tracks table
required_columns = {
    "id": "TEXT",                       # Primary key
    "artist": "TEXT",
    "album": "TEXT",
    "title": "TEXT",
    "spotify_score": "REAL",
    "lastfm_score": "REAL",
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
    "single_manual_override": "INTEGER",     # ✅ 1 if user manually set is_single (skip auto-detection)
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
    "albumartist": "TEXT",                  # Navidrome albumartist field (raw from metadata)
    "albumartistsort": "TEXT",              # Album artist sort name
    "bpm": "INTEGER",                       # Beats per minute
    "bitrate": "INTEGER",                   # Audio bitrate in kbps
    "sample_rate": "INTEGER",               # Sample rate in Hz
    "isrc": "TEXT",                         # International Standard Recording Code
    "composer": "TEXT",                     # Composer/songwriter
    "comment": "TEXT",                      # Comment field from file
    "lyrics": "TEXT",                       # Song lyrics if embedded
    # ✅ Artist Credits and Collaborations (for compilations)
    "featured_artists": "TEXT",             # JSON array of featured/collaborating artists (from ARTISTS raw tag)
    "performers": "TEXT",                   # JSON array of performers (from PERFORMER raw tag)
    "is_compilation_track": "INTEGER",      # 1 if track has multiple artists (featured artists differ from album artist)
    "compilation_artists": "TEXT",          # JSON array of all non-album-artist artists for this track
    # ✅ Additional Navidrome metadata fields
    "arranger": "TEXT",                     # Track arranger
    "artists": "TEXT",                      # JSON array of artist names
    "artistsort": "TEXT",                   # Artist sort name
    "asin": "TEXT",                         # Amazon Standard Identification Number
    "barcode": "TEXT",                      # Album barcode (UPC/EAN)
    "catalognumber": "TEXT",                # Catalog number
    "label": "TEXT",                        # Record label
    "media": "TEXT",                        # Release media type (CD, Vinyl, Digital, etc.)
    "mixer": "TEXT",                        # Audio engineer/mixer
    "performer": "TEXT",                    # JSON array of performer credits
    "producer": "TEXT",                     # JSON array of producer names
    "releasecountry": "TEXT",               # Release country code
    "releasestatus": "TEXT",                # Release status (official, bootleg, etc.)
    "releasetype": "TEXT",                  # Release type (album, single, EP, etc.)
    "script": "TEXT",                       # Script code (e.g., Latn, Cyrl)
    "work": "TEXT",                         # Musical work name
    "writer": "TEXT",                       # JSON array of songwriter/writer names
    # ✅ MusicBrainz relationship IDs
    "musicbrainz_albumartistid": "TEXT",    # MusicBrainz album artist ID
    "musicbrainz_albumid": "TEXT",          # MusicBrainz album/release ID
    "musicbrainz_albumstatus": "TEXT",      # Release status from MusicBrainz
    "musicbrainz_albumtype": "TEXT",        # Release type from MusicBrainz
    "musicbrainz_releasegroupid": "TEXT",   # MusicBrainz release group ID
    "musicbrainz_releasetrackid": "TEXT",   # MusicBrainz release track ID
    "musicbrainz_trackid": "TEXT",          # MusicBrainz track/recording ID (raw from MP3 tag)
    "musicbrainz_workid": "TEXT",           # MusicBrainz work ID
    "musicbrainz_track_artistid": "TEXT",   # MusicBrainz track artist ID (raw from MP3 tag)
    "musicbrainz_releasecountry": "TEXT",   # MusicBrainz release country code
    "musicbrainz_releasestatus": "TEXT",    # MusicBrainz release status (distinct from albumstatus)
    "musicbrainz_releasetype": "TEXT",      # MusicBrainz release type (distinct from albumtype)
    # ✅ Date fields with more granularity
    "originaldate": "TEXT",                 # Original release date (YYYY-MM-DD)
    "originalyear": "INTEGER",              # Original release year
    "totaldiscs": "INTEGER",                # Total number of discs
    "tracktotal": "INTEGER",                # Total number of tracks on the album
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
    "musicbrainz_album_mbid": "TEXT",         # Album MBID (MusicBrainz release ID)
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
    "artist_country": "TEXT",                 # Artist country/origin for genre tagging
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
    "discogs_artist_genres": "TEXT",          # JSON array of Discogs artist genres
    "musicbrainz_artist_genres": "TEXT",      # JSON array of MusicBrainz artist genres
    # ✅ Album Metadata (from /albums endpoint)
    "spotify_album_label": "TEXT",            # Record label
    "spotify_explicit": "INTEGER",            # Explicit flag (0 or 1)
    # ✅ Derived Genre Tags (custom logic)
    "special_tags": "TEXT",                   # JSON array: Christmas, Cover, Live, Acoustic, Orchestral, Instrumental
    "normalized_genres": "TEXT",              # JSON array of broad/normalized genres
    "merged_version_tags": "TEXT",            # JSON array of tags inherited from other versions
    # ✅ Metadata refresh tracking
    "metadata_last_updated": "TEXT",          # Timestamp when metadata was last fetched
    # ✅ Discogs metadata columns for comprehensive single detection
    "discogs_release_id": "TEXT",             # Discogs release ID
    "discogs_master_id": "TEXT",              # Discogs master release ID
    "discogs_formats": "TEXT",                # JSON array of format objects
    "discogs_format_descriptions": "TEXT",    # JSON array of description strings
    "discogs_is_single": "INTEGER",           # 1 if Discogs confirms single, 0 if not
    "discogs_track_titles": "TEXT",           # JSON array of track title strings
    "discogs_release_year": "INTEGER",        # Release year from Discogs
    "discogs_label": "TEXT",                  # Record label from Discogs
    "discogs_country": "TEXT",                # Release country from Discogs
    # ✅ Single Detection Algorithm Fields (per problem statement)
    "single_status": "TEXT",                  # none/low/medium/high per detection algorithm
    "single_confidence_score": "REAL",        # Numeric confidence score (0.0-1.0)
    "single_sources_used": "TEXT",            # JSON array: sources that confirmed single
    "z_score": "REAL",                        # Z-score for single detection (alias for zscore, backward compatibility)
    "album_z_score": "REAL",                  # Album-level z-score for single detection
    "artist_z_score": "REAL",                 # Artist-level z-score for single detection (NEW)
    "spotify_version_count": "INTEGER",       # Number of exact-match Spotify versions
    "discogs_release_ids": "TEXT",            # JSON array of Discogs release IDs
    "musicbrainz_release_group_ids": "TEXT",  # JSON array of MusicBrainz release group IDs
    "single_detection_last_updated": "TEXT",   # Timestamp when single detection last ran
    # ✅ Cover Song Detection
    "is_cover": "INTEGER",                     # 1 if song is detected as a cover (different composer than artist's typical)
    "is_cover_reason": "TEXT",                 # Reason for cover detection (e.g., "composer differs from artist's typical")
    # ✅ Alternate take detection fields (for parenthesis matching)
    "alternate_take": "INTEGER",               # 1 if track is alternate take (similar title with parenthesis)
    "base_track_id": "TEXT",                   # ID of base track (if this is an alternate take)
    "last_spotify_lookup": "TEXT",              # Timestamp of last Spotify API lookup (for 24hr caching)
    "is_standout_track": "INTEGER",             # 1 if track is standout (artist-level z-score >= 2.0)
    # ✅ Last.fm Temporal Popularity Data (for trend detection)
    "lastfm_7day_listeners": "INTEGER",         # Last.fm listeners in past 7 days
    "lastfm_365day_listeners": "INTEGER",       # Last.fm listeners in past 365 days
    "lastfm_alltime_listeners": "INTEGER",      # Last.fm all-time listeners (cache of existing data)
    "momentum_score": "REAL",                   # Trend velocity: (7day/alltime) / (365day/alltime)
    "popularity_trend": "TEXT",                 # Trend classification: 'accelerating', 'stable', 'declining'
    "lastfm_temporal_last_updated": "TEXT",     # Timestamp of last temporal data fetch
    # ✅ Genre and Tag data from multiple sources (for display and aggregation)
    "spotify_genres": "TEXT",                   # JSON array of Spotify artist genres
    "lastfm_tags": "TEXT",                      # JSON array of Last.fm tags with name and count
    "listenbrainz_genres": "TEXT",              # JSON array of ListenBrainz genre tags with count
    "discogs_genres": "TEXT",                   # JSON array of Discogs genres
    "musicbrainz_genres": "TEXT",               # JSON array of MusicBrainz genres
    "tags_last_updated": "TEXT"                 # Timestamp when tags were last fetched
}

# ✅ Define columns for the artists table
required_artist_columns = {
    "id": "TEXT",                           # Primary key
    "name": "TEXT",                         # Artist name
    "beets_genre": "TEXT",                  # Genre from beets metadata
    "navidrome_genre": "TEXT",              # Genre from Navidrome
    "listenbrainz_genre_tags": "TEXT",      # JSON array of genre tags from ListenBrainz
    "lastfm_artist_tags": "TEXT",           # JSON array of Last.fm artist tags
    "genre_display": "TEXT",                # Primary display genre (aggregated)
    "country": "TEXT",                      # Artist country/origin from MusicBrainz
    "musicbrainz_area_id": "TEXT",          # MusicBrainz area ID for geographical data
    "image_url": "TEXT",                    # Cached artist image URL from MusicBrainz
    "bio": "TEXT",                          # Cached artist bio from MusicBrainz
    "similar_artists_lastfm": "TEXT",       # JSON array of similar artists from Last.fm
    "similar_artists_listenbrainz": "TEXT", # JSON array of similar artists from ListenBrainz
    "similar_artists_last_updated": "TEXT"  # Timestamp when similar artists were last fetched
}

# ✅ Define columns for the artist_stats table
required_artist_stats_columns = {
    "artist_id": "TEXT",                    # Primary key
    "artist_name": "TEXT",                  # Artist name
    "album_count": "INTEGER",               # Number of albums
    "track_count": "INTEGER",               # Number of tracks
    "last_updated": "TEXT",                 # Last update timestamp
    "mean_popularity": "REAL",              # Mean (average) popularity across all tracks
    "median_popularity": "REAL",            # Median popularity across all tracks (legacy)
    "popularity_stddev": "REAL",            # Standard deviation of popularity
    "popularity_mad": "REAL",               # Median Absolute Deviation (MAD) - robust alternative to stddev
    "mean_popularity_adjusted": "REAL"      # Mean popularity adjusted for pre-2005 releases
}

def update_schema(db_path):
    """
    Ensure the 'tracks', 'artists', and 'artist_stats' tables exist and have all required columns.
    Adds missing columns dynamically and creates indexes for performance.
    """
    conn = sqlite3.connect(db_path, timeout=DB_TIMEOUT)
    
    # Enable WAL mode for better concurrent access
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError as e:
        # WAL mode might not be supported on some filesystems (e.g., read-only)
        # Log a warning but continue - the timeout alone helps with locking
        print(f"⚠️ Could not enable WAL mode: {e}. Continuing with default journal mode.")
    
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
            artist_id TEXT PRIMARY KEY
        );
    """)
    
    # Get existing columns for artist_stats
    cursor.execute("PRAGMA table_info(artist_stats);")
    existing_artist_stats_columns = [row[1] for row in cursor.fetchall()]
    
    # Add missing artist_stats columns
    # Note: col and col_type values come from required_artist_stats_columns dictionary (not user input)
    artist_stats_columns_added = []
    for col, col_type in required_artist_stats_columns.items():
        if col not in existing_artist_stats_columns:
            try:
                cursor.execute(f"ALTER TABLE artist_stats ADD COLUMN {col} {col_type};")
                artist_stats_columns_added.append(col)
            except sqlite3.OperationalError as e:
                # Column might already exist due to race condition or previous partial run
                if "duplicate column name" in str(e).lower():
                    print(f"⚠️ Column {col} already exists in artist_stats table, skipping")
                else:
                    raise
    
    if artist_stats_columns_added:
        print(f"✅ Added {len(artist_stats_columns_added)} missing artist_stats column(s): {', '.join(artist_stats_columns_added)}")
    
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
            artist_mbid TEXT,
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

    # ✅ Ensure album_art table exists for storing downloaded album artwork
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS album_art (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            artist_name TEXT NOT NULL,
            album_name TEXT NOT NULL,
            image_data BLOB,
            image_mime_type TEXT DEFAULT 'image/jpeg',
            source TEXT,
            downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(artist_name, album_name)
        );
    """)

    # ✅ Add artist_mbid column to missing_releases if it doesn't exist
    cursor.execute("PRAGMA table_info(missing_releases);") 
    missing_releases_columns = [row[1] for row in cursor.fetchall()]
    if 'artist_mbid' not in missing_releases_columns:
        try:
            cursor.execute("ALTER TABLE missing_releases ADD COLUMN artist_mbid TEXT;")
            print("✅ Added artist_mbid column to missing_releases table")
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e).lower():
                raise

    # ✅ Ensure album_art table has required indexes
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
    
    # ✅ Add missing columns to download_queue if they don't exist
    cursor.execute("PRAGMA table_info(download_queue);")
    existing_queue_columns = [row[1] for row in cursor.fetchall()]
    
    download_queue_new_columns = {
        "search_query": "TEXT",
        "source": "TEXT DEFAULT 'soulseek'",
        "source_id": "TEXT",
        "priority": "INTEGER DEFAULT 5",
        "found_filename": "TEXT",
        "metadata": "TEXT",
        "retry_count": "INTEGER DEFAULT 0",
        "max_retries": "INTEGER DEFAULT 5",
        "failure_reason": "TEXT",
        "last_failure_time": "TIMESTAMP",
        "retry_delay_minutes": "INTEGER DEFAULT 30",
        "next_retry_at": "TIMESTAMP",
        "imported_at": "TIMESTAMP",
        "track_number": "TEXT",
        "album_artist": "TEXT",
        "year": "TEXT",
        "release_id": "TEXT",
        "release_source": "TEXT"
    }
    
    queue_columns_added = []
    for col, col_type in download_queue_new_columns.items():
        if col not in existing_queue_columns:
            try:
                cursor.execute(f"ALTER TABLE download_queue ADD COLUMN {col} {col_type};")
                queue_columns_added.append(col)
            except sqlite3.OperationalError as e:
                # Column might already exist due to race condition
                if "duplicate column name" not in str(e).lower():
                    print(f"⚠ Could not add column {col} to download_queue: {e}")
    
    if queue_columns_added:
        print(f"✅ Added {len(queue_columns_added)} missing download_queue column(s): {', '.join(queue_columns_added)}")
    
    # ✅ Ensure download_queue has proper defaults for retry columns
    try:
        # Set default max_retries for existing records if NULL
        cursor.execute("UPDATE download_queue SET max_retries = 5 WHERE max_retries IS NULL;")
        if cursor.rowcount > 0:
            print(f"✅ Set max_retries default (5) for {cursor.rowcount} download_queue records")
        
        # Set default retry_delay_minutes for existing records if NULL
        cursor.execute("UPDATE download_queue SET retry_delay_minutes = 30 WHERE retry_delay_minutes IS NULL;")
        if cursor.rowcount > 0:
            print(f"✅ Set retry_delay_minutes default (30) for {cursor.rowcount} download_queue records")
        
        conn.commit()
    except Exception as e:
        print(f"⚠ Could not update download_queue defaults: {e}")
    
    # ✅ Add missing columns to managed_downloads for persistent search feature
    cursor.execute("PRAGMA table_info(managed_downloads);")
    existing_download_columns = [row[1] for row in cursor.fetchall()]
    
    managed_downloads_new_columns = {
        "persistent_search": "INTEGER DEFAULT 0",
        "max_retries": "INTEGER DEFAULT 3",
        "retry_count": "INTEGER DEFAULT 0",
        "retry_delay_seconds": "INTEGER DEFAULT 300",
        "last_search_attempt": "TIMESTAMP",
        "completion_verified": "INTEGER DEFAULT 0"
    }
    
    download_columns_added = []
    for col, col_type in managed_downloads_new_columns.items():
        if col not in existing_download_columns:
            try:
                cursor.execute(f"ALTER TABLE managed_downloads ADD COLUMN {col} {col_type};")
                download_columns_added.append(col)
            except sqlite3.OperationalError as e:
                # Column might already exist
                if "duplicate column name" not in str(e).lower():
                    raise
    
    if download_columns_added:
        print(f"✅ Added {len(download_columns_added)} missing managed_downloads column(s): {', '.join(download_columns_added)}")
    
    # ✅ Ensure playlist_download_sessions table exists (for grouped downloads)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS playlist_download_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_name TEXT NOT NULL,
            user TEXT,
            status TEXT DEFAULT 'in_progress',
            total_tracks INTEGER,
            completed_tracks INTEGER DEFAULT 0,
            failed_tracks INTEGER DEFAULT 0,
            skipped_tracks INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP
        );
    """)
    
    # ✅ Add missing columns to playlist_download_sessions
    cursor.execute("PRAGMA table_info(playlist_download_sessions);")
    existing_session_columns = [row[1] for row in cursor.fetchall()]
    
    playlist_session_new_columns = {
        "average_retry_count": "REAL DEFAULT 0",
        "estimated_completion": "TIMESTAMP",
        "priority_queue": "INTEGER DEFAULT 0"
    }
    
    session_columns_added = []
    for col, col_type in playlist_session_new_columns.items():
        if col not in existing_session_columns:
            try:
                cursor.execute(f"ALTER TABLE playlist_download_sessions ADD COLUMN {col} {col_type};")
                session_columns_added.append(col)
            except sqlite3.OperationalError as e:
                if "duplicate column name" not in str(e).lower():
                    raise
    
    if session_columns_added:
        print(f"✅ Added {len(session_columns_added)} missing playlist_download_sessions column(s): {', '.join(session_columns_added)}")
    
    # ✅ Update managed_downloads to support method fallback
    cursor.execute("PRAGMA table_info(managed_downloads);")
    existing_fallback_columns = [row[1] for row in cursor.fetchall()]
    
    fallback_columns = {
        "session_id": "INTEGER",
        "current_method": "TEXT",
        "methods_tried": "TEXT",
        "next_method": "TEXT",
        "last_method_failed_at": "TIMESTAMP",
        "priority": "INTEGER DEFAULT 0"
    }
    
    fallback_columns_added = []
    for col, col_type in fallback_columns.items():
        if col not in existing_fallback_columns:
            try:
                cursor.execute(f"ALTER TABLE managed_downloads ADD COLUMN {col} {col_type};")
                fallback_columns_added.append(col)
            except sqlite3.OperationalError as e:
                if "duplicate column name" not in str(e).lower():
                    raise
    
    if fallback_columns_added:
        print(f"✅ Added {len(fallback_columns_added)} fallback column(s) to managed_downloads: {', '.join(fallback_columns_added)}")
    
    # ✅ Create index on playlist_download_sessions
    indexes_to_add = [
        ("idx_playlist_sessions_status", "playlist_download_sessions(status)"),
        ("idx_playlist_sessions_created", "playlist_download_sessions(created_at DESC)"),
        ("idx_managed_downloads_session", "managed_downloads(session_id)"),
        ("idx_managed_downloads_priority", "managed_downloads(priority DESC)")
    ]
    
    for idx_name, idx_def in indexes_to_add:
        if idx_name not in [row[1] for row in cursor.execute("SELECT name, tbl_name FROM sqlite_master WHERE type='index'").fetchall()]:
            try:
                cursor.execute(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {idx_def};")
            except sqlite3.OperationalError:
                pass

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

    # ✅ Ensure download_queue table exists (for tracking user-initiated downloads with retry logic)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS download_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            artist TEXT NOT NULL,
            album TEXT,
            title TEXT NOT NULL,
            search_query TEXT,
            source TEXT DEFAULT 'soulseek',
            source_id TEXT,
            status TEXT DEFAULT 'queued',
            priority INTEGER DEFAULT 5,
            found_filename TEXT,
            file_path TEXT UNIQUE,
            metadata JSON,
            retry_count INTEGER DEFAULT 0,
            max_retries INTEGER DEFAULT 5,
            failure_reason TEXT,
            last_failure_time TIMESTAMP,
            retry_delay_minutes INTEGER DEFAULT 30,
            next_retry_at TIMESTAMP,
            imported_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
        # Download queue indexes
        ("idx_download_queue_status", "download_queue(status)"),
        ("idx_download_queue_source", "download_queue(source)"),
        ("idx_download_queue_next_retry", "download_queue(next_retry_at)"),
        ("idx_download_queue_artist_album", "download_queue(artist, album)"),
        ("idx_download_queue_created", "download_queue(created_at DESC)"),
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
        try:
            cursor.execute(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {idx_target};")
        except sqlite3.OperationalError as e:
            # Silently skip if column doesn't exist - it may be added later
            if "no such column" in str(e).lower() or "no such table" in str(e).lower():
                pass
            else:
                raise


    # ✅ Ensure lastfm_recommendations table exists (for caching Last.fm recommendations)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS lastfm_recommendations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            recommendation_type TEXT NOT NULL,
            item_name TEXT NOT NULL,
            artist_name TEXT,
            image_url TEXT,
            playcount INTEGER DEFAULT 0,
            lastfm_url TEXT,
            mbid TEXT,
            metadata TEXT,
            synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(username, recommendation_type, item_name, artist_name)
        )
    """)
    
    # ✅ Create indexes for lastfm_recommendations
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_lastfm_username_type 
        ON lastfm_recommendations(username, recommendation_type)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_lastfm_synced_at 
        ON lastfm_recommendations(synced_at)
    """)
    
    # ✅ Ensure lastfm_sync_history table exists (to track sync operations)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS lastfm_sync_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            sync_type TEXT NOT NULL,
            artists_count INTEGER DEFAULT 0,
            albums_count INTEGER DEFAULT 0,
            tracks_count INTEGER DEFAULT 0,
            filtered_count INTEGER DEFAULT 0,
            sync_status TEXT DEFAULT 'success',
            error_message TEXT,
            sync_start TIMESTAMP,
            sync_end TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # ✅ Create indexes for lastfm_sync_history
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_sync_history_username_time 
        ON lastfm_sync_history(username, created_at)
    """)
    
    # ✅ Ensure lastfm_scheduler_config table exists (to track scheduler settings)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS lastfm_scheduler_config (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            enabled BOOLEAN DEFAULT 1,
            sync_time TEXT DEFAULT '01:00',
            last_sync TIMESTAMP,
            next_sync TIMESTAMP,
            filter_existing BOOLEAN DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # ✅ Ensure upcoming_releases table exists (for tracking upcoming album releases)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS upcoming_releases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            artist_name TEXT NOT NULL,
            album_name TEXT NOT NULL,
            release_date TEXT,
            release_year INTEGER,
            source TEXT,
            artist_in_collection BOOLEAN DEFAULT FALSE,
            album_in_collection BOOLEAN DEFAULT FALSE,
            is_new_release BOOLEAN DEFAULT FALSE,
            notes TEXT,
            url TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(artist_name, album_name, release_date)
        )
    """)
    
    # ✅ Create indexes for upcoming_releases
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_upcoming_artist_collection 
        ON upcoming_releases(artist_in_collection, release_date DESC)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_upcoming_release_date 
        ON upcoming_releases(release_date)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_upcoming_year 
        ON upcoming_releases(release_year)
    """)
    
    # ✅ Create genre_updates table for tracking genre changes
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS genre_updates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            artist_name TEXT,
            album_name TEXT,
            track_id TEXT,
            genres_before TEXT,
            genres_after TEXT,
            action_type TEXT,
            affected_track_count INTEGER,
            change_summary TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # ✅ Create indexes for genre_updates
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_genre_updates_artist 
        ON genre_updates(artist_name, created_at DESC)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_genre_updates_album 
        ON genre_updates(album_name, created_at DESC)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_genre_updates_date 
        ON genre_updates(created_at DESC)
    """)
    
    # ✅ Ensure release_scrape_history table exists (to track when we last scraped)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS release_scrape_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_url TEXT,
            source_name TEXT,
            items_found INTEGER,
            items_added INTEGER,
            items_updated INTEGER,
            scrape_status TEXT,
            error_message TEXT,
            scrape_start TIMESTAMP,
            scrape_end TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    # ✅ Create indexes for release_scrape_history
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_scrape_history_source 
        ON release_scrape_history(source_name, created_at DESC)
    """)

    # ✅ MIGRATION: Fix download_queue table if file_path has NOT NULL constraint
    # This is needed because the column was created with NOT NULL in older versions
    try:
        cursor.execute("PRAGMA table_info(download_queue);")
        columns = cursor.fetchall()
        file_path_col = None
        for col in columns:
            if col[1] == 'file_path':
                file_path_col = col
                break
        
        # col[3] is the not_null flag (1 = NOT NULL, 0 = NULL allowed)
        if file_path_col and file_path_col[3] == 1:
            # file_path has NOT NULL constraint, need to fix it
            print("⚠️  Fixing download_queue table: removing NOT NULL constraint from file_path")
            
            # Disable foreign key checks
            cursor.execute("PRAGMA foreign_keys=OFF;")
            
            # Drop backup table if it exists from a previous failed migration
            try:
                cursor.execute("DROP TABLE IF EXISTS download_queue_backup;")
            except:
                pass
            
            # Backup data - create table and copy only existing columns
            cursor.execute("""
                CREATE TABLE download_queue_backup AS 
                SELECT * FROM download_queue;
            """)
            
            # Get column names from backup
            cursor.execute("PRAGMA table_info(download_queue_backup);")
            backup_columns = [row[1] for row in cursor.fetchall()]
            
            # Drop old table
            cursor.execute("DROP TABLE download_queue;")
            
            # Recreate with correct schema (file_path WITHOUT NOT NULL)
            cursor.execute("""
                CREATE TABLE download_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    artist TEXT NOT NULL,
                    album TEXT,
                    title TEXT NOT NULL,
                    search_query TEXT,
                    source TEXT DEFAULT 'soulseek',
                    source_id TEXT,
                    status TEXT DEFAULT 'queued',
                    priority INTEGER DEFAULT 5,
                    found_filename TEXT,
                    file_path TEXT UNIQUE,
                    metadata JSON,
                    retry_count INTEGER DEFAULT 0,
                    max_retries INTEGER DEFAULT 5,
                    failure_reason TEXT,
                    last_failure_time TIMESTAMP,
                    retry_delay_minutes INTEGER DEFAULT 30,
                    next_retry_at TIMESTAMP,
                    imported_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            
            # Get column names for new table
            cursor.execute("PRAGMA table_info(download_queue);")
            new_columns = [row[1] for row in cursor.fetchall()]
            
            # Only copy columns that exist in both tables
            common_columns = [col for col in backup_columns if col in new_columns]
            
            if common_columns:
                # Restore data using only common columns
                columns_list = ', '.join(common_columns)
                insert_sql = f"""
                    INSERT INTO download_queue ({columns_list})
                    SELECT {columns_list} FROM download_queue_backup;
                """
                cursor.execute(insert_sql)
                print(f"✅ Restored {len(common_columns)} columns of data from backup")
            
            # Drop backup table
            cursor.execute("DROP TABLE download_queue_backup;")
            
            # Re-enable foreign key checks
            cursor.execute("PRAGMA foreign_keys=ON;")
            
            print("✅ download_queue table fixed successfully")
    except Exception as e:
        print(f"Migration error: {e}")
        try:
            cursor.execute("PRAGMA foreign_keys=ON;")
        except:
            pass

    # ✅ Ensure musicbrainz_releases table exists (for tracking release downloads)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS musicbrainz_releases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            release_id TEXT NOT NULL UNIQUE,
            release_title TEXT NOT NULL,
            artist TEXT NOT NULL,
            release_year INTEGER,
            total_tracks INTEGER,
            
            monitoring_folder_path TEXT,
            final_folder_path TEXT,
            
            status TEXT DEFAULT 'active',
            method TEXT,
            
            discovered_count INTEGER DEFAULT 0,
            organized_count INTEGER DEFAULT 0,
            finalized_count INTEGER DEFAULT 0,
            
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            finalized_at TIMESTAMP
        );
    """)

    # ✅ Ensure musicbrainz_release_tracks table exists
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS musicbrainz_release_tracks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            release_id TEXT NOT NULL,
            queue_id INTEGER,
            
            track_number INTEGER,
            track_title TEXT,
            track_artist TEXT,
            duration INTEGER,
            isrc TEXT,
            
            found_filename TEXT,
            file_path TEXT,
            
            status TEXT DEFAULT 'queued',
            
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            
            FOREIGN KEY (release_id) REFERENCES musicbrainz_releases(release_id),
            FOREIGN KEY (queue_id) REFERENCES download_queue(id)
        );
    """)

    # ✅ Add missing columns to download_queue for release tracking
    cursor.execute("PRAGMA table_info(download_queue);")
    existing_queue_columns = [row[1] for row in cursor.fetchall()]
    
    release_tracking_columns = {
        "release_id": "TEXT",
        "track_number": "INTEGER",
        "is_final_file": "INTEGER DEFAULT 0",
        "mb_release_download_id": "INTEGER"
    }
    
    queue_release_columns_added = []
    for col, col_type in release_tracking_columns.items():
        if col not in existing_queue_columns:
            try:
                cursor.execute(f"ALTER TABLE download_queue ADD COLUMN {col} {col_type};")
                queue_release_columns_added.append(col)
            except sqlite3.OperationalError as e:
                if "duplicate column name" not in str(e).lower():
                    print(f"⚠ Could not add column {col} to download_queue: {e}")
    
    if queue_release_columns_added:
        print(f"✅ Added {len(queue_release_columns_added)} release tracking columns to download_queue: {', '.join(queue_release_columns_added)}")

    # ✅ Create indexes for musicbrainz_releases
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_mb_releases_status 
        ON musicbrainz_releases(status)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_mb_releases_created 
        ON musicbrainz_releases(created_at DESC)
    """)

    # ✅ Create indexes for musicbrainz_release_tracks
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_mb_release_tracks_release 
        ON musicbrainz_release_tracks(release_id)
    """)
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_mb_release_tracks_status 
        ON musicbrainz_release_tracks(release_id, status)
    """)

    # ✅ Create index for release_id in download_queue
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_download_queue_release_id 
        ON download_queue(release_id)
    """)

    conn.commit()
    conn.close()
    
    if columns_added:
        print("✅ Database schema updated successfully")

# ✅ Standalone usage
if __name__ == "__main__":
    print("⚠️ Please call update_schema(db_path) from main.py with the correct DB path.")
