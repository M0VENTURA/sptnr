"""Add upcoming_releases table for Wikipedia-scraped and MusicBrainz release data.

Revision ID: 002_add_upcoming_releases
Revises: 001_initial_schema
Create Date: 2026-07-13
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "002_add_upcoming_releases"
down_revision: Union[str, None] = "001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "upcoming_releases",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("artist_name", sa.Text(), nullable=False),
        sa.Column("album_name", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False, server_default=sa.text("'wikipedia'")),
        sa.Column("release_date", sa.Text(), nullable=True),
        sa.Column("release_group_mbid", sa.Text(), nullable=True),
        sa.Column("match_source", sa.Text(), nullable=True),
        sa.Column("primary_type", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("artist_name", "album_name", "source", name="uq_upcoming_artist_album_source"),
    )
    op.create_index("idx_upcoming_releases_release_date", "upcoming_releases", ["release_date"])


def downgrade() -> None:
    op.drop_index("idx_upcoming_releases_release_date", table_name="upcoming_releases")
    op.drop_table("upcoming_releases")
