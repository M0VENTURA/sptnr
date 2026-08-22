"""Shared PostgreSQL schema definitions for Popularr."""

from __future__ import annotations

# =============================================================================
# TABLE CREATION DEFINITIONS
# =============================================================================

TABLES_TO_ENSURE: dict[str, str] = {
    "artists": """CREATE TABLE IF NOT EXISTS artists (id TEXT PRIMARY KEY, name TEXT NOT NULL)""",
    "tracks": """CREATE TABLE IF NOT EXISTS tracks (id TEXT PRIMARY KEY)""",
    "artist_stats": """
        CREATE TABLE IF NOT EXISTS artist_stats (
            artist_id TEXT PRIMARY KEY, artist_name TEXT NOT NULL, 
            album_count INTEGER, track_count INTEGER, last_updated TEXT
        )
    """,
    "scan_history": """
        CREATE TABLE IF NOT EXISTS scan_history (
            id BIGSERIAL PRIMARY KEY, scan_type TEXT, artist TEXT, album TEXT, 
            status TEXT, message TEXT, started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, 
            completed_at TIMESTAMP, duration_seconds DOUBLE PRECISION
        )
    """,
    "download_queue": """
        CREATE TABLE IF NOT EXISTS download_queue (
            id BIGSERIAL PRIMARY KEY, artist TEXT, title TEXT, album TEXT, 
            status TEXT DEFAULT 'queued', source TEXT DEFAULT 'soulseek', 
            priority INTEGER DEFAULT 5, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, 
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """,
    "bookmarks": """
        CREATE TABLE IF NOT EXISTS bookmarks (
            id BIGSERIAL PRIMARY KEY, bookmark_type TEXT, type TEXT, entity_id TEXT, 
            name TEXT, artist_name TEXT, album_name TEXT, title TEXT, notes TEXT, 
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """,
    "genre_updates": """
        CREATE TABLE IF NOT EXISTS genre_updates (
            id BIGSERIAL PRIMARY KEY, artist_name TEXT, album_name TEXT, track_id TEXT, 
            genres_before TEXT, genres_after TEXT, action_type TEXT, 
            affected_track_count INTEGER, change_summary TEXT, 
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """,
    "track_popularity_cache": """
        CREATE TABLE IF NOT EXISTS track_popularity_cache (
            id BIGSERIAL PRIMARY KEY, artist TEXT NOT NULL, title TEXT NOT NULL, 
            lastfm_listeners INTEGER DEFAULT 0, lastfm_playcount BIGINT DEFAULT 0, 
            listenbrainz_listens INTEGER DEFAULT 0, listenbrainz_users INTEGER DEFAULT 0, 
            lastfm_tags TEXT,
            source TEXT DEFAULT 'bulk', updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_track_popularity_artist_title UNIQUE (artist, title)
        )
    """,
    "artist_release_cache": """
        CREATE TABLE IF NOT EXISTS artist_release_cache (
            id BIGSERIAL PRIMARY KEY,
            artist TEXT NOT NULL,
            title TEXT NOT NULL,
            release_type TEXT,
            category TEXT,
            source TEXT NOT NULL,
            release_id TEXT,
            year INTEGER,
            is_promo BOOLEAN DEFAULT FALSE,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_artist_release_artist_title_source UNIQUE (artist, title, source)
        )
    """,
    "slskd_search_logs": """
        CREATE TABLE IF NOT EXISTS slskd_search_logs (
            id BIGSERIAL PRIMARY KEY, created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP, 
            search_type TEXT, query TEXT, queue_id INTEGER, artist TEXT, title TEXT, 
            album TEXT, result_count INTEGER DEFAULT 0, duration_seconds REAL, 
            notes TEXT, selected_result JSONB, results JSONB
        )
    """,
    "folder_matches": """
        CREATE TABLE IF NOT EXISTS folder_matches (
            id BIGSERIAL PRIMARY KEY,
            folder_path TEXT NOT NULL,
            release_mbid TEXT NOT NULL,
            release_title TEXT,
            artist TEXT,
            release_year INTEGER,
            status TEXT DEFAULT 'matched',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_folder_matches_folder_path UNIQUE (folder_path)
        )
    """,
    "user_favourites": """
        CREATE TABLE IF NOT EXISTS user_favourites (
            id BIGSERIAL PRIMARY KEY,
            username TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            navidrome_id TEXT,
            is_favourite BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_user_favourites_entity UNIQUE (username, entity_type, entity_id)
        )
    """,
    "musicbrainz_releases": """
        CREATE TABLE IF NOT EXISTS musicbrainz_releases (
            id BIGSERIAL PRIMARY KEY, release_id TEXT NOT NULL UNIQUE, 
            release_title TEXT NOT NULL, artist TEXT NOT NULL, release_year INTEGER, 
            total_tracks INTEGER, monitoring_folder_path TEXT, final_folder_path TEXT, 
            status TEXT DEFAULT 'active', method TEXT, discovered_count INTEGER DEFAULT 0, 
            organized_count INTEGER DEFAULT 0, finalized_count INTEGER DEFAULT 0, 
            album_artist TEXT, genres TEXT, cover_art_url TEXT, release_source TEXT, 
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, 
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, finalized_at TIMESTAMP
        )
    """,
    "missing_releases": """
        CREATE TABLE IF NOT EXISTS missing_releases (
            id BIGSERIAL PRIMARY KEY, artist TEXT NOT NULL, release_id TEXT NOT NULL, 
            title TEXT, primary_type TEXT, first_release_date TEXT, cover_art_url TEXT, 
            category TEXT, last_checked TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            tracklist TEXT
        )
    """,
    "musicbrainz_release_tracks": """
        CREATE TABLE IF NOT EXISTS musicbrainz_release_tracks (
            id BIGSERIAL PRIMARY KEY, release_id TEXT NOT NULL, queue_id BIGINT, 
            disc_number INTEGER, track_number INTEGER, track_title TEXT, 
            track_artist TEXT, duration INTEGER, isrc TEXT, recording_title TEXT, 
            recording_mbid TEXT, composer TEXT, album_artist TEXT, year TEXT, 
            status TEXT DEFAULT 'queued', found_filename TEXT, file_path TEXT, 
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, 
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, 
            FOREIGN KEY (release_id) REFERENCES musicbrainz_releases(release_id), 
            FOREIGN KEY (queue_id) REFERENCES download_queue(id)
        )
    """,
    "album_art": """
        CREATE TABLE IF NOT EXISTS album_art (
            id BIGSERIAL PRIMARY KEY, artist_name TEXT NOT NULL, album_name TEXT NOT NULL, 
            image_data BYTEA, image_mime_type TEXT, source TEXT, 
            downloaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, 
            CONSTRAINT unique_artist_album UNIQUE (artist_name, album_name)
        )
    """,
    "metadata_conflicts": """
        CREATE TABLE IF NOT EXISTS metadata_conflicts (
            id BIGSERIAL PRIMARY KEY,
            track_id VARCHAR(255) NOT NULL REFERENCES tracks(id) ON DELETE CASCADE,
            provider VARCHAR(50) NOT NULL,
            field_name VARCHAR(50) NOT NULL,
            local_value TEXT,
            remote_value TEXT,
            artist_name TEXT,
            album_name TEXT,
            track_title TEXT,
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            resolved_at TIMESTAMP,
            resolved_by TEXT,
            CONSTRAINT uq_track_provider_field UNIQUE (track_id, provider, field_name)
        )
    """,
    "correction_ignores": """
        CREATE TABLE IF NOT EXISTS correction_ignores (
            id BIGSERIAL PRIMARY KEY,
            album_artist TEXT NOT NULL DEFAULT '',
            album TEXT NOT NULL,
            field TEXT NOT NULL,
            ignored_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_correction_ignore UNIQUE (album_artist, album, field)
        )
    """,
    "upcoming_releases": """
        CREATE TABLE IF NOT EXISTS upcoming_releases (
            id BIGSERIAL PRIMARY KEY,
            artist_name TEXT NOT NULL,
            album_name TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'wikipedia',
            source_key TEXT,
            release_date TEXT,
            release_year INTEGER,
            artist_in_collection BOOLEAN DEFAULT FALSE,
            album_in_collection BOOLEAN DEFAULT FALSE,
            release_group_mbid TEXT,
            match_source TEXT,
            primary_type TEXT,
            mbid_match_status TEXT DEFAULT 'unmatched',
            mbid_source TEXT,
            mbid_confidence TEXT,
            mbid_match_score REAL,
            mbid_last_checked_at TEXT,
            mbid_manual_override BOOLEAN DEFAULT FALSE,
            status TEXT DEFAULT 'discovered',
            last_seen_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT uq_upcoming_artist_album UNIQUE (artist_name, album_name)
        )
    """,
    "scan_states": """
        CREATE TABLE IF NOT EXISTS scan_states (
            scan_type TEXT PRIMARY KEY,
            is_running BOOLEAN DEFAULT FALSE,
            status TEXT DEFAULT 'idle',
            stop_requested BOOLEAN DEFAULT FALSE,
            current_artist TEXT,
            last_scanned_artist TEXT,
            extra_data JSONB DEFAULT '{}'::jsonb,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        )
    """,
    "artist_images": """
        CREATE TABLE IF NOT EXISTS artist_images (
            artist_name TEXT PRIMARY KEY,
            image_url TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """,
}

