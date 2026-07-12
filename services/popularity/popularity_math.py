"""Pure popularity scoring and statistics helpers.

This module should not import API clients or database modules.
"""

from __future__ import annotations

import math
from datetime import datetime
from statistics import mean, median, stdev
from typing import Dict, List, Optional

from services.popularity.popularity_config import AGE_WEIGHT, LASTFM_WEIGHT, LISTENBRAINZ_WEIGHT

Z_SCORE_MIDPOINT = 50.0
Z_SCORE_TO_POPULARITY_SCALE = 16.7


def calculate_track_zscore(score: float, mean_value: float, stddev: float) -> float:
    """Calculate z-score for a track relative to a reference distribution."""
    if stddev and stddev > 0:
        return (score - mean_value) / stddev
    return 0.0


def zscore_to_popularity(z_score: float) -> float:
    """Convert z-score to a 0-100 popularity score."""
    score = Z_SCORE_MIDPOINT + (z_score * Z_SCORE_TO_POPULARITY_SCALE)
    return min(100.0, max(0.0, score))


def calculate_lastfm_popularity_score(listeners: int, artist_max_listeners: int = 0) -> float:
    """Calculate normalized Last.fm popularity from listener count."""
    if listeners is None or listeners <= 0:
        return 0.0
    if artist_max_listeners and artist_max_listeners > 0:
        return min(100.0, max(0.0, (listeners / artist_max_listeners) * 100.0))
    return min(100.0, math.log10(listeners + 1) * 20.0)


def calculate_lastfm_zscore_popularity(
    listeners: int,
    playcount: int,
    album_listeners_list: List[int],
    album_playcounts_list: List[int],
) -> float:
    """Calculate Last.fm popularity using album-level z-score normalization."""
    valid_listeners = [int(x or 0) for x in album_listeners_list if x is not None and int(x or 0) > 0]
    if not valid_listeners:
        return calculate_lastfm_popularity_score(listeners)
    if len(valid_listeners) < 2:
        return calculate_lastfm_popularity_score(listeners, max(valid_listeners))
    listener_mean = mean(valid_listeners)
    listener_std = stdev(valid_listeners)
    z = calculate_track_zscore(float(listeners or 0), listener_mean, listener_std)
    return zscore_to_popularity(z)


def calculate_listenbrainz_popularity_score(listen_count: int) -> float:
    """Calculate normalized ListenBrainz popularity from global listen count."""
    if listen_count is None or listen_count <= 0:
        return 0.0
    return min(100.0, math.log10(listen_count + 1) * 22.0)


def calculate_listenbrainz_percentile(lb_listens, album_lb_listens):
    """Calculate percentile of a track within album ListenBrainz distribution."""
    if lb_listens is None or lb_listens <= 0:
        return 0.0
    valid = [x for x in (album_lb_listens or []) if x is not None and x > 0]
    if not valid:
        return 0.0
    below = sum(1 for x in valid if x < lb_listens)
    equal = sum(1 for x in valid if x == lb_listens)
    return (below + equal / 2.0) / len(valid)


def score_by_age(playcount: int | float, release_str: str) -> tuple[float, int]:
    """Apply age decay to a playcount-like metric."""
    try:
        if len(release_str) == 4 and release_str.isdigit():
            release_date = datetime.strptime(release_str, "%Y")
        else:
            release_date = datetime.strptime(release_str, "%Y-%m-%d")
        days_since = max((datetime.now() - release_date).days, 30)
        capped_days = min(days_since, 5 * 365)
        return playcount * (1 / math.log2(capped_days + 2)), days_since
    except Exception:
        return 0.0, 9999


def calculate_combined_popularity_score(
    *,
    lastfm_listeners: int = 0,
    lastfm_artist_max_listeners: int = 0,
    listenbrainz_listens: int = 0,
    album_lb_listens: Optional[List[int]] = None,
    age_source_value: float = 0.0,
    release_date: Optional[str] = None,
) -> Dict[str, float]:
    """Blend Last.fm + ListenBrainz + age into one weighted popularity score."""
    lastfm_score = calculate_lastfm_popularity_score(lastfm_listeners, lastfm_artist_max_listeners)
    lb_score = calculate_listenbrainz_popularity_score(listenbrainz_listens)

    if album_lb_listens:
        lb_percentile = calculate_listenbrainz_percentile(listenbrainz_listens, album_lb_listens)
        lb_score = max(lb_score, lb_percentile * 100.0)

    age_score = 0.0
    if release_date and age_source_value:
        aged, _days = score_by_age(age_source_value, release_date)
        age_score = calculate_listenbrainz_popularity_score(int(aged or 0))

    # Check for source mismatch and adjust weights dynamically
    has_mismatch = is_source_mismatch(lastfm_listeners, listenbrainz_listens)
    is_unreliable = is_lastfm_unreliable(lastfm_listeners, listenbrainz_listens)
    
    if has_mismatch or is_unreliable:
        # Use dynamic weights when sources conflict
        lf_weight, lb_weight = adjust_weights(
            lastfm_listeners, 
            listenbrainz_listens,
            is_featured_track=False,
            metadata_confirmed=False
        )
        combined = (lastfm_score * lf_weight) + (lb_score * lb_weight) + (age_score * AGE_WEIGHT)
    else:
        # Use default weights when sources agree
        combined = (
            (lastfm_score * LASTFM_WEIGHT)
            + (lb_score * LISTENBRAINZ_WEIGHT)
            + (age_score * AGE_WEIGHT)
        )
    
    return {
        "combined_score": round(min(100.0, max(0.0, combined)), 3),
        "lastfm_score": round(lastfm_score, 3),
        "listenbrainz_score": round(lb_score, 3),
        "age_score": round(age_score, 3),
    }


def is_source_mismatch(lastfm_listeners, lb_listens) -> bool:
    """Detect large mismatch between Last.fm and ListenBrainz popularity."""
    lastfm_listeners = int(lastfm_listeners or 0)
    lb_listens = int(lb_listens or 0)
    if lastfm_listeners == 0:
        return False
    return lb_listens >= max(100, lastfm_listeners * 3) or lastfm_listeners >= max(100, lb_listens * 5)


def is_lastfm_unreliable(lastfm_listeners, lb_listens) -> bool:
    """Flag Last.fm as unreliable when LF is very low but LB is strong."""
    return int(lastfm_listeners or 0) <= 20 and int(lb_listens or 0) >= 75


def adjust_weights(lastfm_listeners, lb_listens, is_featured_track=False, metadata_confirmed=False):
    """Adjust Last.fm / ListenBrainz weights when sources are mismatched."""
    lastfm_listeners = int(lastfm_listeners or 0)
    lb_listens = int(lb_listens or 0)
    if lastfm_listeners < 20:
        lf_weight = 0.0
    elif lb_listens > lastfm_listeners * 2:
        lf_weight = 0.4
    else:
        lf_weight = 0.6
    if is_featured_track:
        lf_weight = min(lf_weight, 0.35)
    if metadata_confirmed:
        lf_weight = max(lf_weight, 0.25)
    lb_weight = 1.0 - lf_weight
    return lf_weight, lb_weight
