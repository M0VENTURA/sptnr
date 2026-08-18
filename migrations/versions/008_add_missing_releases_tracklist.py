"""Add tracklist column to missing_releases.

``services.popularity.release_cache_service.populate_missing_release_tracklists``
reads/writes ``missing_releases.tracklist`` (a JSON-encoded list of track
titles) so the download queue can match a missing release's tracks.  The
column was never added to the schema, so every popularity scan hit
``ERROR: column "tracklist" does not exist`` — the exception was caught and
logged at DEBUG, but it also surfaced as a "futures unfinished" symptom when
the prefetch stage's bounded call / future collection misbehaved.

Revision ID: 008_add_missing_releases_tracklist
Revises: 007_essential_playlist_artist_index
Create Date: 2026-08-18
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "008_add_missing_releases_tracklist"
down_revision: Union[str, None] = "007_essential_playlist_artist_index"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _existing_columns() -> set[str]:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    try:
        cols = inspector.get_columns("missing_releases")
        return {c["name"] for c in cols}
    except Exception:
        # Table may not exist yet (fresh install) — nothing to migrate.
        return set()


def upgrade() -> None:
    if "tracklist" not in _existing_columns():
        op.execute(
            sa.text("ALTER TABLE missing_releases ADD COLUMN IF NOT EXISTS tracklist TEXT")
        )


def downgrade() -> None:
    if "tracklist" in _existing_columns():
        op.execute(sa.text("ALTER TABLE missing_releases DROP COLUMN IF EXISTS tracklist"))
