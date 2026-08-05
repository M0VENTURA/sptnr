"""Add post-001 Essentia feature columns to tracks.

Databases that applied ``001_initial_schema`` before the rewrite added
``essentia_scan_version`` and ``bpm`` never receive those columns via the
in-place edit (Alembic does not re-run applied revisions).  The Essentia
scanner writes to both, so existing installs fail with
``column "essentia_scan_version" of relation "tracks" does not exist``.

Revision ID: 003_add_essentia_scan_columns
Revises: 002_add_upcoming_releases
Create Date: 2026-08-05
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "003_add_essentia_scan_columns"
down_revision: Union[str, None] = "002_add_upcoming_releases"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("tracks", sa.Column("essentia_scan_version", sa.Text(), nullable=True))
    op.add_column("tracks", sa.Column("bpm", sa.Double(), nullable=True))


def downgrade() -> None:
    op.drop_column("tracks", "bpm")
    op.drop_column("tracks", "essentia_scan_version")
