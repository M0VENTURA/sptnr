"""
services/popularity/scoring.py

Single source of truth for popularity scoring logic.

Combines:
- Last.fm
- ListenBrainz
- Age weighting
- Z-score calculations
"""

from services.popularity.popularity_math import calculate_combined_popularity_score


def calculate_track_score(track):
    """
    Calculate final track score.

    This replaces fragmented scoring logic from popularity1 + helpers.
    """

    lastfm = track.get("lastfm_listeners", 0)
    listenbrainz = track.get("listenbrainz_listens", 0)

    combined = calculate_combined_popularity_score(
        lastfm_listeners=lastfm,
        listenbrainz_listens=listenbrainz,
    )

    return {
        "lastfm_score": combined.get("lastfm_score", 0),
        "listenbrainz_score": combined.get("listenbrainz_score", 0),
        "final_score": combined.get("combined_score", 0),
    }