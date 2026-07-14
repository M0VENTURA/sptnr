"""
Shared utility functions for popularity scoring and z-score calculations.
Consolidates duplicated logic across popularity_helpers.py and popularity.py
"""

import logging
from contextlib import contextmanager
from typing import Optional

# Constants for z-score to popularity conversion
# Formula: 50 + (z_score * Z_SCORE_TO_POPULARITY_SCALE)
# Maps z-scores to 0-100 popularity scale:
#   z=-3 → 0 points (far below mean)
#   z=-1 → 33.3 points (1 stdev below mean)
#   z=0 → 50 points (at mean)
#   z=+1 → 66.7 points (1 stdev above mean)
#   z=+3 → 100 points (far above mean)
Z_SCORE_MIDPOINT = 50.0
Z_SCORE_TO_POPULARITY_SCALE = 16.7


def calculate_track_zscore(score: float, mean: float, stddev: float) -> float:
    """
    Calculate z-score for a track relative to a reference distribution.
    
    Z-score = (score - mean) / stddev
    
    Handles edge cases:
    - Zero or negative stddev → returns 0.0 (no variance)
    - Missing values → returns 0.0
    
    Args:
        score: Track's score (popularity, listeners, playcount, etc.)
        mean: Mean of the reference distribution
        stddev: Standard deviation of the reference distribution
        
    Returns:
        Z-score (typically -3 to +3 for normal distributions)
    """
    if stddev and stddev > 0:
        return (score - mean) / stddev
    return 0.0


def zscore_to_popularity(z_score: float) -> float:
    """
    Convert z-score to 0-100 popularity scale.
    
    Formula: 50 + (z_score * 16.7)
    
    Maps z-scores to popularity:
    - z=-3 → 0 (far below average)
    - z=-1 → 33.3 (below average)
    - z=0 → 50 (at average)
    - z=+1 → 66.7 (above average)
    - z=+3 → 100 (far above average)
    
    Args:
        z_score: Standard z-score value
        
    Returns:
        Popularity score (0-100), clamped to valid range
    """
    score = Z_SCORE_MIDPOINT + (z_score * Z_SCORE_TO_POPULARITY_SCALE)
    return min(100.0, max(0.0, score))


@contextmanager
def get_db_connection_context(conn=None):
    """
    Context manager for safe database connection handling.
    
    Automatically closes connections that were created by this manager,
    while leaving externally-provided connections open.
    
    Usage:
        with get_db_connection_context() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT ...")
            
        # Or with existing connection:
        with get_db_connection_context(existing_conn) as conn:
            # Connection won't be closed when exiting
            pass
    
    Args:
        conn: Optional existing database connection. If provided, it won't be closed.
        
    Yields:
        Database connection
    """
    should_close = conn is None
    
    if should_close:
        try:
            from .db_utils import get_db_connection
            conn = get_db_connection()
        except Exception as e:
            logging.error(f"Failed to get database connection: {e}")
            raise
    
    try:
        yield conn
    finally:
        if should_close and conn:
            try:
                conn.close()
            except Exception as e:
                logging.warning(f"Error closing database connection: {e}")
