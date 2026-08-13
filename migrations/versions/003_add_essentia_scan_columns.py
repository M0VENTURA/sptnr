"""Add post-001 Essentia feature columns to tracks.

Databases that applied ``001_initial_schema`` before the rewrite added
``essentia_scan_version`` and ``bpm`` never receive those columns via the
in-place edit (Alembic does not re-run applied revisions).  The Essentia
scanner writes to both, so existing installs fail with
``column "essentia_scan_version" of relation "tracks" does not exist``.

Note: ``001_initial_schema`` ALREADY declares both columns, so a fresh
``upgrade head`` would fail on duplicate-column errors — every add/drop
below is guarded by an inspector existence check to keep the revision
idempotent against either schema generation path.

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


_COLUMNS = [
    ("essentia_scan_version", sa.Text()),
    ("bpm", sa.Double()),
]


def _existing_columns() -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {col["name"] for col in inspector.get_columns("tracks")}


def upgrade() -> None:
    existing = _existing_columns()
    for name, column_type in _COLUMNS:
        if name not in existing:
            op.add_column("tracks", sa.Column(name, column_type, nullable=True))


def downgrade() -> None:
    existing = _existing_columns()
    for name, _column_type in reversed(_COLUMNS):
        if name in existing:
            op.drop_column("tracks", name)
