"""Track popularity cache repository.

Stores bulk-fetched Last.fm / ListenBrainz popularity for tracks keyed by
``(artist, title)``.  Non-forced scans read from this table instead of making
per-track API calls, which avoids rate limits and inconsistent data.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from db.engine import db_session

logger = logging.getLogger(__name__)


def get_cached_track_popularity(artist: str, title: str) -> Optional[Dict[str, Any]]:
    """Return a single cached popularity row, or None."""
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT artist, title, lastfm_listeners, lastfm_playcount,
                           listenbrainz_listens, listenbrainz_users, updated_at
                    FROM track_popularity_cache
                    WHERE LOWER(artist) = LOWER(:artist) AND LOWER(title) = LOWER(:title)
                """),
                {"artist": artist, "title": title},
            )
            row = result.fetchone()
            return dict(row._mapping) if row else None
    except Exception as exc:
        logger.error("[track_popularity_cache] get failed for %s / %s: %s", artist, title, exc)
        return None


def get_cached_popularity_for_titles(
    artist: str,
    titles: List[str],
) -> Dict[str, Dict[str, Any]]:
    """Return cached popularity for all given titles of one artist.

    Rows are returned for the whole artist (not just the exact requested
    titles) so feat variants of a song — e.g. a local ``Herzblut`` and a
    cached ``Herzblut (feat. Melissa Bonny)`` — are both fetched; callers
    re-key by their NORMALISED title, which collapses such variants onto
    one entry.  Returns a dict keyed by lowercased title.
    """
    if not titles:
        return {}
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT artist, title, lastfm_listeners, lastfm_playcount,
                           listenbrainz_listens, listenbrainz_users, updated_at
                    FROM track_popularity_cache
                    WHERE LOWER(artist) = LOWER(:artist)
                    ORDER BY lastfm_listeners DESC
                    LIMIT 500
                """),
                {"artist": artist},
            )
            rows = result.fetchall() or []
            out: Dict[str, Dict[str, Any]] = {}
            for row in rows:
                # SQLAlchemy 2.0 Row objects do NOT support string indexing
                # (``row["title"]`` raises ``TypeError: tuple indices must be
                # integers or slices, not str``) — read through ``_mapping``.
                title = row._mapping["title"]
                if title:
                    out[str(title).lower()] = dict(row._mapping)
            return out
    except Exception as exc:
        logger.error("[track_popularity_cache] bulk get failed for %s: %s", artist, exc)
        return {}


def upsert_track_popularity_bulk(rows: List[Dict[str, Any]]) -> int:
    """Upsert popularity rows for one artist.

    Each row: ``artist``, ``title``, optional ``lastfm_listeners``,
    ``lastfm_playcount``, ``listenbrainz_listens``, ``listenbrainz_users``,
    ``source``.  Returns the number of rows written.
    """
    if not rows:
        return 0
    try:
        with db_session() as session:
            for row in rows:
                session.execute(
                    text("""
                        INSERT INTO track_popularity_cache
                            (artist, title, lastfm_listeners, lastfm_playcount,
                             listenbrainz_listens, listenbrainz_users, source, updated_at)
                        VALUES
                            (:artist, :title, :lastfm_listeners, :lastfm_playcount,
                             :listenbrainz_listens, :listenbrainz_users, :source, CURRENT_TIMESTAMP)
                        ON CONFLICT (artist, title) DO UPDATE SET
                            lastfm_listeners = EXCLUDED.lastfm_listeners,
                            lastfm_playcount = EXCLUDED.lastfm_playcount,
                            listenbrainz_listens = EXCLUDED.listenbrainz_listens,
                            listenbrainz_users = EXCLUDED.listenbrainz_users,
                            source = EXCLUDED.source,
                            updated_at = CURRENT_TIMESTAMP
                    """),
                    {
                        "artist": row["artist"],
                        "title": row["title"],
                        "lastfm_listeners": int(row.get("lastfm_listeners") or 0),
                        "lastfm_playcount": int(row.get("lastfm_playcount") or 0),
                        "listenbrainz_listens": int(row.get("listenbrainz_listens") or 0),
                        "listenbrainz_users": int(row.get("listenbrainz_users") or 0),
                        "source": row.get("source") or "bulk",
                    },
                )
            return len(rows)
    except Exception as exc:
        logger.error("[track_popularity_cache] bulk upsert failed for %s: %s", rows[0].get("artist"), exc)
        return 0


import logging
logger = logging.getLogger(__name__)