# =============================================================================
# COLUMN REGISTRIES (For incremental schema updates)
# =============================================================================

COLUMN_REGISTRY: dict[str, dict[str, str]] = {
    "tracks": {
        "artist_id": "TEXT", "artist": "TEXT", "album_artist": "TEXT", "album": "TEXT",
        "title": "TEXT", "genres": "TEXT", "genre": "TEXT", "manual_genres": "TEXT",
        "navidrome_genres": "TEXT", "spotify_genres": "TEXT", "listenbrainz_genres": "TEXT",
        "discogs_genres": "TEXT", "musicbrainz_genres": "TEXT", "essentia_genres": "TEXT",
        "file_path": "TEXT", "duration": "DOUBLE PRECISION",
        "track_number": "TEXT", "disc_number": "TEXT", "year": "TEXT", "release_year": "INTEGER",
        "last_scanned": "TEXT", "updated_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP", "spotify_score": "DOUBLE PRECISION", "lastfm_score": "DOUBLE PRECISION",
        "lastfm_listeners": "INTEGER", "lastfm_playcount": "BIGINT",
        "lastfm_tags": "TEXT", "lastfm_last_updated": "TIMESTAMP",
        "listenbrainz_score": "DOUBLE PRECISION", "listenbrainz_listens": "INTEGER",
        "listenbrainz_users": "INTEGER", "listenbrainz_last_updated": "TIMESTAMP",
        "musicbrainz_last_updated": "TIMESTAMP",
        "discogs_last_updated": "TIMESTAMP",
        "final_score": "DOUBLE PRECISION", "stars": "INTEGER", "star_rating": "INTEGER",
        "popularity": "DOUBLE PRECISION", "is_single": "BOOLEAN DEFAULT FALSE",
        "single_confidence": "TEXT", "single_confidence_score": "DOUBLE PRECISION",
        "single_status": "TEXT", "single_sources": "TEXT", "single_sources_used": "TEXT",
        "single_detection_last_updated": "TIMESTAMP", "single_manual_override": "BOOLEAN DEFAULT FALSE",
        "popularity_marked": "BOOLEAN DEFAULT FALSE",
        "popularity_frozen": "BOOLEAN DEFAULT FALSE",
        "popularity_frozen_at": "TIMESTAMP", "mbid": "TEXT", "suggested_mbid": "TEXT",
        "musicbrainz_id": "TEXT", "musicbrainz_trackid": "TEXT", "musicbrainz_albumid": "TEXT",
        "recording_mbid": "TEXT",
        "musicbrainz_album_mbid": "TEXT", "musicbrainz_artistid": "TEXT", "musicbrainz_albumartistid": "TEXT",
        "musicbrainz_releasegroupid": "TEXT", "musicbrainz_releasetrackid": "TEXT",
        "musicbrainz_workid": "TEXT", "musicbrainz_albumstatus": "TEXT", "musicbrainz_albumtype": "TEXT",
        "spotify_album_type": "TEXT", "releasetype": "TEXT",
        "writer": "TEXT", "isrc": "TEXT", "work": "TEXT", "pending_mb_updates": "TEXT",
        "mb_ignored_fields": "TEXT", "is_cover": "BIGINT DEFAULT 0", "is_cover_reason": "TEXT",
        "original_cover_artist": "TEXT", "cover_manual_override": "BOOLEAN DEFAULT FALSE",
        "cover_last_checked": "TIMESTAMP",
        "is_live": "BIGINT DEFAULT 0", "is_acoustic": "BIGINT DEFAULT 0", "is_remix": "BIGINT DEFAULT 0",
        "album_context_live": "BIGINT DEFAULT 0",
        "alternate_take": "BIGINT DEFAULT 0", "base_track_id": "TEXT",
        "is_compilation": "BIGINT DEFAULT 0", "releasecountry": "TEXT",
        "discogs_artist_id": "TEXT",
        "mood": "TEXT", "mood_confidence": "DOUBLE PRECISION", "mood_source": "TEXT",
        "mood_last_updated": "TIMESTAMP", "danceability": "DOUBLE PRECISION",
        "essentia_last_updated": "TIMESTAMP", "essentia_model_version": "TEXT",
        "essentia_scan_version": "TEXT", "bpm": "DOUBLE PRECISION",
        "verification_status": "TEXT", "verification_checked_at": "TIMESTAMP", "verification_error": "TEXT",
    },
    "artists": {
        "country": "TEXT", "bio": "TEXT", "image_url": "TEXT",
        "similar_artists_lastfm": "TEXT", "similar_artists_listenbrainz": "TEXT",
        "similar_artists_last_updated": "TEXT",
        "lastfm_artist_tags": "TEXT",
        "members": "TEXT", "members_last_updated": "TEXT",
    },
    "artist_stats": {
        "mean_popularity": "DOUBLE PRECISION",
        "median_popularity": "DOUBLE PRECISION",
        "popularity_stddev": "DOUBLE PRECISION",
        "popularity_mad": "DOUBLE PRECISION",
    },
    "download_queue": {
        "priority": "INTEGER DEFAULT 5",
        "album_artist": "TEXT", "track_number": "TEXT", "disc_number": "TEXT", "year": "TEXT",
        "release_year": "INTEGER", "release_id": "TEXT", "release_source": "TEXT", "release_mbid": "TEXT",
        "release_date": "TEXT", "recording_mbid": "TEXT", "cover_art_url": "TEXT", "duration": "DOUBLE PRECISION",
        "updated_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        "found_filename": "TEXT", "file_path": "TEXT", "matched_file_path": "TEXT", "music_file_path": "TEXT",
        "failure_reason": "TEXT", "retry_count": "INTEGER DEFAULT 0", "max_retries": "INTEGER DEFAULT 5",
        "retry_delay_minutes": "INTEGER DEFAULT 30", "next_retry_at": "TIMESTAMP", "last_failure_time": "TIMESTAMP",
        "imported_at": "TIMESTAMP", "copied_individually": "BOOLEAN DEFAULT FALSE",
        "copied_individually_at": "TIMESTAMP", "match_confidence": "DOUBLE PRECISION",
        "verified_in_music_at": "TIMESTAMP", "moved_at": "TIMESTAMP",
        "match_method": "TEXT", "metadata": "JSONB DEFAULT '{}'::jsonb", "metadata_id": "BIGINT",
        "release_metadata_id": "BIGINT", "collection_track_id": "TEXT", "collection_matched_at": "TEXT",
        "in_collection": "INTEGER DEFAULT 0", "auto_delete_at": "TIMESTAMP", "queue_folder": "TEXT",
        "is_manual_download": "BOOLEAN DEFAULT FALSE", "slskd_username": "TEXT", "slskd_transfer_id": "TEXT",
        "slskd_state": "TEXT", "slskd_queue_position": "INTEGER", "slskd_last_sync_at": "TIMESTAMP",
        "search_query": "TEXT", "source_id": "TEXT", "source": "TEXT DEFAULT 'soulseek'",
        "status_changed_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        "import_group": "TEXT", "import_type": "TEXT DEFAULT 'song'",
        "progress": "DOUBLE PRECISION DEFAULT 0", "speed": "BIGINT DEFAULT 0",
    },
    "musicbrainz_releases": {"album_artist": "TEXT", "genres": "TEXT", "cover_art_url": "TEXT", "release_source": "TEXT"},
    "musicbrainz_release_tracks": {"composer": "TEXT", "album_artist": "TEXT", "year": "TEXT"},
    "artist_release_cache": {"is_promo": "BOOLEAN DEFAULT FALSE", "category": "TEXT"},
    "missing_releases": {"tracklist": "TEXT"},
    "track_popularity_cache": {"lastfm_tags": "TEXT"},
    "scan_history": {
        "scan_type": "TEXT", "artist": "TEXT", "album": "TEXT",
        "status": "TEXT", "message": "TEXT",
        "started_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        "completed_at": "TIMESTAMP", "duration_seconds": "DOUBLE PRECISION",
    },
    "slskd_search_logs": {
        "search_type": "TEXT", "query": "TEXT", "queue_id": "INTEGER",
        "artist": "TEXT", "title": "TEXT", "album": "TEXT",
        "result_count": "INTEGER DEFAULT 0", "duration_seconds": "REAL",
        "notes": "TEXT", "selected_result": "JSONB", "results": "JSONB",
    },
    "folder_matches": {
        "release_mbid": "TEXT", "release_title": "TEXT", "artist": "TEXT",
        "release_year": "INTEGER", "status": "TEXT DEFAULT 'matched'",
        "updated_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
    },
    "user_favourites": {
        "navidrome_id": "TEXT", "is_favourite": "BOOLEAN DEFAULT FALSE",
        "updated_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
    },
    "upcoming_releases": {
        "release_year": "INTEGER",
        "source_key": "TEXT",
        "artist_in_collection": "BOOLEAN DEFAULT FALSE",
        "album_in_collection": "BOOLEAN DEFAULT FALSE",
        "mbid_match_status": "TEXT DEFAULT 'unmatched'",
        "mbid_source": "TEXT",
        "mbid_confidence": "TEXT",
        "mbid_match_score": "REAL",
        "mbid_last_checked_at": "TEXT",
        "mbid_manual_override": "BOOLEAN DEFAULT FALSE",
        "candidate_release_group_mbid": "TEXT",
        "updated_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
    },
}

