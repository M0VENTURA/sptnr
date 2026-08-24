"""Add folder_matches table for two-phase (associate → confirm) folder matching.

The downloads page's "Matched Folders" section needs to persist the
folder → MusicBrainz release association WITHOUT moving files.  The first
phase ("Match") writes a row here; the second phase ("Confirm Match")
performs the tag/path/move/cleanup pipeline and removes the row.

Idempotent against the runtime schema bootstrap, which creates this same
table from ``db.schema.TABLES_TO_ENSURE`` — a bare ``CREATE TABLE`` aborted
the chain with ``relation already exists`` on bootstrap-built databases.

Revision ID: 004_add_folder_matches
Revises: 003_add_essentia_scan_columns
Create Date: 2026-08-14
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from migrations.idempotent import create_index_if_missing, create_table_if_missing

revision: str = "004_add_folder_matches"
down_revision: Union[str, None] = "003_add_essentia_scan_columns"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()

    def _create() -> None:
        op.create_table(
            "folder_matches",
            sa.Column("id", sa.BigInteger(), nullable=False),
            sa.Column("folder_path", sa.Text(), nullable=False),
            sa.Column("release_mbid", sa.Text(), nullable=False),
            sa.Column("release_title", sa.Text(), nullable=True),
            sa.Column("artist", sa.Text(), nullable=True),
            sa.Column("release_year", sa.Integer(), nullable=True),
            sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'matched'")),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("folder_path", name="uq_folder_matches_folder_path"),
        )

    created = create_table_if_missing(bind, "folder_matches", _create)

    if created:
        op.create_index("idx_folder_matches_folder_path", "folder_matches", ["folder_path"])
    else:
        create_index_if_missing(
            bind,
            "idx_folder_matches_folder_path",
            "folder_matches",
            "CREATE INDEX idx_folder_matches_folder_path ON folder_matches (folder_path)",
        )


def downgrade() -> None:
    op.drop_index("idx_folder_matches_folder_path", table_name="folder_matches")
    op.drop_table("folder_matches")