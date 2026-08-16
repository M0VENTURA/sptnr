"""Add a functional index on the essential-playlist artist predicate.

The Essential Collection .m3u writer and ``_refresh_all_essential_collections``
filter tracks with ``LOWER(TRIM(COALESCE(NULLIF(album_artist, ''), artist)))``.
Since the popularity scan now refreshes an artist's essential collection at the
END of every artist section (including fully-skipped artists), this predicate
runs once per artist in the library — without a matching functional index each
call full-scans ``tracks``.

The index is created ``IF NOT EXISTS`` and matches the expression already added
to ``db/schema.py`` (``INDEXES_TO_ENSURE``), so it is safe on fresh installs
and a no-op where the runtime bootstrap already created it.

Revision ID: 007_essential_playlist_artist_index
Revises: 006_sync_upcoming_releases
Create Date: 2026-08-16
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "007_essential_playlist_artist_index"
down_revision: Union[str, None] = "006_sync_upcoming_releases"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS idx_tracks_album_artist_trim ON tracks "
            "(LOWER(TRIM(COALESCE(NULLIF(album_artist, ''), artist))))"
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS idx_tracks_album_artist_trim"))
