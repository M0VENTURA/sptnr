"""Popularity statistics and eligibility helpers."""

from __future__ import annotations
import logging
from statistics import mean, median, stdev

from services.catalog.album_classification_service import (
    should_exclude_track_from_stats
)
from services.popularity.popularity_math import calculate_track_zscore

logger = logging.getLogger(__name__)


def calculate_album_stats(conn, artist: str, album: str) -> tuple[float, float, list[float]]:
    """Return (mean, stdev, values) of popularity scores for an album.

    When ``conn`` is ``None`` a fresh ``db_session`` is opened so callers
    (e.g. the single-detection service) never crash on ``None.cursor()``.
    """
    values: list[float] = []
    if conn is None:
        from sqlalchemy import text as _text
        from db.engine import db_session as _db_session
        try:
            with _db_session() as session:
                result = session.execute(
                    _text("""
                        SELECT final_score FROM tracks
                        WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
                          AND album = :album
                          AND final_score > 0
                    """),
                    {"artist": artist, "album": album},
                )
                values = [float(row[0] or 0) for row in result.fetchall() or []]
        except Exception as exc:
            logger.debug("[POPULARITY_STATS] Album stats session error for %s - %s: %s", artist, album, exc)
            return 0.0, 0.0, []
    else:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT final_score FROM tracks
            WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s
              AND album = %s
              AND final_score > 0
            """,
            (artist, album),
        )
        values = [float(row[0] or 0) for row in cursor.fetchall() or []]

    if not values:
        logger.debug("[POPULARITY_STATS] No valid score tracks found for album: %s - %s", artist, album)
        return 0.0, 0.0, []

    avg = mean(values)
    sd = stdev(values) if len(values) > 1 else 0.0
    logger.debug("[POPULARITY_STATS] Album stats for %s - %s: mean=%.1f, stdev=%.1f (count=%d)", artist, album, avg, sd, len(values))

    return avg, sd, values


def calculate_artist_stats(conn, artist: str) -> tuple[float, float, list[float]]:
    """Return (mean, stdev, values) of popularity scores for an artist.

    When ``conn`` is ``None`` a fresh ``db_session`` is opened so callers
    (e.g. the single-detection service) never crash on ``None.cursor()``.
    """
    values: list[float] = []
    if conn is None:
        from sqlalchemy import text as _text
        from db.engine import db_session as _db_session
        try:
            with _db_session() as session:
                result = session.execute(
                    _text("""
                        SELECT final_score FROM tracks
                        WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
                          AND final_score > 0
                    """),
                    {"artist": artist},
                )
                values = [float(row[0] or 0) for row in result.fetchall() or []]
        except Exception as exc:
            logger.debug("[POPULARITY_STATS] Artist stats session error for %s: %s", artist, exc)
            return 0.0, 0.0, []
    else:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT final_score FROM tracks
            WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s
              AND final_score > 0
            """,
            (artist,),
        )
        values = [float(row[0] or 0) for row in cursor.fetchall() or []]

    if not values:
        logger.debug("[POPULARITY_STATS] No valid score tracks found for artist: %s", artist)
        return 0.0, 0.0, []

    avg = mean(values)
    sd = stdev(values) if len(values) > 1 else 0.0
    logger.debug("[POPULARITY_STATS] Artist stats for %s: mean=%.1f, stdev=%.1f (count=%d)", artist, avg, sd, len(values))

    return avg, sd, values


def calculate_artist_popularity_stats(artist_name: str, conn) -> dict:
    avg, sd, values = calculate_artist_stats(conn, artist_name)

    return {
        "mean": avg,
        "median": median(values) if values else 0.0,
        "stdev": sd,
        "count": len(values),
        "max": max(values) if values else 0.0,
    }


def calculate_album_listener_stats(conn, artist: str, album: str) -> tuple[list[float], list[float]]:
    """Return ``(lastfm_listeners[], listenbrainz_listens[])`` for an album.

    Raw listener/listen counts per album track, used to build the album-local
    LF/LB distributions for single-detection's composite z-score.  Only
    positive counts are included (missing data is not signal).  When ``conn``
    is ``None`` a fresh ``db_session`` is opened so callers (e.g. the
    single-detection service) never crash on ``None.cursor()``.
    """
    lf_listeners: list[float] = []
    lb_listens: list[float] = []
    rows = []
    if conn is None:
        from sqlalchemy import text as _text
        from db.engine import db_session as _db_session
        try:
            with _db_session() as session:
                result = session.execute(
                    _text("""
                        SELECT lastfm_listeners, listenbrainz_listens FROM tracks
                        WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
                          AND album = :album
                    """),
                    {"artist": artist, "album": album},
                )
                rows = result.fetchall() or []
        except Exception as exc:
            logger.debug("[POPULARITY_STATS] Album listener session error for %s - %s: %s", artist, album, exc)
            return [], []
    else:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT lastfm_listeners, listenbrainz_listens FROM tracks
            WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s
              AND album = %s
            """,
            (artist, album),
        )
        rows = cursor.fetchall() or []

    for row in rows:
        _lf = float(row[0] or 0)
        _lb = float(row[1] or 0)
        if _lf > 0:
            lf_listeners.append(_lf)
        if _lb > 0:
            lb_listens.append(_lb)
    return lf_listeners, lb_listens


def should_exclude_from_stats(tracks_with_scores, alternate_takes_map: dict | None = None):
    """Return set of track IDs to exclude from popularity statistics."""
    excluded = set()

    for track in tracks_with_scores or []:
        if should_exclude_track_from_stats(
            track.get("title", ""),
            track.get("album", ""),
            int(track.get("is_live") or 0),
            int(track.get("album_context_live") or 0),
        ):
            excluded.add(track.get("id"))

    for _, variants in (alternate_takes_map or {}).items():
        for variant in variants[1:]:
            if isinstance(variant, dict):
                excluded.add(variant.get("id"))

    return excluded


def is_top_artist_catalog_score(cursor, canonical_artist, popularity_score, threshold=0.25):
    """Return True when a score is in the top *threshold* fraction of the artist's catalog."""
    if not canonical_artist or popularity_score <= 0:
        return False

    cursor.execute(
        """
        SELECT COUNT(*),
               SUM(CASE WHEN final_score > %s THEN 1 ELSE 0 END)
        FROM tracks
        WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s
          AND final_score > 0
        """,
        (popularity_score, canonical_artist),
    )

    row = cursor.fetchone()

    if not row:
        return False

    total = row[0] or 0
    above = row[1] or 0

    return bool(total and (above / total) <= threshold)
