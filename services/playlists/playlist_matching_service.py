"""Playlist track-to-library matching service.

Matches tracks from external playlist sources against the local music
library using fuzzy title/artist matching.

Key Functions:
    - match_playlist_tracks(): Main entry point that loops through
      playlist tracks and matches each one against the library.
      Returns (matched_tracks, missing_tracks, stats).

Architecture:
    Owns the matching loop and result aggregation. Delegates individual
    track matching to the ``enhanced_match_track`` callable parameter.
    Manages its own database context via ``db_cursor()``.

    Returns:
        matched_tracks: List of successfully matched library tracks.
        missing_tracks: Tracks that could not be found in the library.
        stats: Dict with counts for each matching strategy used.
"""

from __future__ import annotations
import logging
from sqlalchemy import text
from db.engine import db_session


from collections.abc import Callable
from typing import Any

def match_playlist_tracks(
    tracks: list[dict[str, Any]],
    enhanced_match_track: Callable[..., tuple[Any, float, str]],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, int],
]:

    """
    Handles matching playlist tracks to the library.
    Now manages its own DB context and logging.
    """
    matched_tracks = []
    missing_tracks = []
    stats = {"isrc": 0, "fuzzy": 0, "strict": 0, "unmatched": 0}

    # Open the database context here, so the service is self-contained
    with db_session() as session:
        # Resolve fuzzy threshold from config
        _fuzzy_threshold = 0.80
        try:
            from helpers.config_helpers import get_matching_thresholds
            _fuzzy_threshold = get_matching_thresholds()["fuzzy_threshold"]
        except Exception:
            pass

        # Get a raw DBAPI cursor from the session for enhanced_match_track compatibility
        cursor = session.connection().connection.cursor()

        for track in tracks:
            # We no longer pass 'cursor' or 'logger' into the matching function
            # if we can avoid it. If 'enhanced_match_track' MUST have them,
            # define them locally or handle it within that function.
            result, confidence, strategy = enhanced_match_track(
                track,
                cursor, 
                enable_isrc=True,
                enable_fuzzy=True,
                enable_strict=True,
                fuzzy_threshold=_fuzzy_threshold,
            )

            if result:
                matched_tracks.append({
                    "id": result["id"],
                    "title": result["title"],
                    "artist": result["artist"],
                    "album": result["album"],
                    "confidence": confidence,
                    "strategy": strategy
                })
                stats[strategy] = stats.get(strategy, 0) + 1
            else:
                missing_tracks.append(track)
                stats["unmatched"] += 1

    return matched_tracks, missing_tracks, stats