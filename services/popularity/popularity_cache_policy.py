"""Popularity cache policy helpers.

Determines when cached popularity data can be reused vs.
requiring a fresh API lookup. Uses age-based rules:
- Tracks older than 2 years with existing scores are frozen.
- Recently looked-up tracks reuse cached Spotify scores.

Reduces API calls during repeated scans.
"""

from datetime import datetime, timedelta


def is_track_older_than_years(year, years=2):
    """Return True when a release year is at least *years* old."""
    if not year:
        return False
    return (datetime.now().year - int(year)) >= years


def should_freeze_track(track):
    """Return True when a mature track with existing score should skip refresh."""
    return (
        is_track_older_than_years(track.get("year"), 2)
        and track.get("final_score")
    )


def should_use_cached_score(track):
    """Return True when a cached score is fresh enough to reuse."""
    last_lookup = track.get("last_spotify_lookup")

    if not last_lookup:
        return False

    return (datetime.now() - last_lookup) < timedelta(days=7)