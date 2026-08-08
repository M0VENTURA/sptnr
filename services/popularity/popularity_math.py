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
# Logistic growth rate for the soft-ceiling z-score→popularity mapping.
# Chosen so the slope at z=0 matches the legacy linear scale (16.7 per z):
# the derivative of 100/(1+e^{-kz}) at 0 is 25k, so k = 16.7/25 = 0.668.
Z_SCORE_LOGISTIC_K = Z_SCORE_TO_POPULARITY_SCALE / 25.0

# Log-scale multiplier mapping raw listener/listen counts to a 0-100 score.
# The previous 22.0 saturated at ~35k listens — every mid-popularity track
# capped at 100, so the score stopped reflecting real popularity differences
# (e.g. 90k vs 120k listens scored identically). 16.0 only saturates at
# ~1.78M listens, keeping the scale responsive across the typical 10k-1M range:
#   10k -> 64   50k -> 75   100k -> 80   250k -> 86   1M -> 96
LOG_SCALE_MULTIPLIER = 16.0


def calculate_track_zscore(score: float, mean_value: float, stddev: float) -> float:
    """Calculate z-score for a track relative to a reference distribution."""
    if stddev and stddev > 0:
        return (score - mean_value) / stddev
    return 0.0


def zscore_to_popularity(z_score: float) -> float:
    """Convert z-score to a 0-100 popularity score with a soft ceiling.

    The legacy linear mapping (``50 + z * 16.7``) clamped at 100 for any
    z >= 3, so every track well above its artist's median saturated to 100
    and lost all discrimination (e.g. 128k vs 156k listeners both scored
    100.0).  Below the midpoint the linear map is kept (it already floors at
    0 for far-below-median tracks); above it a logistic curve keeps the same
    slope at z=0 but approaches 100 asymptotically, so genuinely more popular
    tracks keep a higher score instead of collapsing onto the same ceiling:
    z=1→66, z=2→79, z=3→88, z=4→93, z=6→98.
    """
    if z_score <= 0:
        return min(100.0, max(0.0, Z_SCORE_MIDPOINT + (z_score * Z_SCORE_TO_POPULARITY_SCALE)))
    score = 100.0 / (1.0 + math.exp(-Z_SCORE_LOGISTIC_K * z_score))
    return min(100.0, max(0.0, score))


def calculate_lastfm_popularity_score(listeners: int, artist_max_listeners: int = 0) -> float:
    """Calculate normalized Last.fm popularity from listener count."""
    if listeners is None or listeners <= 0:
        return 0.0
    if artist_max_listeners and artist_max_listeners > 0:
        return min(100.0, max(0.0, (listeners / artist_max_listeners) * 100.0))
    return min(100.0, math.log10(listeners + 1) * LOG_SCALE_MULTIPLIER)


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
    """Calculate normalized ListenBrainz popularity from global listen count.

    Log scale with ``LOG_SCALE_MULTIPLIER`` so the score keeps discriminating
    between mid-popularity tracks instead of capping at 100 for anything above
    ~35k listens (the previous 22.0 multiplier's saturation point).
    """
    if listen_count is None or listen_count <= 0:
        return 0.0
    return min(100.0, math.log10(listen_count + 1) * LOG_SCALE_MULTIPLIER)


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