# =============================================================================
# INDEXES
# =============================================================================

INDEXES_TO_ENSURE: tuple[str, ...] = (
    "CREATE INDEX IF NOT EXISTS idx_tracks_artist_id ON tracks (artist_id)",
    "CREATE INDEX IF NOT EXISTS idx_tracks_artist ON tracks (artist)",
    "CREATE INDEX IF NOT EXISTS idx_tracks_album_artist ON tracks (album_artist)",
    "CREATE INDEX IF NOT EXISTS idx_tracks_album ON tracks (album)",
    "CREATE INDEX IF NOT EXISTS idx_tracks_final_score ON tracks (final_score DESC)",
    "CREATE INDEX IF NOT EXISTS idx_download_queue_status ON download_queue (status)",
    "CREATE INDEX IF NOT EXISTS idx_download_queue_release_id ON download_queue (release_id)",
    "CREATE INDEX IF NOT EXISTS idx_slskd_search_logs_created_at ON slskd_search_logs (created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_missing_releases_artist ON missing_releases (artist)",
    "CREATE INDEX IF NOT EXISTS idx_missing_releases_release_id ON missing_releases (release_id)",
    "CREATE INDEX IF NOT EXISTS idx_scan_history_scope ON scan_history (scan_type, artist, album, status, started_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_tracks_album_scope ON tracks (LOWER(COALESCE(NULLIF(album_artist, ''), artist)), LOWER(COALESCE(album, '')))",
    "CREATE INDEX IF NOT EXISTS idx_tracks_artist_norm ON tracks (LOWER(COALESCE(NULLIF(album_artist, ''), artist)))",
    "CREATE INDEX IF NOT EXISTS idx_tracks_album_norm ON tracks (LOWER(COALESCE(album, '')))",
    "CREATE INDEX IF NOT EXISTS idx_tracks_album_artist_trim ON tracks (LOWER(TRIM(COALESCE(NULLIF(album_artist, ''), artist))))",
    "CREATE INDEX IF NOT EXISTS idx_album_art_artist_album ON album_art (LOWER(artist_name), LOWER(album_name))",
    "CREATE INDEX IF NOT EXISTS idx_mb_releases_status ON musicbrainz_releases(status)",
    "CREATE INDEX IF NOT EXISTS idx_mb_releases_created ON musicbrainz_releases(created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_mb_release_tracks_release ON musicbrainz_release_tracks(release_id)",
    "CREATE INDEX IF NOT EXISTS idx_mb_release_tracks_status ON musicbrainz_release_tracks(release_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_folder_matches_folder_path ON folder_matches (folder_path)",
    "CREATE INDEX IF NOT EXISTS idx_user_favourites_user ON user_favourites (username, entity_type)",
)

# =============================================================================
# LEGACY ALIASES (For bootstrap.py compatibility)
# =============================================================================

TRACK_COLUMNS_TO_ENSURE = COLUMN_REGISTRY["tracks"]
DOWNLOAD_QUEUE_COLUMNS_TO_ENSURE = COLUMN_REGISTRY["download_queue"]
MUSICBRAINZ_RELEASES_COLUMNS_TO_ENSURE = COLUMN_REGISTRY["musicbrainz_releases"]
MUSICBRAINZ_RELEASE_TRACKS_COLUMNS_TO_ENSURE = COLUMN_REGISTRY["musicbrainz_release_tracks"]

# This matches your original logic where you only wanted a specific subset
# for the startup/bootstrap check.
QUEUE_STARTUP_COLUMNS_TO_ENSURE = {
    key: DOWNLOAD_QUEUE_COLUMNS_TO_ENSURE[key]
    for key in (
        "priority", "release_id", "release_source", "track_number", "disc_number",
        "status_changed_at", "updated_at",
    )
}
