"""Enable pg_trgm trigram fuzzy-search indexes on tracks text columns.

Adds the Postgres ``pg_trgm`` extension and GIN trigram indexes on the
library's most-searched text columns (artist, album_artist, album, title).
This makes the dashboard's /api/search typo-tolerant and turns the previous
``LIKE '%query%'`` full-table scans into indexed lookups — a 40,000+ track
library returns results in milliseconds instead of scanning every row.

The extension is created with ``CREATE EXTENSION IF NOT EXISTS`` (idempotent)
and the indexes with ``CREATE INDEX IF NOT EXISTS`` so a partial or repeated
boot never fails with "already exists".  Index creation is NOT concurrent
(boot-time only, one worker due to the migration advisory lock in
``db.engine``); a ``CONCURRENTLY`` build is unnecessary here.

Revision ID: 010_add_pg_trgm_indexes
Revises: 009_add_missing_album_tracks
Create Date: 2026-08-23
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "010_add_pg_trgm_indexes"
down_revision: Union[str, None] = "009_add_missing_album_tracks"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # pg_trgm is a Postgres-only extension; other dialects must skip it.
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # A legacy database (created by the original system) can carry a BARE
    # ``tracks`` table (``id`` only) — none of the text columns exist yet.
    # Indexing a missing column aborts ``alembic upgrade head`` HERE, which
    # blocks every later revision (including 011, which adds album_artist)
    # from ever running — the exact "rebuilt container still broken" failure.
    # Guard each index on its column existing so the chain converges on ANY
    # starting state; the columns are added by the bootstrap's
    # ``_ensure_columns`` and/or revision 011.
    existing = {col["name"] for col in sa.inspect(bind).get_columns("tracks")}

    # GIN trigram indexes power both fuzzy (%, <->) and indexed ILIKE '%q%'
    # lookups.  artist and album_artist are indexed separately so searches
    # that fall back from album_artist to artist stay fast.
    if "artist" in existing:
        op.execute(
            "CREATE INDEX IF NOT EXISTS idx_tracks_trgm_artist "
            "ON tracks USING GIN (artist gin_trgm_ops)"
        )
    if "album_artist" in existing:
        op.execute(
            "CREATE INDEX IF NOT EXISTS idx_tracks_trgm_album_artist "
            "ON tracks USING GIN (album_artist gin_trgm_ops)"
        )
    if "album" in existing:
        op.execute(
            "CREATE INDEX IF NOT EXISTS idx_tracks_trgm_album "
            "ON tracks USING GIN (album gin_trgm_ops)"
        )
    if "title" in existing:
        op.execute(
            "CREATE INDEX IF NOT EXISTS idx_tracks_trgm_title "
            "ON tracks USING GIN (title gin_trgm_ops)"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute("DROP INDEX IF EXISTS idx_tracks_trgm_title")
    op.execute("DROP INDEX IF EXISTS idx_tracks_trgm_album")
    op.execute("DROP INDEX IF EXISTS idx_tracks_trgm_album_artist")
    op.execute("DROP INDEX IF EXISTS idx_tracks_trgm_artist")
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
