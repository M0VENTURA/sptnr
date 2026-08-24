"""Add missing_album_tracks table for persisted missing-track detection.

The album page's "missing tracks" feature (tracks in the MusicBrainz release
that are absent from the library) used to compute the missing list on the fly
each page load, so the list disappeared after a refresh or when the release
MBID changed.  This table persists discovered missing tracks so they survive
page refreshes until they are downloaded (library check clears them) or the
user explicitly rejects them (``ignored = TRUE``).

Revision ID: 009_add_missing_album_tracks
Revises: 008_add_missing_releases_tracklist
Create Date: 2026-08-23
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "009_add_missing_album_tracks"
down_revision: Union[str, None] = "008_add_missing_releases_tracklist"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "missing_album_tracks",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("artist_name", sa.Text(), nullable=False),
        sa.Column("album_name", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("track_number", sa.Text(), nullable=True),
        sa.Column("disc_number", sa.Integer(), nullable=True, server_default="1"),
        sa.Column("track_artist", sa.Text(), nullable=True),
        sa.Column("year", sa.Text(), nullable=True),
        sa.Column("release_id", sa.Text(), nullable=True),
        sa.Column("recording_mbid", sa.Text(), nullable=True),
        sa.Column("duration", sa.Integer(), nullable=True),
        sa.Column("ignored", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=True),
        sa.UniqueConstraint(
            "artist_name", "album_name", "title", "disc_number",
            name="uq_missing_album_tracks",
        ),
    )
    op.create_index(
        "idx_missing_album_tracks_scope",
        "missing_album_tracks",
        ["artist_name", "album_name", "ignored"],
        postgresql_using="btree",
    )


def downgrade() -> None:
    op.drop_index("idx_missing_album_tracks_scope", table_name="missing_album_tracks")
    op.drop_table("missing_album_tracks")