"""Add upcoming_releases table for Wikipedia-scraped and MusicBrainz release data.

Revision ID: 002_add_upcoming_releases
Revises: 001_initial_schema
Create Date: 2026-07-13
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from migrations.idempotent import create_index_if_missing, create_table_if_missing

revision: str = "002_add_upcoming_releases"
down_revision: Union[str, None] = "001_initial_schema"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()

    # NOTE: this table must stay in sync with db/schema.py's
    # ``upcoming_releases`` DDL + COLUMN_REGISTRY.  The runtime writers use
    # ``ON CONFLICT (artist_name, album_name)``, so the unique constraint is
    # on exactly those two columns (NOT the three-column artist/album/source
    # pair that an earlier revision shipped).  Existing installs that applied
    # the old minimal table are upgraded by ``006_sync_upcoming_releases".
    #
    # The bootstrap path also creates this table (db.schema.TABLES_TO_ENSURE),
    # so guard against "relation already exists" aborting the chain.
    def _create() -> None:
        op.create_table(
            "upcoming_releases",
            sa.Column("id", sa.BigInteger(), nullable=False),
            sa.Column("artist_name", sa.Text(), nullable=False),
            sa.Column("album_name", sa.Text(), nullable=False),
            sa.Column("source", sa.Text(), nullable=False, server_default=sa.text("'wikipedia'")),
            sa.Column("source_key", sa.Text(), nullable=True),
            sa.Column("release_date", sa.Text(), nullable=True),
            sa.Column("release_year", sa.Integer(), nullable=True),
            sa.Column("artist_in_collection", sa.Boolean(), server_default=sa.text("FALSE"), nullable=True),
            sa.Column("album_in_collection", sa.Boolean(), server_default=sa.text("FALSE"), nullable=True),
            sa.Column("release_group_mbid", sa.Text(), nullable=True),
            sa.Column("match_source", sa.Text(), nullable=True),
            sa.Column("primary_type", sa.Text(), nullable=True),
            sa.Column("mbid_match_status", sa.Text(), server_default=sa.text("'unmatched'"), nullable=True),
            sa.Column("mbid_source", sa.Text(), nullable=True),
            sa.Column("mbid_confidence", sa.Text(), nullable=True),
            sa.Column("mbid_match_score", sa.Float(), nullable=True),
            sa.Column("mbid_last_checked_at", sa.Text(), nullable=True),
            sa.Column("mbid_manual_override", sa.Boolean(), server_default=sa.text("FALSE"), nullable=True),
            sa.Column("candidate_release_group_mbid", sa.Text(), nullable=True),
            sa.Column("status", sa.Text(), server_default=sa.text("'discovered'"), nullable=True),
            sa.Column("last_seen_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("artist_name", "album_name", name="uq_upcoming_artist_album"),
        )

    created = create_table_if_missing(bind, "upcoming_releases", _create)

    if created:
        op.create_index(
            "idx_upcoming_releases_release_date",
            "upcoming_releases",
            ["release_date"],
        )
    else:
        # Table came from the bootstrap path — still ensure the index.
        create_index_if_missing(
            bind,
            "idx_upcoming_releases_release_date",
            "upcoming_releases",
            "CREATE INDEX idx_upcoming_releases_release_date ON upcoming_releases (release_date)",
        )


def downgrade() -> None:
    op.drop_index("idx_upcoming_releases_release_date", table_name="upcoming_releases")
    op.drop_table("upcoming_releases")