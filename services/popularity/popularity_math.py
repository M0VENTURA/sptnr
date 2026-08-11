"""Pure popularity scoring and statistics helpers.

This module should not import API clients or database modules.
"""

from __future__ import annotations

import math
from datetime import datetime
from statistics import mean, median, stdev
from typing import Dict, List, Optional

from services.popularity.popularity_config import resolve_weights

Z_SCORE_MIDPOINT = 50.0
Z_SCORE_TO_POPULARITY_SCALE = 16.7
# Minimum album spread (MAD*1.4826) when re-mapping a track's raw score to an
# album-relative popularity.  A near-uniform album (MAD ~ 0) would otherwise
# explode every z-score; the floor keeps the re-map bounded while still
# ordering tracks by how far they sit above/below the album median.
ALBUM_RELATIVE_MIN_SPREAD = 8.0
# Minimum number of valid album scores needed before album-relative re-mapping
# is meaningful.  Below this the raw score is kept unchanged (a 1-2 track
# "album" has no distribution to compare against).
ALBUM_RELATIVE_MIN_ALBUM_TRACKS = 3
# Adaptive spread floor for low-volatility albums: the absolute
# ``ALBUM_RELATIVE_MIN_SPREAD`` is scale-blind, so a UNIFORM high-scoring
# album (every track ~90, MAD ≈ 0) still amplifies tiny score gaps into large
# z-swings (a 10-point gap → z = 10/8 = 1.25 at the fixed floor).  The floor
# grows with the reference's own median so low-variance albums at ANY
# magnitude damp the same relative noise.  At the typical album-relative
# median (~50) the adaptive term (0.10 × 50 = 5.0) stays below the absolute
# floor, so normal albums are unaffected.
ADAPTIVE_MIN_SPREAD_FRACTION = 0.10
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

# The confirmed-single boost is a fade-out, not a flat multiplier: the full
# ``single_boost`` applies up to ``SINGLE_BOOST_FADE_START`` and tapers to
# zero by ``SINGLE_BOOST_FADE_END``.  Without the taper the legacy ``*1.15``
# saturates every top single against the ceiling — on 5-STAR, S-Class
# (364,373 listeners) and "Mixtape : Time Out" (128,085 listeners) both
# scored ~95-97 because the boost multiplied already-high raw scores past
# the soft ceiling, erasing the real popularity gap.
SINGLE_BOOST_FADE_START = 60.0
SINGLE_BOOST_FADE_END = 92.0


def fmt_count(count) -> str:
    """Format a listener/listen count compactly (14201 → '14.2k')."""
    try:
        value = float(count or 0)
    except (TypeError, ValueError):
        value = 0.0
    if value >= 1000:
        return f"{value / 1000:.1f}k"
    return f"{value:.0f}"


def single_boost_fade(score: float) -> float:
    """Return the boost multiplier taper for a raw combined score."""
    if score <= SINGLE_BOOST_FADE_START:
        return 1.0
    if score >= SINGLE_BOOST_FADE_END:
        return 0.0
    return 1.0 - (score - SINGLE_BOOST_FADE_START) / (SINGLE_BOOST_FADE_END - SINGLE_BOOST_FADE_START)


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


def calculate_robust_zscore(
    score: float,
    reference_scores: list[float],
    min_count: int = ALBUM_RELATIVE_MIN_ALBUM_TRACKS,
    min_spread: float = ALBUM_RELATIVE_MIN_SPREAD,
) -> tuple[float, float]:
    """Robust z-score (median + scaled-MAD) against a reference distribution.

    ``z = (score - reference_median) / max(reference_MAD * 1.4826, min_spread)``
    — the SAME robust z the album/artist-relative popularity re-map uses
    (``_remap_relative_popularity``), so star-rating bands, popularity
    re-mapping and single-detection z-standouts all measure a track's standing
    with identical mathematics.  No stage compares with mean/stddev z while
    another uses median/MAD z (which made stages disagree on a track's
    relative standing).

    Returns ``(z, spread)`` — ``spread`` is the scaled-MAD denominator used,
    letting callers convert score-point tolerances into z-units (e.g. the
    star-tier epsilon buffer).  Returns ``(0.0, 0.0)`` when the reference has
    too few valid positive scores (below ``min_count``) — there is no
    distribution to compare against.
    """
    valid = [float(s) for s in (reference_scores or []) if float(s or 0) > 0]
    if len(valid) < min_count:
        return 0.0, 0.0
    ref_median = median(valid)
    mad = median([abs(v - ref_median) for v in valid])
    spread = max(mad * 1.4826, min_spread, ADAPTIVE_MIN_SPREAD_FRACTION * ref_median)
    if spread <= 0:
        return 0.0, 0.0
    return (float(score) - ref_median) / spread, spread


