"""Standout Detection and Star Rating Service

This module provides standout track detection and star rating assignment based on
z-score analysis and artist catalog context.

Key Responsibilities:
    - Detect standout tracks using iterative z-score analysis
    - Identify top tracks via popularity gap detection
    - Assign star ratings (1-5 stars) based on multiple criteria
    
Star Rating Criteria:
    ⭐⭐⭐⭐⭐ (5 stars): Album z ≥ 1.0 OR Artist z ≥ 1.2 OR Top 10% of catalog
    ⭐⭐⭐⭐ (4 stars): Album z ≥ 0.8 OR Artist z ≥ 1.0 OR Top 20% of catalog
    ⭐⭐⭐ (3 stars): Above album average (z ≥ 0.0)
    ⭐⭐ (2 stars): At album average
    ⭐ (1 star): Default for all tracks with valid scores

Configuration:
    All thresholds are configurable via config.yaml under `single_detection`:
    
    ```yaml
    single_detection:
      album_zscore_threshold: 0.8
      artist_zscore_threshold: 2.2
      artist_top_percentile: 0.10
      artist_min_tracks: 10
      star_5:
        album_z: 1.0
        artist_z: 1.2
        artist_pct: 0.10
      star_4:
        album_z: 0.8
        artist_z: 1.0
        artist_pct: 0.20
    ```

Usage:
    >>> from services.popularity.standout_service import detect_via_iterative_zscore
    >>> is_standout = detect_via_iterative_zscore(score=85, artist="The Beatles", album="Abbey Road")

Architecture:
    Database-backed service (requires track popularity scores).
    Called by: popularity scoring pipeline, star rating display
    Uses: services/popularity/popularity_math.calculate_track_zscore()
"""

from __future__ import annotations

import logging
from statistics import mean, stdev

from db.utils import get_db_connection, row_get
from services.popularity.popularity_math import calculate_track_zscore
from helpers.config_helpers import get_standout_config

logger = logging.getLogger(__name__)

# Load configuration from centralized config
STANDOUT_CONFIG = get_standout_config()


def detect_via_iterative_zscore(current_track_score: float, artist: str, album: str, conn=None, verbose: bool = False) -> bool:
    """Detect standout using album z-score."""
    if not current_track_score or current_track_score <= 0:
        return False
    should_close = conn is None
    db_conn = conn or get_db_connection()
    try:
        cursor = db_conn.cursor()
        cursor.execute(
            """
            SELECT final_score FROM tracks
            WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s
              AND album = %s
              AND final_score > 0
            """,
            (artist, album),
        )
        scores = [float(row_get(row, "final_score", 0) or 0) for row in cursor.fetchall()]
        if len(scores) < 3:
            return False
        z = calculate_track_zscore(current_track_score, mean(scores), stdev(scores))
        return z >= STANDOUT_CONFIG["album_zscore_threshold"]
    except Exception as exc:
        logger.debug("Iterative z-score standout failed for %s / %s: %s", artist, album, exc)
        return False
    finally:
        if should_close and db_conn:
            db_conn.close()


def get_top_standout_tracks_with_gap(artist: str, album: str, conn=None, gap_threshold: float = 0.5, is_compilation: bool = False, verbose: bool = False) -> set:
    """Identify top album tracks separated by a popularity gap."""
    should_close = conn is None
    db_conn = conn or get_db_connection()
    try:
        cursor = db_conn.cursor()
        cursor.execute(
            """
            SELECT id, title, final_score
            FROM tracks
            WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s
              AND album = %s
              AND final_score > 0
            ORDER BY final_score DESC
            """,
            (artist, album),
        )
        rows = cursor.fetchall() or []
        if len(rows) < 2:
            return {row_get(rows[0], "id", 0)} if rows else set()
        scores = [(row_get(row, "id", 0), float(row_get(row, "final_score", 2) or 0)) for row in rows]
        selected = {scores[0][0]}
        for idx in range(1, min(len(scores), 5)):
            previous = scores[idx - 1][1]
            current = scores[idx][1]
            if previous - current <= gap_threshold:
                selected.add(scores[idx][0])
            else:
                break
        return selected
    except Exception as exc:
        logger.debug("Top standout gap detection failed for %s / %s: %s", artist, album, exc)
        return set()
    finally:
        if should_close and db_conn:
            db_conn.close()
