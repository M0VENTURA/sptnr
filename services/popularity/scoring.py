"""
services/popularity/scoring.py

Legacy compatibility wrapper around ``popularity_math``.

The canonical scoring entry point is now ``track_stage.process_track``
which calls ``popularity_math.calculate_combined_popularity_score``
directly.  This file exists only for any remaining old imports.
"""

from __future__ import annotations

from typing import Any

from services.popularity.popularity_math import (
    calculate_combined_popularity_score,
)


def calculate_track_score(track: dict[str, Any]) -> dict[str, float]:
    """Backward-compatible track scoring wrapper."""
    lastfm = track.get("lastfm_listeners", 0) or 0
    listenbrainz = track.get("listenbrainz_listens", 0) or 0

    combined = calculate_combined_popularity_score(
        lastfm_listeners=lastfm,
        listenbrainz_listens=listenbrainz,
    )

    return {
        "lastfm_score": float(combined.get("lastfm_score", 0)),
        "listenbrainz_score": float(combined.get("listenbrainz_score", 0)),
        "final_score": float(combined.get("combined_score", 0)),
    }