def _remap_relative_popularity(raw_score: float, reference_scores: list[float]) -> float:
    """Shared robust-z re-map of a raw score against a reference distribution.

        z = (raw - reference_median) / max(reference_MAD * 1.4826, ALBUM_RELATIVE_MIN_SPREAD)
        popularity = zscore_to_popularity(z)

    Every reference group (album or artist catalogue) is centred at ~50, so a
    group's strongest track scores ~66-88 while its median sits at 50 — a
    tight, meaningful spread with no ceiling clumping.  Returns ``raw_score``
    unchanged when the reference has too few valid scores
    (< ``ALBUM_RELATIVE_MIN_ALBUM_TRACKS``) or the raw score is zero.
    """
    if raw_score is None or float(raw_score) <= 0:
        return float(raw_score or 0)
    valid = [float(s) for s in (reference_scores or []) if float(s or 0) > 0]
    if len(valid) < ALBUM_RELATIVE_MIN_ALBUM_TRACKS:
        return float(raw_score)
    reference_median = median(valid)
    mad = median([abs(v - reference_median) for v in valid])
    spread = max(mad * 1.4826, ALBUM_RELATIVE_MIN_SPREAD)
    if spread <= 0:
        return float(raw_score)
    z = (float(raw_score) - reference_median) / spread
    return zscore_to_popularity(z)


def apply_album_relative_popularity(raw_score: float, album_scores: list[float]) -> float:
    """Re-map a raw popularity score relative to its ALBUM's distribution.

    Popularity is album-relative only: the score becomes how strong a track is
    *within its album*, using the album's median and scaled-MAD as the robust
    reference (artist-wide stats are deliberately ignored).  See
    ``_remap_relative_popularity`` for the mapping.
    """
    return _remap_relative_popularity(raw_score, album_scores)


def apply_track_artist_relative_popularity(raw_score: float, artist_scores: list[float]) -> float:
    """Re-map a raw popularity score relative to a TRACK ARTIST's distribution.

    Used for compilation / Various-Artists albums: every track has a different
    artist, so comparing a track against the compilation's median (the "album
    artist" reference) is meaningless.  The score instead becomes how strong
    the track is *within its own track artist's catalogue*, using the artist's
    median and scaled-MAD as the robust reference.  Same mapping as the
    album-relative variant (``_remap_relative_popularity``).
    """
    return _remap_relative_popularity(raw_score, artist_scores)


def reanchor_scores_to_album_relative(rows: list[tuple[str, str, float]]) -> list[float]:
    """Re-anchor stored ``(album, score)`` rows onto the album-relative scale.

    The stored ``final_score`` column is a MIX of two scales.  Albums scanned
    after the album-relative re-map persist values centred at ~50 (tight, top
    ~62-67), while albums scored before the feature — or frozen/skipped tracks —
    keep their RAW combined score, which for an artist's biggest hits can sit
    at 85-95.  Merging the two in the artist-wide distribution silently
    inflates the top-10% ``popularity_marked`` cutoff and skews artist z-scores:
    a handful of raw-scale outliers occupy the top of the merged list and push
    genuinely top-10% album-relative tracks below the cut.

    Each stored album is therefore re-anchored against ITS OWN stored
    distribution with the same mapping used for freshly-scanned albums
    (``apply_album_relative_popularity``): the album's median → ~50 and its
    hits land in the same ~60-67 band as fresh re-mapped albums, so the merged
    catalogue becomes scale-consistent.  Raw-scale albums are corrected exactly
    as a fresh scan would re-map them; already-album-relative albums are
    centred at ~50 already, so the re-anchor only nudges them on the same scale
    (self-healing as albums get re-scanned from raw).  Albums with fewer than
    ``ALBUM_RELATIVE_MIN_ALBUM_TRACKS`` valid scores keep their stored value
    (no distribution to compare against), matching fresh-scan behaviour.
    """
    by_album: dict[str, list[float]] = {}
    for _album, _score in (rows or []):
        _s = float(_score or 0)
        if _s > 0:
            by_album.setdefault(str(_album or ""), []).append(_s)
    reanchored: list[float] = []
    for _album, _score in (rows or []):
        _s = float(_score or 0)
        if _s <= 0:
            continue
        reanchored.append(
            apply_album_relative_popularity(_s, by_album.get(str(_album or "")) or [])
        )
    return reanchored


# ---------------------------------------------------------------------------
# 3-step album scaling model (M_peak → A_skew → R_eff)
# ---------------------------------------------------------------------------

