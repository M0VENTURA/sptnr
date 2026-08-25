"""Add tracks.album_artist for legacy databases missing the column.

Legacy installs (created by the original system) can carry a bare
``tracks`` table (``id TEXT PRIMARY KEY`` only) with ``alembic_version``
stamped at head.  The bootstrap column-ensure path
(``db.bootstrap._ensure_columns``) may also have been skipped on such
databases, so ``album_artist`` never gets added — every
``COALESCE(NULLIF(album_artist, ''), artist)`` expression in the search
queries then fails with::

    column "album_artist" does not exist at character ...

(the visible symptom: the Search page 500s while the rest of the app keeps
working, because /api/search is the surface that groups by this expression
across the whole tracks table).

This revision converges on either starting state:

- Fresh DB built by 001: the column already exists → no-op.
- Bootstrap-built DB: column added by ``_ensure_columns`` → no-op.
- Legacy bare-tracks DB (the regression): column is added and backfilled
  from ``artist`` so search results keep working without a re-scan.

Every DDL statement is existence-guarded and dialect-portable, so the chain
also runs cleanly against the SQLite test engine.

Revision ID: 011_add_tracks_album_artist
Revises: 010_add_pg_trgm_indexes
Create Date: 2026-08-25
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "011_add_tracks_album_artist"
down_revision: Union[str, None] = "010_add_pg_trgm_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _existing_columns() -> set[str]:
    """Return the current column set of the ``tracks`` table."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return {col["name"] for col in inspector.get_columns("tracks")}


def upgrade() -> None:
    existing = _existing_columns()

    if "album_artist" not in existing:
        op.add_column("tracks", sa.Column("album_artist", sa.Text(), nullable=True))

    # Backfill for the bare-tracks legacy state: album_artist should default
    # to the track artist so search / grouping behaves as before a re-scan.
    # The runtime bootstrap (ensure_album_artist_column_data) performs the
    # same UPDATE; this just converges migration-only builds too.  Guarded on
    # ``artist`` existing: a bootstrap-built table that was never fully
    # column-ensured may hold ONLY ``id``, in which case there is nothing to
    # backfill from (later columns are added by db.bootstrap._ensure_columns).
    if "artist" in existing:
        op.execute("UPDATE tracks SET album_artist = artist WHERE album_artist IS NULL")

    # Recreate the plain btree index used by the bootstrap's
    # INDEXES_TO_ENSURE when the column was missing (guarded, so a DB that
    # already had the column keeps its existing index untouched).
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_tracks_album_artist "
        "ON tracks (album_artist)"
    )


def downgrade() -> None:
    existing = _existing_columns()
    if "album_artist" not in existing:
        return
    op.execute("DROP INDEX IF EXISTS idx_tracks_album_artist")
    op.drop_column("tracks", "album_artist")
