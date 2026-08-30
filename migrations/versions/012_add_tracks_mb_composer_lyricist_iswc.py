"""Add tracks columns for MusicBrainz composer/lyricist/iswc metadata.

The MusicBrainz lookup now captures the full metadata checklist:
- ``iswc`` — the underlying composition's ISWC (links the work across
  covers / live versions / acoustic renditions).
- ``lyricist`` — the person who wrote the words (separate from composer).
- ``original_title`` — the ORIGINAL song title when a track is a cover or
  translation (companion to ``original_cover_artist`` which already exists).

Every DDL statement is existence-guarded and dialect-portable, so the chain
also runs cleanly against the SQLite test engine.

Revision ID: 012_add_tracks_mb_composer_lyricist_iswc
Revises: 011_add_tracks_album_artist
Create Date: 2026-08-30
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "012_add_tracks_mb_composer_lyricist_iswc"
down_revision: Union[str, None] = "011_add_tracks_album_artist"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _existing_columns() -> set[str]:
    """Return the current column set of the ``tracks`` table."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {col["name"] for col in inspector.get_columns("tracks")}


def upgrade() -> None:
    existing = _existing_columns()

    if "iswc" not in existing:
        op.add_column("tracks", sa.Column("iswc", sa.Text(), nullable=True))
    if "lyricist" not in existing:
        op.add_column("tracks", sa.Column("lyricist", sa.Text(), nullable=True))
    if "original_title" not in existing:
        op.add_column("tracks", sa.Column("original_title", sa.Text(), nullable=True))


def downgrade() -> None:
    existing = _existing_columns()

    for col in ("original_title", "lyricist", "iswc"):
        if col in existing:
            op.drop_column("tracks", col)