def age_skew_multiplier(
    album_year: Optional[float | int],
    current_year: int,
    peak_year: Optional[float | int] = None,
) -> float:
    """Return the age-skew multiplier ``A_skew`` for an album.

    Raw popularity inherently favours mid-career albums (5-15 years old), so
    an album's median score is scaled by its release year relative to the
    artist's PEAK album (``peak_year``):

    - Newer than the peak (fresh releases): logarithmic boost
      ``1.0 + 0.35 * log2(1 + 3 / max(1, Y_current - Y_album))`` — gives
      brand-new albums a +15%..+35% multiplier to offset their short
      accumulation window.
    - Older than the peak (legacy / pre-peak): gentle linear boost
      ``1.0 + 0.15 * min(1.0, (Y_peak - Y_album) / 20)`` (+5%..+15%) for
      pre-digital / pre-streaming gaps.
    - Peak-era albums (or unknown years): ``1.0`` (no adjustment).

    Returns ``1.0`` whenever the album year is missing/invalid so unknown-age
    albums are never penalised or inflated.
    """
    y_album = float(album_year or 0)
    if y_album <= 0 or current_year <= 0:
        return 1.0
    y_peak = float(peak_year or 0)
    if y_peak > 0:
        if y_album > y_peak:
            years_since = max(1, int(current_year) - int(y_album))
            return 1.0 + 0.35 * math.log2(1.0 + 3.0 / years_since)
        if y_album < y_peak:
            return 1.0 + 0.15 * min(1.0, (y_peak - y_album) / 20.0)
    return 1.0


def effective_album_ratio(effective_median: float, m_peak: float) -> float:
    """Return ``R_eff`` — where the album sits on the artist's career curve.

    ``R_eff = min(1.0, effective_median / M_peak)`` with ``effective_median``
    already age-skewed (``album_median * A_skew``).  Clamped to [0.0, 1.0];
    returns ``1.0`` when the benchmark is missing/zero (no constraint).
    """
    if not m_peak or float(m_peak) <= 0:
        return 1.0
    return min(1.0, max(0.0, float(effective_median) / float(m_peak)))


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
    - confirmed singles receive a ``single_boost`` (default +15%), faded out
      as the raw score nears the ceiling so top singles stay apart
    - tracks with a confirmed MusicBrainz ID get a ``metadata_score_floor``
      (default 5.0) so a known track never scores near zero when external
      APIs have no data
    - Last.fm is scored against the ALBUM's listener distribution (z-score)
      when ``album_lf_listeners`` is provided (legacy parity).  The
      artist-max-relative scale compresses every track of an artist that has
      a bigger hit elsewhere, so LF-only tracks would rank below LB tracks
      with fewer listeners.
    """
    # Absolute log-scale evidence (same calibration as the final 0-100 scale).
    # The album-relative z-score / percentile scores below can LIFT a track,
    # but never drag it: the floor is built from these absolute components so
    # a genuinely popular track keeps the score its strongest evidence proves.
    lastfm_log = calculate_lastfm_popularity_score(lastfm_listeners, 0)
    lb_log = calculate_listenbrainz_popularity_score(listenbrainz_listens)

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
        lastfm_score = lastfm_log
    lb_score = lb_log

    if album_lb_listens:
        lb_percentile = calculate_listenbrainz_percentile(listenbrainz_listens, album_lb_listens)
        lb_score = max(lb_log, lb_percentile * 100.0)

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
    # Weights are resolved LIVE from config on every call so a config.html
    # edit applies without a process restart (the module-level constants
    # used to freeze them at import time).
    _live_lf_w, _live_lb_w, _live_age_w = resolve_weights()
    effective_lf_weight = lastfm_weight_override
    if effective_lf_weight is None:
        effective_lf_weight = _live_lf_w
        if is_live_track:
            effective_lf_weight = _live_lf_w * max(0.0, min(1.0, live_weight_penalty))

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
            active_weights.append(_live_lb_w)

    if age_score > 0:
        active_scores.append(age_score)
        active_weights.append(_live_age_w)

    if active_scores and active_weights:
        total_weight = sum(active_weights)
        combined = sum(s * w for s, w in zip(active_scores, active_weights)) / total_weight
        # A blend can never out-rank its strongest ABSOLUTE evidence.  The
        # album-relative z-score / percentile and age scores are corroborating
        # signals: averaging them into the mix must not drag a genuinely
        # popular track below what its absolute log popularity already proves.
        # Legacy regressions under the naive average: a 139k-listener track
        # with 5.3k LB listens scored *below* a 133k-listener track with no LB
        # data, and the 179k-listener album lead scored lowest because its
        # under-counted LB weight out-voted its own Last.fm footprint.
        absolute_components = [s for s in (lastfm_log, lb_log, age_score) if s > 0]
        if absolute_components:
            strongest = max(absolute_components)
            if strongest > combined:
                combined = strongest
    else:
        combined = 0.0

    # Minimum popularity floor for tracks with confirmed metadata (MBID) so a
    # known track never scores near zero when external APIs have no data.
    if has_metadata and 0.0 < combined < metadata_score_floor:
        combined = metadata_score_floor

    # Confirmed singles receive a subtle boost (legacy behaviour).  The
    # boost fades to zero as the raw score approaches the ceiling so two
    # high-popularity singles don't collapse onto the same score (a flat
    # ``* 1.15`` pushed every top single to 95+, making 364k and 128k
    # listeners score within a point of each other).
    if is_single and combined > 0:
        combined *= 1.0 + (single_boost - 1.0) * single_boost_fade(combined)

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
