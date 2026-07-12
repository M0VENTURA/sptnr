"""
services/popularity/scoring.py

Single source of truth for popularity scoring logic.

Combines:
- Last.fm
- ListenBrainz
- Age weighting
- Z-score calculations
"""

from services.popularity.popularity_math import (
    calculate_lastfm_popularity_score,
    calculate_listenbrainz_popularity_score,
    calculate_combined_popularity_score,
)


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
        "lastfm_score": combined["lastfm"],
        "listenbrainz_score": combined["listenbrainz"],
        "final_score": combined["combined"],
    }