"""Sync upcoming_releases to the canonical schema.

Existing installs that applied the ORIGINAL ``002_add_upcoming_releases``
revision created a minimal ``upcoming_releases`` table (a handful of columns
plus the three-column unique constraint ``uq_upcoming_artist_album_source``
on ``(artist_name, album_name, source)``).  The runtime writers use many more
columns and ``ON CONFLICT (artist_name, album_name)``, which requires a
unique index on exactly those two columns.  ``db.bootstrap`` repairs the
table at runtime, but a fresh migration-only build produced the wrong schema.

This revision upgrades an existing (minimal) table to the canonical schema
matching ``db/schema.py``.  It is written idempotently so it is also a safe
no-op on fresh installs that applied the updated ``002`` (which already
creates the full table).

Revision ID: 006_sync_upcoming_releases
Revises: 005_add_user_favourites
Create Date: 2026-08-14
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "006_sync_upcoming_releases"
down_revision: Union[str, None] = "005_add_user_favourites"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()

    # The ADD COLUMN IF NOT EXISTS / DROP CONSTRAINT syntax below is
    # PostgreSQL-only.  On other dialects (SQLite test suite) the fresh
    # install path already created the full canonical table via the updated
    # ``002`` revision, so there is nothing to sync.
    if bind.dialect.name != "postgresql":
        return

    # ── 1. Add the columns the runtime writers rely on ──────────────────
    # Mirrors db/schema.py's upcoming_releases DDL + COLUMN_REGISTRY.
    # ADD COLUMN IF NOT EXISTS makes this safe on fresh installs where the
    # updated 002 already created these columns.
    _ensure_columns = [
        ("source_key", "TEXT"),
        ("release_year", "INTEGER"),
        ("artist_in_collection", "BOOLEAN DEFAULT FALSE"),
        ("album_in_collection", "BOOLEAN DEFAULT FALSE"),
        ("mbid_match_status", "TEXT DEFAULT 'unmatched'"),
        ("mbid_source", "TEXT"),
        ("mbid_confidence", "TEXT"),
        ("mbid_match_score", "REAL"),
        ("mbid_last_checked_at", "TEXT"),
        ("mbid_manual_override", "BOOLEAN DEFAULT FALSE"),
        ("candidate_release_group_mbid", "TEXT"),
        ("status", "TEXT DEFAULT 'discovered'"),
        ("last_seen_at", "TIMESTAMP"),
        ("updated_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
    ]
    for column_name, column_ddl in _ensure_columns:
        bind.execute(
            sa.text(
                f"ALTER TABLE upcoming_releases ADD COLUMN IF NOT EXISTS {column_name} {column_ddl}"
            )
        )

    # ── 2. Dedupe before swapping the unique key ─────────────────────────
    # The old three-column constraint allowed one row per (artist, album,
    # source), so the same album could exist as both a Wikipedia row and a
    # MusicBrainz row.  Collapse to per-album identity the same way
    # db.bootstrap.ensure_upcoming_releases_schema does: MusicBrainz rows win
    # (they carry the authoritative MBID), older same-source duplicates drop.
    bind.execute(sa.text("""
        DELETE FROM upcoming_releases a
        USING upcoming_releases b
        WHERE a.id <> b.id
          AND a.artist_name = b.artist_name
          AND a.album_name = b.album_name
          AND a.source <> 'MusicBrainz Daily Collection'
          AND b.source = 'MusicBrainz Daily Collection'
    """))
    bind.execute(sa.text("""
        DELETE FROM upcoming_releases a
        USING upcoming_releases b
        WHERE a.id > b.id
          AND a.artist_name = b.artist_name
          AND a.album_name = b.album_name
          AND a.source = b.source
    """))

    # ── 3. Swap the unique constraint to (artist_name, album_name) ───────
    bind.execute(
        sa.text(
            "ALTER TABLE upcoming_releases "
            "DROP CONSTRAINT IF EXISTS uq_upcoming_artist_album_source"
        )
    )
    bind.execute(
        sa.text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_upcoming_artist_album "
            "ON upcoming_releases (artist_name, album_name)"
        )
    )

    # ── 4. Backfill last_seen_at for rows with no visibility ─────────────
    bind.execute(sa.text("""
        UPDATE upcoming_releases
        SET last_seen_at = COALESCE(last_seen_at, updated_at, created_at, CURRENT_TIMESTAMP)
    """))


def downgrade() -> None:
    # Best-effort reverse: drop the two-column unique index and restore the
    # three-column constraint.  Column removals are intentionally NOT
    # performed (dropping columns would destroy data on a real downgrade).
    bind = op.get_bind()
    bind.execute(sa.text("DROP INDEX IF EXISTS uq_upcoming_artist_album"))
    bind.execute(
        sa.text(
            "ALTER TABLE upcoming_releases ADD CONSTRAINT uq_upcoming_artist_album_source "
            "UNIQUE (artist_name, album_name, source)"
        )
    )
