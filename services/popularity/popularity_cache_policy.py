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


def get_cache_duration_hours(track_year: int | None = None) -> int:
    """Return cache TTL in hours based on track release age.

    Older releases change less frequently, so they can be cached longer:
    - 3+ years old: 168 hours (7 days)
    - 1-3 years old: 72 hours (3 days)
    - < 1 year or unknown: 24 hours (conservative)
    """
    if not track_year:
        return 24
    try:
        age_years = datetime.now().year - int(track_year)
        if age_years >= 3:
            return 168
        elif age_years >= 1:
            return 72
        else:
            return 24
    except (ValueError, TypeError):
        return 24