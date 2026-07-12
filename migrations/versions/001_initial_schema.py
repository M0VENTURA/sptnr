"""Initial schema — matches the existing ``db/schema.py`` definitions.

This migration can be applied to a fresh database to create all tables, or
stamped on an existing database with ``alembic stamp head`` to mark the
schema as up-to-date without running DDL.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # artists
    # ------------------------------------------------------------------
    op.create_table(
        "artists",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    # ------------------------------------------------------------------
    # artist_stats
    # ------------------------------------------------------------------
    op.create_table(
        "artist_stats",
        sa.Column("artist_id", sa.Text(), nullable=False),
        sa.Column("artist_name", sa.Text(), nullable=False),
        sa.Column("album_count", sa.Integer(), nullable=True),
        sa.Column("track_count", sa.Integer(), nullable=True),
        sa.Column("last_updated", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("artist_id"),
    )

    # ------------------------------------------------------------------
    # tracks
    # ------------------------------------------------------------------
    op.create_table(
        "tracks",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("artist_id", sa.Text(), nullable=True),
        sa.Column("artist", sa.Text(), nullable=True),
        sa.Column("album_artist", sa.Text(), nullable=True),
        sa.Column("album", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("genres", sa.Text(), nullable=True),
        sa.Column("genre", sa.Text(), nullable=True),
        sa.Column("manual_genres", sa.Text(), nullable=True),
        sa.Column("navidrome_genres", sa.Text(), nullable=True),
        sa.Column("spotify_genres", sa.Text(), nullable=True),
        sa.Column("lastfm_tags", sa.Text(), nullable=True),
        sa.Column("listenbrainz_genres", sa.Text(), nullable=True),
        sa.Column("discogs_genres", sa.Text(), nullable=True),
        sa.Column("musicbrainz_genres", sa.Text(), nullable=True),
        sa.Column("tags_last_updated", sa.Text(), nullable=True),
        sa.Column("file_path", sa.Text(), nullable=True),
        sa.Column("duration", sa.Float(), nullable=True),
        sa.Column("track_number", sa.Text(), nullable=True),
        sa.Column("disc_number", sa.Text(), nullable=True),
        sa.Column("year", sa.Text(), nullable=True),
        sa.Column("release_year", sa.Integer(), nullable=True),
        sa.Column("last_scanned", sa.Text(), nullable=True),
        sa.Column("spotify_score", sa.Float(), nullable=True),
        sa.Column("lastfm_score", sa.Float(), nullable=True),
        sa.Column("listenbrainz_score", sa.Float(), nullable=True),
        sa.Column("age_score", sa.Float(), nullable=True),
        sa.Column("final_score", sa.Float(), nullable=True),
        sa.Column("stars", sa.Integer(), nullable=True),
        sa.Column("star_rating", sa.Integer(), nullable=True),
        sa.Column("popularity", sa.Float(), nullable=True),
        sa.Column("is_single", sa.Boolean(), server_default=sa.text("FALSE"), nullable=True),
        sa.Column("single_confidence", sa.Float(), nullable=True),
        sa.Column("popularity_frozen", sa.Boolean(), server_default=sa.text("FALSE"), nullable=True),
        sa.Column("popularity_frozen_at", sa.DateTime(), nullable=True),
        sa.Column("mbid", sa.Text(), nullable=True),
        sa.Column("suggested_mbid", sa.Text(), nullable=True),
        sa.Column("musicbrainz_id", sa.Text(), nullable=True),
        sa.Column("musicbrainz_trackid", sa.Text(), nullable=True),
        sa.Column("musicbrainz_albumid", sa.Text(), nullable=True),
        sa.Column("musicbrainz_album_mbid", sa.Text(), nullable=True),
        sa.Column("musicbrainz_artistid", sa.Text(), nullable=True),
        sa.Column("musicbrainz_albumartistid", sa.Text(), nullable=True),
        sa.Column("musicbrainz_releasegroupid", sa.Text(), nullable=True),
        sa.Column("musicbrainz_releasetrackid", sa.Text(), nullable=True),
        sa.Column("musicbrainz_workid", sa.Text(), nullable=True),
        sa.Column("musicbrainz_albumstatus", sa.Text(), nullable=True),
        sa.Column("musicbrainz_albumtype", sa.Text(), nullable=True),
        sa.Column("writer", sa.Text(), nullable=True),
        sa.Column("isrc", sa.Text(), nullable=True),
        sa.Column("work", sa.Text(), nullable=True),
        sa.Column("pending_mb_updates", sa.Text(), nullable=True),
        sa.Column("mb_ignored_fields", sa.Text(), nullable=True),
        sa.Column("is_cover", sa.BigInteger(), server_default=sa.text("0"), nullable=True),
        sa.Column("is_cover_reason", sa.Text(), nullable=True),
        sa.Column("original_cover_artist", sa.Text(), nullable=True),
        sa.Column("cover_manual_override", sa.Boolean(), server_default=sa.text("FALSE"), nullable=True),
        sa.Column("is_live", sa.BigInteger(), server_default=sa.text("0"), nullable=True),
        sa.Column("is_acoustic", sa.BigInteger(), server_default=sa.text("0"), nullable=True),
        sa.Column("is_remix", sa.BigInteger(), server_default=sa.text("0"), nullable=True),
        sa.Column("mood", sa.Text(), nullable=True),
        sa.Column("mood_confidence", sa.Float(), nullable=True),
        sa.Column("mood_source", sa.Text(), nullable=True),
        sa.Column("mood_last_updated", sa.DateTime(), nullable=True),
        sa.Column("danceability", sa.Float(), nullable=True),
        sa.Column("essentia_last_updated", sa.DateTime(), nullable=True),
        sa.Column("essentia_model_version", sa.Text(), nullable=True),
        sa.Column("verification_status", sa.Text(), nullable=True),
        sa.Column("verification_checked_at", sa.DateTime(), nullable=True),
        sa.Column("verification_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    # ------------------------------------------------------------------
    # scan_history
    # ------------------------------------------------------------------
    op.create_table(
        "scan_history",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("scan_type", sa.Text(), nullable=True),
        sa.Column("artist", sa.Text(), nullable=True),
        sa.Column("album", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("changed_albums", sa.Integer(), nullable=True),
        sa.Column("tracks_added", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    # ------------------------------------------------------------------
    # download_queue
    # ------------------------------------------------------------------
    op.create_table(
        "download_queue",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("artist", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("album", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'queued'"), nullable=True),
        sa.Column("source", sa.Text(), server_default=sa.text("'soulseek'"), nullable=True),
        sa.Column("priority", sa.Integer(), server_default=sa.text("5"), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=True),
        sa.Column("album_artist", sa.Text(), nullable=True),
        sa.Column("track_number", sa.Text(), nullable=True),
        sa.Column("disc_number", sa.Text(), nullable=True),
        sa.Column("year", sa.Text(), nullable=True),
        sa.Column("release_year", sa.Integer(), nullable=True),
        sa.Column("release_id", sa.Text(), nullable=True),
        sa.Column("release_source", sa.Text(), nullable=True),
        sa.Column("release_mbid", sa.Text(), nullable=True),
        sa.Column("recording_mbid", sa.Text(), nullable=True),
        sa.Column("cover_art_url", sa.Text(), nullable=True),
        sa.Column("duration", sa.Float(), nullable=True),
        sa.Column("found_filename", sa.Text(), nullable=True),
        sa.Column("file_path", sa.Text(), nullable=True),
        sa.Column("matched_file_path", sa.Text(), nullable=True),
        sa.Column("music_file_path", sa.Text(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column("max_retries", sa.Integer(), server_default=sa.text("5"), nullable=True),
        sa.Column("retry_delay_minutes", sa.Integer(), server_default=sa.text("30"), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(), nullable=True),
        sa.Column("last_failure_time", sa.DateTime(), nullable=True),
        sa.Column("imported_at", sa.DateTime(), nullable=True),
        sa.Column("copied_individually", sa.Boolean(), server_default=sa.text("FALSE"), nullable=True),
        sa.Column("copied_individually_at", sa.DateTime(), nullable=True),
        sa.Column("match_confidence", sa.Float(), nullable=True),
        sa.Column("match_method", sa.Text(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=True),
        sa.Column("metadata_id", sa.BigInteger(), nullable=True),
        sa.Column("release_metadata_id", sa.BigInteger(), nullable=True),
        sa.Column("collection_track_id", sa.Text(), nullable=True),
        sa.Column("collection_matched_at", sa.Text(), nullable=True),
        sa.Column("in_collection", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column("auto_delete_at", sa.DateTime(), nullable=True),
        sa.Column("queue_folder", sa.Text(), nullable=True),
        sa.Column("is_manual_download", sa.Boolean(), server_default=sa.text("FALSE"), nullable=True),
        sa.Column("slskd_username", sa.Text(), nullable=True),
        sa.Column("slskd_transfer_id", sa.Text(), nullable=True),
        sa.Column("slskd_state", sa.Text(), nullable=True),
        sa.Column("slskd_queue_position", sa.Integer(), nullable=True),
        sa.Column("slskd_last_sync_at", sa.DateTime(), nullable=True),
        sa.Column("search_query", sa.Text(), nullable=True),
        sa.Column("source_id", sa.Text(), nullable=True),
        sa.Column("status_changed_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    # ------------------------------------------------------------------
    # bookmarks
    # ------------------------------------------------------------------
    op.create_table(
        "bookmarks",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("bookmark_type", sa.Text(), nullable=True),
        sa.Column("type", sa.Text(), nullable=True),
        sa.Column("entity_id", sa.Text(), nullable=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("artist_name", sa.Text(), nullable=True),
        sa.Column("album_name", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    # ------------------------------------------------------------------
    # genre_updates
    # ------------------------------------------------------------------
    op.create_table(
        "genre_updates",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("artist_name", sa.Text(), nullable=True),
        sa.Column("album_name", sa.Text(), nullable=True),
        sa.Column("track_id", sa.Text(), nullable=True),
        sa.Column("genres_before", sa.Text(), nullable=True),
        sa.Column("genres_after", sa.Text(), nullable=True),
        sa.Column("action_type", sa.Text(), nullable=True),
        sa.Column("affected_track_count", sa.Integer(), nullable=True),
        sa.Column("change_summary", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    # ------------------------------------------------------------------
    # slskd_search_logs
    # ------------------------------------------------------------------
    op.create_table(
        "slskd_search_logs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=True),
        sa.Column("search_type", sa.Text(), nullable=True),
        sa.Column("query", sa.Text(), nullable=True),
        sa.Column("queue_id", sa.Integer(), nullable=True),
        sa.Column("artist", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("album", sa.Text(), nullable=True),
        sa.Column("result_count", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("selected_result", postgresql.JSONB(), nullable=True),
        sa.Column("results", postgresql.JSONB(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    # ------------------------------------------------------------------
    # musicbrainz_releases
    # ------------------------------------------------------------------
    op.create_table(
        "musicbrainz_releases",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("release_id", sa.Text(), nullable=False),
        sa.Column("release_title", sa.Text(), nullable=False),
        sa.Column("artist", sa.Text(), nullable=False),
        sa.Column("release_year", sa.Integer(), nullable=True),
        sa.Column("total_tracks", sa.Integer(), nullable=True),
        sa.Column("monitoring_folder_path", sa.Text(), nullable=True),
        sa.Column("final_folder_path", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'active'"), nullable=True),
        sa.Column("method", sa.Text(), nullable=True),
        sa.Column("discovered_count", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column("organized_count", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column("finalized_count", sa.Integer(), server_default=sa.text("0"), nullable=True),
        sa.Column("album_artist", sa.Text(), nullable=True),
        sa.Column("genres", sa.Text(), nullable=True),
        sa.Column("cover_art_url", sa.Text(), nullable=True),
        sa.Column("release_source", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=True),
        sa.Column("finalized_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("release_id", name="uq_musicbrainz_releases_release_id"),
    )

    # ------------------------------------------------------------------
    # musicbrainz_release_tracks
    # ------------------------------------------------------------------
    op.create_table(
        "musicbrainz_release_tracks",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("release_id", sa.Text(), nullable=False),
        sa.Column("queue_id", sa.BigInteger(), nullable=True),
        sa.Column("disc_number", sa.Integer(), nullable=True),
        sa.Column("track_number", sa.Integer(), nullable=True),
        sa.Column("track_title", sa.Text(), nullable=True),
        sa.Column("track_artist", sa.Text(), nullable=True),
        sa.Column("duration", sa.Integer(), nullable=True),
        sa.Column("isrc", sa.Text(), nullable=True),
        sa.Column("recording_title", sa.Text(), nullable=True),
        sa.Column("recording_mbid", sa.Text(), nullable=True),
        sa.Column("composer", sa.Text(), nullable=True),
        sa.Column("album_artist", sa.Text(), nullable=True),
        sa.Column("year", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'queued'"), nullable=True),
        sa.Column("found_filename", sa.Text(), nullable=True),
        sa.Column("file_path", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=True),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["release_id"], ["musicbrainz_releases.release_id"]),
        sa.ForeignKeyConstraint(["queue_id"], ["download_queue.id"]),
    )

    # ------------------------------------------------------------------
    # missing_releases
    # ------------------------------------------------------------------
    op.create_table(
        "missing_releases",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("artist", sa.Text(), nullable=False),
        sa.Column("release_id", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("primary_type", sa.Text(), nullable=True),
        sa.Column("first_release_date", sa.Text(), nullable=True),
        sa.Column("cover_art_url", sa.Text(), nullable=True),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column("last_checked", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    # ------------------------------------------------------------------
    # album_art
    # ------------------------------------------------------------------
    op.create_table(
        "album_art",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("artist_name", sa.Text(), nullable=False),
        sa.Column("album_name", sa.Text(), nullable=False),
        sa.Column("image_data", sa.LargeBinary(), nullable=True),
        sa.Column("image_mime_type", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("downloaded_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("artist_name", "album_name", name="unique_artist_album"),
    )

    # ------------------------------------------------------------------
    # Indexes
    # ------------------------------------------------------------------
    op.create_index("idx_tracks_artist", "tracks", ["artist"])
    op.create_index("idx_tracks_album_artist", "tracks", ["album_artist"])
    op.create_index("idx_tracks_album", "tracks", ["album"])
    op.create_index("idx_tracks_stars", "tracks", ["stars"])
    op.create_index("idx_tracks_final_score", "tracks", ["final_score"])
    op.create_index("idx_tracks_is_single", "tracks", ["is_single"])
    op.create_index("idx_tracks_file_path", "tracks", ["file_path"])
    op.create_index("idx_download_queue_status", "download_queue", ["status"])
    op.create_index("idx_download_queue_artist", "download_queue", ["artist"])
    op.create_index("idx_scan_history_started_at", "scan_history", ["started_at"])
    op.create_index("idx_scan_history_artist", "scan_history", ["artist"])
    op.create_index("idx_musicbrainz_releases_artist", "musicbrainz_releases", ["artist"])
    op.create_index("idx_missing_releases_artist", "missing_releases", ["artist"])

    # ------------------------------------------------------------------
    # Status-changed trigger for download_queue
    # ------------------------------------------------------------------
    op.execute("""
        CREATE OR REPLACE FUNCTION fn_dq_status_changed_at() RETURNS TRIGGER
        LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.status IS DISTINCT FROM OLD.status THEN
                NEW.status_changed_at = CURRENT_TIMESTAMP;
            END IF;
            RETURN NEW;
        END; $$;
    """)
    op.execute("""
        CREATE TRIGGER trg_dq_status_changed_at
            BEFORE UPDATE ON download_queue
            FOR EACH ROW
            EXECUTE FUNCTION fn_dq_status_changed_at();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_dq_status_changed_at ON download_queue")
    op.execute("DROP FUNCTION IF EXISTS fn_dq_status_changed_at()")

    op.drop_table("album_art")
    op.drop_table("missing_releases")
    op.drop_table("musicbrainz_release_tracks")
    op.drop_table("musicbrainz_releases")
    op.drop_table("slskd_search_logs")
    op.drop_table("genre_updates")
    op.drop_table("bookmarks")
    op.drop_table("download_queue")
    op.drop_table("scan_history")
    op.drop_table("tracks")
    op.drop_table("artist_stats")
    op.drop_table("artists")
