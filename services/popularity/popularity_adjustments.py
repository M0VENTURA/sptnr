"""DB-backed popularity adjustment helpers.

Handles artist-context and album-level popularity adjustments.
These functions modify raw popularity scores based on statistical context.
"""

from __future__ import annotations

import logging
from statistics import median
from typing import Optional

from sqlalchemy import text
from db.engine import db_session
from db.utils import get_db_connection, row_get
from services.popularity.popularity_math import zscore_to_popularity

logger = logging.getLogger(__name__)


def apply_mean_popularity_adjustment(
    track_popularity: float,
    artist_name: str,
    release_year: int | None = None,
    conn=None,
) -> float:
    """Apply median+MAD-based popularity adjustment with optional time decay for pre-2005 releases.
    
    This function adjusts a track's popularity score based on the artist's catalog distribution,
    using median and Median Absolute Deviation (MAD) for robustness against outliers.
    
    For tracks released before 2005, applies time-based decay to account for sparse historical data.
    
    Args:
        track_popularity: Raw popularity score (0-100)
        artist_name: Artist name for lookup in artist_stats table
        release_year: Optional release year for time decay calculation
        conn: Optional existing database connection
        
    Returns:
        Adjusted popularity score (0-100)
    """
    if track_popularity <= 0:
        return 0.0

    MIN_SPREAD = 10.0
    should_close = conn is None
    db_conn = conn or get_db_connection()
    
    try:
        cursor = db_conn.cursor()
        placeholder = "%s"

        cursor.execute(
            f"""
            SELECT median_popularity, popularity_mad
            FROM artist_stats
            WHERE artist_name = {placeholder}
            """,
            (artist_name,),
        )

        row = cursor.fetchone()
        if not row:
            return track_popularity

        artist_median = row_get(row, "median_popularity")
        artist_mad = row_get(row, "popularity_mad")

        if artist_median is None or artist_median <= 0:
            return track_popularity

        # Use MAD or minimum spread, whichever is larger
        artist_spread = max(artist_mad if artist_mad else 0, MIN_SPREAD)

        # Calculate z-score
        if artist_spread > 0:
            z_score = (track_popularity - artist_median) / artist_spread
        else:
            z_score = 0

        # Apply time decay for pre-2005 releases
        if release_year and release_year < 2005:
            years_before_2005 = 2005 - release_year
            decay_factor = max(0.2, 1.0 - (years_before_2005 * 0.04))
            z_score *= decay_factor
            logger.debug(
                "Applied time decay to '%s' release (%s): decay_factor=%.2f z_score=%.2f",
                artist_name,
                release_year,
                decay_factor,
                z_score,
            )

        # Convert z-score back to 0-100 scale
        adjusted_score = zscore_to_popularity(z_score)

        logger.debug(
            "Median+MAD popularity adjustment for '%s': original=%.1f, z_score=%.2f, adjusted=%.1f (artist_median=%.1f, MAD=%.1f, spread=%.1f)",
            artist_name,
            track_popularity,
            z_score,
            adjusted_score,
            artist_median,
            artist_mad if artist_mad is not None else 0.0,
            artist_spread,
        )

        return adjusted_score

    except Exception as e:
        logger.debug("Error applying median+MAD popularity adjustment for '%s': %s", artist_name, e)
        return track_popularity
    finally:
        if should_close and db_conn:
            db_conn.close()


def apply_album_deviation_adjustment(
    track_popularity: float,
    artist_name: str,
    album_name: str,
    artist_mean_popularity: float | None = None,
    conn=None,
) -> float:
    """Apply album-level deviation adjustment for underperforming albums.

    Boosts tracks that stand out above an album's median when the album is
    underperforming relative to the artist's catalogue average (legacy
    standout-within-underperforming-album signal).
    """
    if track_popularity <= 0:
        return track_popularity
    should_close = conn is None
    db_conn = conn or get_db_connection()
    try:
        cursor = db_conn.cursor()
        cursor.execute(
            """
            SELECT final_score
            FROM tracks
            WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s
              AND album = %s
              AND final_score > 0
            """,
            (artist_name, album_name),
        )
        album_values = [
            float(row_get(row, "final_score", 0) or 0)
            for row in cursor.fetchall()
        ]
        if len(album_values) < 2:
            return track_popularity
        album_median = median(album_values)
        if (
            artist_mean_popularity
            and album_median < artist_mean_popularity * 0.6
            and track_popularity > album_median
        ):
            return min(100.0, track_popularity * 1.05)
        return track_popularity
    except Exception as exc:
        logger.debug(
            "Album deviation adjustment failed for %s / %s: %s",
            artist_name,
            album_name,
            exc,
        )
        return track_popularity
    finally:
        if should_close and db_conn:
            db_conn.close()