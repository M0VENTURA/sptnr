"""Add folder_matches table for two-phase (associate → confirm) folder matching.

The downloads page's "Matched Folders" section needs to persist the
folder → MusicBrainz release association WITHOUT moving files.  The first
phase ("Match") writes a row here; the second phase ("Confirm Match")
performs the tag/path/move/cleanup pipeline and removes the row.

Revision ID: 004_add_folder_matches
Revises: 003_add_essentia_scan_columns
Create Date: 2026-08-14
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "004_add_folder_matches"
down_revision: Union[str, None] = "003_add_essentia_scan_columns"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
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
    op.create_index("idx_folder_matches_folder_path", "folder_matches", ["folder_path"])


def downgrade() -> None:
    op.drop_index("idx_folder_matches_folder_path", table_name="folder_matches")
    op.drop_table("folder_matches")
