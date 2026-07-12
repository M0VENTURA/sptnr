"""Popularity statistics and eligibility helpers."""

from __future__ import annotations
from statistics import mean, median, stdev

from services.catalog.album_classification_service import (
    should_exclude_track_from_stats
)
from services.popularity.popularity_math import calculate_track_zscore


def calculate_album_stats(conn, artist: str, album: str) -> tuple[float, float, list[float]]:
    """Return (mean, stdev, values) of popularity scores for an album."""
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
        return 0.0, 0.0, []

    return mean(values), (stdev(values) if len(values) > 1 else 0.0), values


def calculate_artist_stats(conn, artist: str) -> tuple[float, float, list[float]]:
    """Return (mean, stdev, values) of popularity scores for an artist."""
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
        return 0.0, 0.0, []

    return mean(values), (stdev(values) if len(values) > 1 else 0.0), values


def calculate_artist_popularity_stats(artist_name: str, conn) -> dict:
    avg, sd, values = calculate_artist_stats(conn, artist_name)

    return {
        "mean": avg,
        "median": median(values) if values else 0.0,
        "stdev": sd,
        "count": len(values),
        "max": max(values) if values else 0.0,
    }


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