def evaluate_listenbrainz_validity(
    *,
    listenbrainz_listens: int = 0,
    lastfm_listeners: int = 0,
    album_lb_listens: Optional[List[int]] = None,
    album_lf_lb_pairs: Optional[List[tuple]] = None,
    is_single: bool = False,
) -> tuple[bool, list[str]]:
    """Decide whether a track's ListenBrainz count is realistic for its album.

    A mismatched recording MBID (a wrong / split / obscure recording) can
    resolve a tiny LB count for a track whose real popularity is healthy,
    dragging the album average down.  LB is treated as **invalid** when:

    - it sits more than 2× scaled-MAD below the album's LB median, or
    - the track's LF/LB ratio is more than 10× the album's median LF/LB
      ratio (a far bigger LF footprint than LB implies a count mismatch).

    Confirmed singles are exempt: their LB is legitimate standalone evidence
    and is never dropped.

    Returns ``(lb_valid, reasons)``.
    """
    if is_single:
        return True, []
    lb = int(listenbrainz_listens or 0)
    if lb <= 0:
        return True, []  # missing data is not invalid data
    reasons: list[str] = []

    # LB vs the album's LB distribution (median + scaled MAD).
    valid = [int(x) for x in (album_lb_listens or []) if int(x or 0) > 0]
    if len(valid) >= 5:
        med = median(valid)
        mad = median([abs(v - med) for v in valid])
        spread = mad * 1.4826
        if spread > 0 and lb < med - 2 * spread:
            reasons.append("lb_far_below_album_median")

    # LF/LB ratio vs the album's median ratio.
    lf = int(lastfm_listeners or 0)
    if lf > 0 and not reasons:
        ratios = [
            int(a) / int(b)
            for a, b in (album_lf_lb_pairs or [])
            if int(a or 0) > 0 and int(b or 0) > 0
        ]
        if len(ratios) >= 5:
            med_ratio = median(ratios)
            if med_ratio > 0 and (lf / lb) > 10 * med_ratio:
                reasons.append("lf_lb_ratio_outlier")

    return not reasons, reasons


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
    album_lf_listeners: Optional[List[int]] = None,
    age_source_value: float = 0.0,
    release_date: Optional[str] = None,
    is_single: bool = False,
    has_metadata: bool = False,
    is_featured_track: bool = False,
    is_live_track: bool = False,
    lastfm_weight_override: Optional[float] = None,
    single_boost: float = 1.15,
    metadata_score_floor: float = 5.0,
    live_weight_penalty: float = 0.5,
) -> Dict[str, float]:
    """Blend Last.fm + ListenBrainz + age into one weighted popularity score.

    Matches the legacy ``popularity_scan`` behaviour:
    - live tracks get a ``live_weight_penalty`` (default 50%) on the Last.fm weight
    - source-mismatch weighting honours featured-track / metadata flags
    - confirmed singles receive a ``single_boost`` (default +15%)
    - tracks with a confirmed MusicBrainz ID get a ``metadata_score_floor``
      (default 5.0) so a known track never scores near zero when external
      APIs have no data
    - Last.fm is scored against the ALBUM's listener distribution (z-score)
      when ``album_lf_listeners`` is provided (legacy parity).  The
      artist-max-relative scale compresses every track of an artist that has
      a bigger hit elsewhere, so LF-only tracks would rank below LB tracks
      with fewer listeners.
    """
    if album_lf_listeners and listenbrainz_listens > 0:
        lastfm_score = calculate_lastfm_zscore_popularity(
            lastfm_listeners,
            lastfm_listeners,  # playcount placeholder (unused by the z path)
            album_lf_listeners,
            album_lf_listeners,
        )
    else:
        # No ListenBrainz evidence: score Last.fm on its absolute log
        # popularity so a listener-rich track without LB data is not crushed
        # by the catalogue-relative z-score (which compares against the
        # artist's biggest hits) — it relies on Last.fm alone.
        lastfm_score = calculate_lastfm_popularity_score(lastfm_listeners, 0)
    lb_score = calculate_listenbrainz_popularity_score(listenbrainz_listens)

    if album_lb_listens:
        lb_percentile = calculate_listenbrainz_percentile(listenbrainz_listens, album_lb_listens)
        lb_score = max(lb_score, lb_percentile * 100.0)

    age_score = 0.0
    if release_date and age_source_value:
        # Decay raw count by age, then normalise to 0-100.
        # Decay-then-normalise (vs normalise-then-decay) retains better
        # discrimination: a recent smash still scores ~73, while a 5-year-old
        # hit with the same raw count scores ~65.
        aged, _days = score_by_age(age_source_value, release_date)
        age_score = calculate_listenbrainz_popularity_score(int(aged or 0))

    # Check for source mismatch and adjust weights dynamically
    has_mismatch = is_source_mismatch(lastfm_listeners, listenbrainz_listens)
    is_unreliable = is_lastfm_unreliable(lastfm_listeners, listenbrainz_listens)

    # Live-track penalty: legacy logic halves the Last.fm weight because live
    # recordings are streamed far less than their studio counterparts.
    effective_lf_weight = lastfm_weight_override
    if effective_lf_weight is None:
        effective_lf_weight = LASTFM_WEIGHT
        if is_live_track:
            effective_lf_weight = LASTFM_WEIGHT * max(0.0, min(1.0, live_weight_penalty))

    # Build active score/weight pairs so missing sources don't dilute the blend
    active_scores: list[float] = []
    active_weights: list[float] = []

    if has_mismatch or is_unreliable:
        # Use dynamic weights when sources conflict
        lf_weight, lb_weight = adjust_weights(
            lastfm_listeners,
            listenbrainz_listens,
            is_featured_track=is_featured_track,
            metadata_confirmed=has_metadata,
        )
        if is_live_track:
            lf_weight = lf_weight * max(0.0, min(1.0, live_weight_penalty))
        if lastfm_score > 0:
            active_scores.append(lastfm_score)
            active_weights.append(lf_weight)
        if lb_score > 0:
            active_scores.append(lb_score)
            active_weights.append(lb_weight)
    else:
        # Use default weights when sources agree
        if lastfm_score > 0:
            active_scores.append(lastfm_score)
            active_weights.append(effective_lf_weight)
        if lb_score > 0:
            active_scores.append(lb_score)
            active_weights.append(LISTENBRAINZ_WEIGHT)

    if age_score > 0:
        active_scores.append(age_score)
        active_weights.append(AGE_WEIGHT)

    if active_scores and active_weights:
        total_weight = sum(active_weights)
        combined = sum(s * w for s, w in zip(active_scores, active_weights)) / total_weight
    else:
        combined = 0.0

    # Minimum popularity floor for tracks with confirmed metadata (MBID) so a
    # known track never scores near zero when external APIs have no data.
    if has_metadata and 0.0 < combined < metadata_score_floor:
        combined = metadata_score_floor

    # Confirmed singles receive a subtle boost (legacy behaviour).
    if is_single and combined > 0:
        combined *= single_boost

    return {
        "combined_score": round(min(100.0, max(0.0, combined)), 3),
        "lastfm_score": round(lastfm_score, 3),
        "listenbrainz_score": round(lb_score, 3),
        "age_score": round(age_score, 3),
    }


def is_source_mismatch(lastfm_listeners, lb_listens) -> bool:
    """Detect large mismatch between Last.fm and ListenBrainz popularity.

    A zero on either side is missing data, not a disagreement — a track with
    no ListenBrainz listens is scored on Last.fm alone, so it must not be
    routed into the dynamic-weight path.
    """
    lastfm_listeners = int(lastfm_listeners or 0)
    lb_listens = int(lb_listens or 0)
    if lastfm_listeners == 0 or lb_listens == 0:
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
