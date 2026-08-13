"""Popularity statistics and eligibility helpers."""

from __future__ import annotations
import logging
from statistics import mean, median, stdev

from services.catalog.album_classification_service import (
    is_bonus_track_title,
    should_exclude_track_from_stats,
)
from services.popularity.popularity_math import (
    ALBUM_RELATIVE_MIN_ALBUM_TRACKS,
    calculate_track_zscore,
)

logger = logging.getLogger(__name__)


def _filter_bonus_rows(rows) -> list:
    """Drop bonus/live/alternate track rows (by title) from a DB result set.

    A deluxe/expanded album carries extra live/acoustic/remix cuts whose low
    scores would drag the album baseline down and inflate every core track's
    relative z.  DB rows carry no ``is_live``/``album_context_live`` flags, so
    the same title-pattern check used by the star-rating merges
    (``is_bonus_track_title``) is applied here.
    """
    return [row for row in (rows or []) if not is_bonus_track_title(str(row[0] or ""))]


def calculate_album_stats(conn, artist: str, album: str) -> tuple[float, float, list[float]]:
    """Return (mean, stdev, values) of popularity scores for an album.

    Bonus/alternate/live tracks (flagged by title pattern) are excluded from
    the distribution, mirroring ``finalise_stage``'s star-rating baseline and
    ``scan_stage_runner._album_reference_scores``.  When fewer than 3 core
    tracks remain (a genuine live album flags everything), the full tracklist
    is used — a live album is scored against itself.

    ``conn`` is kept for backward compatibility — the query runs on its own
    SQLAlchemy session.
    """
    from sqlalchemy import text as _text
    from db.engine import db_session as _db_session
    try:
        with _db_session() as session:
            rows = session.execute(
                _text("""
                    SELECT title, final_score FROM tracks
                    WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
                      AND album = :album
                      AND final_score > 0
                """),
                {"artist": artist, "album": album},
            ).fetchall() or []
    except Exception as exc:
        logger.debug("[POPULARITY_STATS] Album stats session error for %s - %s: %s", artist, album, exc)
        return 0.0, 0.0, []

    values = [float(row[1] or 0) for row in rows if float(row[1] or 0) > 0]
    core_values = [float(row[1] or 0) for row in _filter_bonus_rows(rows) if float(row[1] or 0) > 0]
    if len(core_values) >= ALBUM_RELATIVE_MIN_ALBUM_TRACKS:
        values = core_values

    if not values:
        logger.debug("[POPULARITY_STATS] No valid score tracks found for album: %s - %s", artist, album)
        return 0.0, 0.0, []

    avg = mean(values)
    sd = stdev(values) if len(values) > 1 else 0.0
    logger.debug("[POPULARITY_STATS] Album stats for %s - %s: mean=%.1f, stdev=%.1f (count=%d)", artist, album, avg, sd, len(values))

    return avg, sd, values


def calculate_artist_stats(conn, artist: str) -> tuple[float, float, list[float]]:
    """Return (mean, stdev, values) of popularity scores for an artist.

    ``conn`` is kept for backward compatibility — the query runs on its own
    SQLAlchemy session.
    """
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

    if not values:
        logger.debug("[POPULARITY_STATS] No valid score tracks found for artist: %s", artist)
        return 0.0, 0.0, []

    avg = mean(values)
    sd = stdev(values) if len(values) > 1 else 0.0
    logger.debug("[POPULARITY_STATS] Artist stats for %s: mean=%.1f, stdev=%.1f (count=%d)", artist, avg, sd, len(values))

    return avg, sd, values


def calculate_album_listener_stats(conn, artist: str, album: str) -> tuple[list[float], list[float]]:
    """Return ``(lastfm_listeners[], listenbrainz_listens[])`` for an album.

    Raw listener/listen counts per album track, used to build the album-local
    LF/LB distributions for single-detection's composite z-score.  Only
    positive counts are included (missing data is not signal).  Bonus/live
    tracks are excluded from the distributions (deluxe-edition padding must
    not compress the core tracks' z) with the same fallback as
    ``calculate_album_stats``: when fewer than 3 core tracks remain, the full
    tracklist is used.  ``conn`` is kept for backward compatibility — the
    query runs on its own SQLAlchemy session.
    """
    from sqlalchemy import text as _text
    from db.engine import db_session as _db_session
    try:
        with _db_session() as session:
            result = session.execute(
                _text("""
                    SELECT title, lastfm_listeners, listenbrainz_listens FROM tracks
                    WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
                      AND album = :album
                """),
                {"artist": artist, "album": album},
            )
            rows = result.fetchall() or []
    except Exception as exc:
        logger.debug("[POPULARITY_STATS] Album listener session error for %s - %s: %s", artist, album, exc)
        return [], []

    lf_listeners: list[float] = []
    lb_listens: list[float] = []
    source_rows = _filter_bonus_rows(rows)

    for row in source_rows:
        _lf = float(row[1] or 0)
        _lb = float(row[2] or 0)
        if _lf > 0:
            lf_listeners.append(_lf)
        if _lb > 0:
            lb_listens.append(_lb)

    if len(lf_listeners) < ALBUM_RELATIVE_MIN_ALBUM_TRACKS or len(lb_listens) < ALBUM_RELATIVE_MIN_ALBUM_TRACKS:
        # Live album / over-filtered: fall back to the full tracklist so a
        # legitimate live album is still scored against its own tracklist.
        lf_listeners = []
        lb_listens = []
        for row in rows or []:
            _lf = float(row[1] or 0)
            _lb = float(row[2] or 0)
            if _lf > 0:
                lf_listeners.append(_lf)
            if _lb > 0:
                lb_listens.append(_lb)
    return lf_listeners, lb_listens

