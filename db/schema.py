"""Shared PostgreSQL schema definitions for Popularr/SPTNR."""

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
    "slskd_search_logs": """
        CREATE TABLE IF NOT EXISTS slskd_search_logs (
            id BIGSERIAL PRIMARY KEY, created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP, 
            search_type TEXT, query TEXT, queue_id INTEGER, artist TEXT, title TEXT, 
            album TEXT, result_count INTEGER DEFAULT 0, duration_seconds REAL, 
            notes TEXT, selected_result JSONB, results JSONB
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
            category TEXT, last_checked TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
}

# =============================================================================
# COLUMN REGISTRIES (For incremental schema updates)
# =============================================================================

COLUMN_REGISTRY: dict[str, dict[str, str]] = {
    "tracks": {
        "artist_id": "TEXT", "artist": "TEXT", "album_artist": "TEXT", "album": "TEXT",
        "title": "TEXT", "genres": "TEXT", "genre": "TEXT", "manual_genres": "TEXT",
        "navidrome_genres": "TEXT", "file_path": "TEXT", "duration": "DOUBLE PRECISION",
        "track_number": "TEXT", "disc_number": "TEXT", "year": "TEXT", "release_year": "INTEGER",
        "last_scanned": "TEXT", "spotify_score": "DOUBLE PRECISION", "lastfm_score": "DOUBLE PRECISION",
        "listenbrainz_score": "DOUBLE PRECISION", "age_score": "DOUBLE PRECISION",
        "final_score": "DOUBLE PRECISION", "stars": "INTEGER", "star_rating": "INTEGER",
        "popularity": "DOUBLE PRECISION", "is_single": "BOOLEAN DEFAULT FALSE",
        "single_confidence": "DOUBLE PRECISION", "popularity_frozen": "BOOLEAN DEFAULT FALSE",
        "popularity_frozen_at": "TIMESTAMP", "mbid": "TEXT", "suggested_mbid": "TEXT",
        "musicbrainz_id": "TEXT", "musicbrainz_trackid": "TEXT", "musicbrainz_albumid": "TEXT",
        "musicbrainz_album_mbid": "TEXT", "musicbrainz_artistid": "TEXT", "musicbrainz_albumartistid": "TEXT",
        "musicbrainz_releasegroupid": "TEXT", "musicbrainz_releasetrackid": "TEXT",
        "musicbrainz_workid": "TEXT", "musicbrainz_albumstatus": "TEXT", "musicbrainz_albumtype": "TEXT",
        "writer": "TEXT", "isrc": "TEXT", "work": "TEXT", "pending_mb_updates": "TEXT",
        "mb_ignored_fields": "TEXT", "is_cover": "BIGINT DEFAULT 0", "is_cover_reason": "TEXT",
        "original_cover_artist": "TEXT", "cover_manual_override": "BOOLEAN DEFAULT FALSE",
        "is_live": "BIGINT DEFAULT 0", "is_acoustic": "BIGINT DEFAULT 0", "is_remix": "BIGINT DEFAULT 0",
        "mood": "TEXT", "mood_confidence": "DOUBLE PRECISION", "mood_source": "TEXT",
        "mood_last_updated": "TIMESTAMP", "danceability": "DOUBLE PRECISION",
        "essentia_last_updated": "TIMESTAMP", "essentia_model_version": "TEXT",
        "verification_status": "TEXT", "verification_checked_at": "TIMESTAMP", "verification_error": "TEXT",
    },
    "download_queue": {
        "album_artist": "TEXT", "track_number": "TEXT", "disc_number": "TEXT", "year": "TEXT",
        "release_year": "INTEGER", "release_id": "TEXT", "release_source": "TEXT", "release_mbid": "TEXT",
        "recording_mbid": "TEXT", "cover_art_url": "TEXT", "duration": "DOUBLE PRECISION",
        "updated_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        "found_filename": "TEXT", "file_path": "TEXT", "matched_file_path": "TEXT", "music_file_path": "TEXT",
        "failure_reason": "TEXT", "retry_count": "INTEGER DEFAULT 0", "max_retries": "INTEGER DEFAULT 5",
        "retry_delay_minutes": "INTEGER DEFAULT 30", "next_retry_at": "TIMESTAMP", "last_failure_time": "TIMESTAMP",
        "imported_at": "TIMESTAMP", "copied_individually": "BOOLEAN DEFAULT FALSE",
        "copied_individually_at": "TIMESTAMP", "match_confidence": "DOUBLE PRECISION",
        "match_method": "TEXT", "metadata": "JSONB DEFAULT '{}'::jsonb", "metadata_id": "BIGINT",
        "release_metadata_id": "BIGINT", "collection_track_id": "TEXT", "collection_matched_at": "TEXT",
        "in_collection": "INTEGER DEFAULT 0", "auto_delete_at": "TIMESTAMP", "queue_folder": "TEXT",
        "is_manual_download": "BOOLEAN DEFAULT FALSE", "slskd_username": "TEXT", "slskd_transfer_id": "TEXT",
        "slskd_state": "TEXT", "slskd_queue_position": "INTEGER", "slskd_last_sync_at": "TIMESTAMP",
        "search_query": "TEXT", "source_id": "TEXT", "status_changed_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
    },
    "musicbrainz_releases": {"album_artist": "TEXT", "genres": "TEXT", "cover_art_url": "TEXT", "release_source": "TEXT"},
    "musicbrainz_release_tracks": {"composer": "TEXT", "album_artist": "TEXT", "year": "TEXT"},
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
    "CREATE INDEX IF NOT EXISTS idx_album_art_artist_album ON album_art (LOWER(artist_name), LOWER(album_name))",
    "CREATE INDEX IF NOT EXISTS idx_mb_releases_status ON musicbrainz_releases(status)",
    "CREATE INDEX IF NOT EXISTS idx_mb_releases_created ON musicbrainz_releases(created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_mb_release_tracks_release ON musicbrainz_release_tracks(release_id)",
    "CREATE INDEX IF NOT EXISTS idx_mb_release_tracks_status ON musicbrainz_release_tracks(release_id, status)",
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
        "release_id", "release_source", "track_number", "disc_number", 
        "album_artist", "year", "release_mbid", "recording_mbid", 
        "release_year", "duration", "matched_file_path", "music_file_path", 
        "cover_art_url", "metadata_id", "release_metadata_id", 
        "status_changed_at", "updated_at",
    )
}


