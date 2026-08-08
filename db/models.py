"""SQLAlchemy ORM models for Popularr.

Auto-generated from the existing ``db/schema.py`` table and column definitions.
Each class maps to a table managed by the application.

Conventions:
    - Table names are ``snake_case`` (matching the database).
    - Column names are ``snake_case`` with ``Nullable`` types where the schema
      does not specify ``NOT NULL``.
    - ``relationship()`` declarations are added sparingly — most queries in the
      codebase are explicit JOINs; we preserve that pattern.
    - Legacy ``tracks`` columns with ``DEFAULT`` values are replicated via
      ``server_default=text("...")`` or ``default=...`` on the ORM attribute.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Double,
    Float,
    ForeignKey,
    Integer,
    JSON,
    LargeBinary,
    Sequence,
    String,
    Text,
    text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import backref, relationship

from db.engine import Base


# =============================================================================
# artists
# =============================================================================

class Artist(Base):
    __tablename__ = "artists"

    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    country = Column(String, nullable=True)
    bio = Column(Text, nullable=True)
    image_url = Column(String, nullable=True)
    similar_artists_lastfm = Column(Text, nullable=True)
    similar_artists_listenbrainz = Column(Text, nullable=True)
    similar_artists_last_updated = Column(String, nullable=True)

    def __repr__(self) -> str:
        return f"<Artist(id={self.id!r}, name={self.name!r})>"


# =============================================================================
# artist_stats
# =============================================================================

class ArtistStat(Base):
    __tablename__ = "artist_stats"

    artist_id = Column(String, primary_key=True)
    artist_name = Column(String, nullable=False)
    album_count = Column(Integer, nullable=True)
    track_count = Column(Integer, nullable=True)
    last_updated = Column(String, nullable=True)

    def __repr__(self) -> str:
        return f"<ArtistStat(artist_id={self.artist_id!r})>"


# =============================================================================
# tracks — the largest table in the system
# =============================================================================

class Track(Base):
    __tablename__ = "tracks"

    id = Column(String, primary_key=True)

    # Core metadata
    artist_id = Column(String, nullable=True)
    artist = Column(String, nullable=True)
    album_artist = Column(String, nullable=True)
    album = Column(String, nullable=True)
    title = Column(String, nullable=True)

    # Genre / tags
    genres = Column(Text, nullable=True)
    genre = Column(String, nullable=True)
    manual_genres = Column(Text, nullable=True)
    navidrome_genres = Column(Text, nullable=True)
    spotify_genres = Column(Text, nullable=True)
    lastfm_tags = Column(Text, nullable=True)
    listenbrainz_genres = Column(Text, nullable=True)
    discogs_genres = Column(Text, nullable=True)
    musicbrainz_genres = Column(Text, nullable=True)
    essentia_genres = Column(Text, nullable=True)
    tags_last_updated = Column(String, nullable=True)

    # File & tech metadata
    file_path = Column(String, nullable=True)
    duration = Column(Double, nullable=True)
    track_number = Column(String, nullable=True)
    disc_number = Column(String, nullable=True)
    year = Column(String, nullable=True)
    release_year = Column(Integer, nullable=True)
    last_scanned = Column(String, nullable=True)

    # Popularity scores
    spotify_score = Column(Double, nullable=True)
    lastfm_score = Column(Double, nullable=True)
    listenbrainz_score = Column(Double, nullable=True)
    age_score = Column(Double, nullable=True)
    final_score = Column(Double, nullable=True)

    # Star rating
    stars = Column(Integer, nullable=True)
    star_rating = Column(Integer, nullable=True)
    popularity = Column(Double, nullable=True)

    # Single detection
    is_single = Column(Boolean, server_default=text("FALSE"), nullable=True)
    single_confidence = Column(Text, nullable=True)
    single_confidence_score = Column(Double, nullable=True)
    single_status = Column(Text, nullable=True)
    single_sources = Column(Text, nullable=True)
    single_sources_used = Column(Text, nullable=True)
    single_detection_last_updated = Column(DateTime, nullable=True)
    single_manual_override = Column(Boolean, server_default=text("FALSE"), nullable=True)

    # Artist-wide top-10% popularity marking (top 10% of the artist's catalogue)
    popularity_marked = Column(Boolean, server_default=text("FALSE"), nullable=True)

    # Popularity freeze
    popularity_frozen = Column(Boolean, server_default=text("FALSE"), nullable=True)
    popularity_frozen_at = Column(DateTime, nullable=True)

    # MusicBrainz identifiers
    mbid = Column(String, nullable=True)
    suggested_mbid = Column(String, nullable=True)
    musicbrainz_id = Column(String, nullable=True)
    musicbrainz_trackid = Column(String, nullable=True)
    musicbrainz_albumid = Column(String, nullable=True)
    musicbrainz_album_mbid = Column(String, nullable=True)
    musicbrainz_artistid = Column(String, nullable=True)
    musicbrainz_albumartistid = Column(String, nullable=True)
    musicbrainz_releasegroupid = Column(String, nullable=True)
    musicbrainz_releasetrackid = Column(String, nullable=True)
    musicbrainz_workid = Column(String, nullable=True)
    musicbrainz_albumstatus = Column(String, nullable=True)
    musicbrainz_albumtype = Column(String, nullable=True)

    # Writer / ISRC / work
    writer = Column(String, nullable=True)
    isrc = Column(String, nullable=True)
    work = Column(String, nullable=True)

    # Pending MB updates
    pending_mb_updates = Column(Text, nullable=True)
    mb_ignored_fields = Column(Text, nullable=True)

    # Cover detection
    is_cover = Column(BigInteger, server_default=text("0"), nullable=True)
    is_cover_reason = Column(String, nullable=True)
    original_cover_artist = Column(String, nullable=True)
    cover_manual_override = Column(Boolean, server_default=text("FALSE"), nullable=True)

    # Mood / genre classification
    is_live = Column(BigInteger, server_default=text("0"), nullable=True)
    is_acoustic = Column(BigInteger, server_default=text("0"), nullable=True)
    is_remix = Column(BigInteger, server_default=text("0"), nullable=True)
    alternate_take = Column(BigInteger, server_default=text("0"), nullable=True)
    base_track_id = Column(String, nullable=True)
    is_compilation = Column(BigInteger, server_default=text("0"), nullable=True)
    releasecountry = Column(String, nullable=True)
    discogs_artist_id = Column(String, nullable=True)
    recording_mbid = Column(String, nullable=True)
    mood = Column(String, nullable=True)
    mood_confidence = Column(Double, nullable=True)
    mood_source = Column(String, nullable=True)
    mood_last_updated = Column(DateTime, nullable=True)
    danceability = Column(Double, nullable=True)

    # Essentia
    essentia_last_updated = Column(DateTime, nullable=True)
    essentia_model_version = Column(String, nullable=True)
    essentia_scan_version = Column(String, nullable=True)
    bpm = Column(Double, nullable=True)

    # Verification
    verification_status = Column(String, nullable=True)
    verification_checked_at = Column(DateTime, nullable=True)
    verification_error = Column(String, nullable=True)

    def __repr__(self) -> str:
        return f"<Track(id={self.id!r}, title={self.title!r}, artist={self.artist!r})>"


# =============================================================================
# scan_history
# =============================================================================

class ScanHistory(Base):
    __tablename__ = "scan_history"

    id = Column(BigInteger, Sequence("scan_history_id_seq"), primary_key=True)
    scan_type = Column(String, nullable=True)
    artist = Column(String, nullable=True)
    album = Column(String, nullable=True)
    status = Column(String, nullable=True)
    message = Column(String, nullable=True)
    started_at = Column(DateTime, server_default=text("CURRENT_TIMESTAMP"), nullable=True)
    completed_at = Column(DateTime, nullable=True)
    duration_seconds = Column(Double, nullable=True)
    changed_albums = Column(Integer, nullable=True)
    tracks_added = Column(Integer, nullable=True)

    def __repr__(self) -> str:
        return f"<ScanHistory(id={self.id}, artist={self.artist!r}, status={self.status!r})>"


# =============================================================================
# scan_states (New Table for Cross-Process State Tracking)
# =============================================================================

class ScanState(Base):
    __tablename__ = "scan_states"

    scan_type = Column(String, primary_key=True)
    is_running = Column(Boolean, server_default=text("FALSE"), nullable=True)
    status = Column(String, server_default=text("'idle'"), nullable=True)
    stop_requested = Column(Boolean, server_default=text("FALSE"), nullable=True)
    current_artist = Column(String, nullable=True)
    last_scanned_artist = Column(String, nullable=True)
    extra_data = Column(JSONB, server_default=text("'{}'::jsonb"), nullable=True)
    updated_at = Column(
        DateTime(timezone=True), 
        server_default=text("CURRENT_TIMESTAMP"), 
        onupdate=func.now(),
        nullable=True
    )

    def __repr__(self) -> str:
        return f"<ScanState(scan_type={self.scan_type!r}, status={self.status!r})>"


# =============================================================================
# download_queue
# =============================================================================

class DownloadQueue(Base):
    __tablename__ = "download_queue"

    id = Column(BigInteger, Sequence("download_queue_id_seq"), primary_key=True)
    artist = Column(String, nullable=True)
    title = Column(String, nullable=True)
    album = Column(String, nullable=True)
    status = Column(String, server_default=text("'queued'"), nullable=True)
    source = Column(String, server_default=text("'soulseek'"), nullable=True)
    priority = Column(Integer, server_default=text("5"), nullable=True)
    created_at = Column(DateTime, server_default=text("CURRENT_TIMESTAMP"), nullable=True)
    updated_at = Column(DateTime, server_default=text("CURRENT_TIMESTAMP"), nullable=True)

    # Extended columns
    album_artist = Column(String, nullable=True)
    track_number = Column(String, nullable=True)
    disc_number = Column(String, nullable=True)
    year = Column(String, nullable=True)
    release_year = Column(Integer, nullable=True)
    release_id = Column(String, nullable=True)
    release_source = Column(String, nullable=True)
    release_mbid = Column(String, nullable=True)
    recording_mbid = Column(String, nullable=True)
    cover_art_url = Column(String, nullable=True)
    duration = Column(Double, nullable=True)
    found_filename = Column(String, nullable=True)
    file_path = Column(String, nullable=True)
    matched_file_path = Column(String, nullable=True)
    music_file_path = Column(String, nullable=True)
    failure_reason = Column(String, nullable=True)
    retry_count = Column(Integer, server_default=text("0"), nullable=True)
    max_retries = Column(Integer, server_default=text("5"), nullable=True)
    retry_delay_minutes = Column(Integer, server_default=text("30"), nullable=True)
    next_retry_at = Column(DateTime, nullable=True)
    last_failure_time = Column(DateTime, nullable=True)
    imported_at = Column(DateTime, nullable=True)
    copied_individually = Column(Boolean, server_default=text("FALSE"), nullable=True)
    copied_individually_at = Column(DateTime, nullable=True)
    match_confidence = Column(Double, nullable=True)
    match_method = Column(String, nullable=True)
    metadata_data = Column("metadata", JSONB, server_default=text("'{}'::jsonb"), nullable=True)
    metadata_id = Column(BigInteger, nullable=True)
    release_metadata_id = Column(BigInteger, nullable=True)
    collection_track_id = Column(String, nullable=True)
    collection_matched_at = Column(String, nullable=True)
    in_collection = Column(Integer, server_default=text("0"), nullable=True)
    auto_delete_at = Column(DateTime, nullable=True)
    queue_folder = Column(String, nullable=True)
    is_manual_download = Column(Boolean, server_default=text("FALSE"), nullable=True)
    slskd_username = Column(String, nullable=True)
    slskd_transfer_id = Column(String, nullable=True)
    slskd_state = Column(String, nullable=True)
    slskd_queue_position = Column(Integer, nullable=True)
    slskd_last_sync_at = Column(DateTime, nullable=True)
    search_query = Column(String, nullable=True)
    source_id = Column(String, nullable=True)
    status_changed_at = Column(DateTime, server_default=text("CURRENT_TIMESTAMP"), nullable=True)

    def __repr__(self) -> str:
        return f"<DownloadQueue(id={self.id}, title={self.title!r}, status={self.status!r})>"


# =============================================================================
# bookmarks
# =============================================================================

class Bookmark(Base):
    __tablename__ = "bookmarks"

    id = Column(BigInteger, Sequence("bookmarks_id_seq"), primary_key=True)
    bookmark_type = Column(String, nullable=True)
    type = Column(String, nullable=True)
    entity_id = Column(String, nullable=True)
    name = Column(String, nullable=True)
    artist_name = Column(String, nullable=True)
    album_name = Column(String, nullable=True)
    title = Column(String, nullable=True)
    notes = Column(String, nullable=True)
    created_at = Column(DateTime, server_default=text("CURRENT_TIMESTAMP"), nullable=True)

    def __repr__(self) -> str:
        return f"<Bookmark(id={self.id}, name={self.name!r})>"


# =============================================================================
# genre_updates
# =============================================================================

class GenreUpdate(Base):
    __tablename__ = "genre_updates"

    id = Column(BigInteger, Sequence("genre_updates_id_seq"), primary_key=True)
    artist_name = Column(String, nullable=True)
    album_name = Column(String, nullable=True)
    track_id = Column(String, nullable=True)
    genres_before = Column(String, nullable=True)
    genres_after = Column(String, nullable=True)
    action_type = Column(String, nullable=True)
    affected_track_count = Column(Integer, nullable=True)
    change_summary = Column(String, nullable=True)
    created_at = Column(DateTime, server_default=text("CURRENT_TIMESTAMP"), nullable=True)

    def __repr__(self) -> str:
        return f"<GenreUpdate(id={self.id}, action={self.action_type!r})>"


# =============================================================================
# slskd_search_logs
# =============================================================================

class SlskdSearchLog(Base):
    __tablename__ = "slskd_search_logs"

    id = Column(BigInteger, Sequence("slskd_search_logs_id_seq"), primary_key=True)
    created_at = Column(DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP"), nullable=True)
    search_type = Column(String, nullable=True)
    query = Column(String, nullable=True)
    queue_id = Column(Integer, nullable=True)
    artist = Column(String, nullable=True)
    title = Column(String, nullable=True)
    album = Column(String, nullable=True)
    result_count = Column(Integer, server_default=text("0"), nullable=True)
    duration_seconds = Column(Float, nullable=True)
    notes = Column(String, nullable=True)
    selected_result = Column(JSONB, nullable=True)
    results = Column(JSONB, nullable=True)

    def __repr__(self) -> str:
        return f"<SlskdSearchLog(id={self.id}, query={self.query!r})>"


# =============================================================================
# musicbrainz_releases
# =============================================================================

class MusicbrainzRelease(Base):
    __tablename__ = "musicbrainz_releases"

    id = Column(BigInteger, Sequence("musicbrainz_releases_id_seq"), primary_key=True)
    release_id = Column(String, nullable=False, unique=True)
    release_title = Column(String, nullable=False)
    artist = Column(String, nullable=False)
    release_year = Column(Integer, nullable=True)
    total_tracks = Column(Integer, nullable=True)
    monitoring_folder_path = Column(String, nullable=True)
    final_folder_path = Column(String, nullable=True)
    status = Column(String, server_default=text("'active'"), nullable=True)
    method = Column(String, nullable=True)
    discovered_count = Column(Integer, server_default=text("0"), nullable=True)
    organized_count = Column(Integer, server_default=text("0"), nullable=True)
    finalized_count = Column(Integer, server_default=text("0"), nullable=True)
    album_artist = Column(String, nullable=True)
    genres = Column(String, nullable=True)
    cover_art_url = Column(String, nullable=True)
    release_source = Column(String, nullable=True)
    created_at = Column(DateTime, server_default=text("CURRENT_TIMESTAMP"), nullable=True)
    updated_at = Column(DateTime, server_default=text("CURRENT_TIMESTAMP"), nullable=True)
    finalized_at = Column(DateTime, nullable=True)

    tracks = relationship(
        "MusicbrainzReleaseTrack",
        back_populates="release",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<MusicbrainzRelease(id={self.id}, release_id={self.release_id!r})>"


# =============================================================================
# musicbrainz_release_tracks
# =============================================================================

class MusicbrainzReleaseTrack(Base):
    __tablename__ = "musicbrainz_release_tracks"

    id = Column(BigInteger, Sequence("musicbrainz_release_tracks_id_seq"), primary_key=True)
    release_id = Column(String, ForeignKey("musicbrainz_releases.release_id"), nullable=False)
    queue_id = Column(BigInteger, ForeignKey("download_queue.id"), nullable=True)
    disc_number = Column(Integer, nullable=True)
    track_number = Column(Integer, nullable=True)
    track_title = Column(String, nullable=True)
    track_artist = Column(String, nullable=True)
    duration = Column(Integer, nullable=True)
    isrc = Column(String, nullable=True)
    recording_title = Column(String, nullable=True)
    recording_mbid = Column(String, nullable=True)
    composer = Column(String, nullable=True)
    album_artist = Column(String, nullable=True)
    year = Column(String, nullable=True)
    status = Column(String, server_default=text("'queued'"), nullable=True)
    found_filename = Column(String, nullable=True)
    file_path = Column(String, nullable=True)
    created_at = Column(DateTime, server_default=text("CURRENT_TIMESTAMP"), nullable=True)
    updated_at = Column(DateTime, server_default=text("CURRENT_TIMESTAMP"), nullable=True)

    release = relationship("MusicbrainzRelease", back_populates="tracks")

    def __repr__(self) -> str:
        return f"<MusicbrainzReleaseTrack(id={self.id}, title={self.track_title!r})>"


# =============================================================================
# missing_releases
# =============================================================================

class MissingRelease(Base):
    __tablename__ = "missing_releases"

    id = Column(BigInteger, Sequence("missing_releases_id_seq"), primary_key=True)
    artist = Column(String, nullable=False)
    release_id = Column(String, nullable=False)
    title = Column(String, nullable=True)
    primary_type = Column(String, nullable=True)
    first_release_date = Column(String, nullable=True)
    cover_art_url = Column(String, nullable=True)
    category = Column(String, nullable=True)
    last_checked = Column(DateTime, server_default=text("CURRENT_TIMESTAMP"), nullable=True)

    def __repr__(self) -> str:
        return f"<MissingRelease(id={self.id}, release_id={self.release_id!r})>"


# =============================================================================
# album_art
# =============================================================================

class AlbumArt(Base):
    __tablename__ = "album_art"

    id = Column(BigInteger, Sequence("album_art_id_seq"), primary_key=True)
    artist_name = Column(String, nullable=False)
    album_name = Column(String, nullable=False)
    image_data = Column(LargeBinary, nullable=True)
    image_mime_type = Column(String, nullable=True)
    source = Column(String, nullable=True)
    downloaded_at = Column(DateTime, server_default=text("CURRENT_TIMESTAMP"), nullable=True)

    __table_args__ = (
        # Unique constraint matches schema: unique_artist_album
        # SQLAlchemy handles this via ``UniqueConstraint`` if needed at the
        # schema level; for now we rely on the database-side unique index.
    )

    def __repr__(self) -> str:
        return f"<AlbumArt(id={self.id}, artist={self.artist_name!r}, album={self.album_name!r})>"
