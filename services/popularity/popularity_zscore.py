"""Shared listener-count z-score helpers for popularity scoring.

Used by single detection (``services/enrichment/single_detection_service``)
and the star-rating finalisation stage (``services/popularity/stages/
finalise_stage``) — previously duplicated as ``_log_listener_z`` /
``_composite_album_z`` / ``_listener_z`` / ``_composite_listener_z`` in each
module.  Both stages log-scale listener/listen counts (right-skewed: one hit
dominates an album) and blend Last.fm / ListenBrainz z-scores against the
album's own tracklist distribution.
"""

from __future__ import annotations

import math
from statistics import mean, stdev

import structlog

logger = structlog.get_logger(__name__)

# Log-space noise floor for ``log_listener_z``.  On a UNIFORM album (all
# counts near-identical, stdev → 0) the z formula amplifies tiny scrobble
# gaps into huge swings — a 10-scrobble difference across a tracklist of
# ~10k-count tracks is measurement noise, not a standout signal.  The sigma
# is floored at a small absolute value AND a fraction of the mean log so
# low-variance albums at any magnitude damp the same relative noise.
LOG_LISTENER_Z_MIN_SIGMA = 0.05
LOG_LISTENER_Z_RELATIVE_SIGMA = 0.02


def log_listener_z(count: float, counts: list[float]) -> float:
    """Log-scaled z-score of a track's listener/listen count within its album.

    Listener counts are heavily right-skewed (one hit dominates), so the
    z-score is computed over ``log1p``-transformed values.  This is the raw
    "standout within the album" signal that blended/decay-adjusted popularity
    scores compress.  Zero-variance distributions (all counts identical, or
    fewer than 3 positive counts) carry no outlier signal — return 0.0.
    """
    logs = [math.log1p(float(c)) for c in (counts or []) if float(c) > 0]
    if len(logs) < 3 or not any(logs):
        return 0.0
    mu = mean(logs)
    sigma = stdev(logs) if len(logs) > 1 else 1.0
    if not sigma or sigma <= 0:
        return 0.0
    sigma = max(sigma, LOG_LISTENER_Z_MIN_SIGMA, LOG_LISTENER_Z_RELATIVE_SIGMA * abs(mu))
    return (math.log1p(float(count)) - mu) / sigma


def composite_listener_z(
    lastfm_listeners: int | float | None,
    listenbrainz_listens: int | float | None,
    artist: str | None = None,
    album: str | None = None,
    album_lf_listeners: list[float] | None = None,
    album_lb_listens: list[float] | None = None,
) -> float:
    """Album-local composite z-score from the track's raw LF/LB counts.

    ``z_composite = (w_LF * z_LF + w_LB * z_LB) / (w_LF + w_LB)`` where each
    provider z is the track's log-scaled z within ITS ALBUM'S OWN tracklist
    distribution (never the artist catalogue).  When only one provider has
    usable data the composite collapses to that provider's z (the "LB
    invalid/bypassed → score on Last.fm only" behaviour).

    Caller-supplied album distributions are used when provided; otherwise the
    album's stored listener counts are loaded from the DB (keyed by the album
    artist, matching every other popularity-stats lookup).  Returns 0.0 when
    there is not enough album data for a meaningful signal.
    """
    try:
        if album and (album_lf_listeners is None or album_lb_listens is None):
            from services.popularity.popularity_stats_service import calculate_album_listener_stats
            _db_lf, _db_lb = calculate_album_listener_stats(None, artist, album)
            if album_lf_listeners is None:
                album_lf_listeners = _db_lf
            if album_lb_listens is None:
                album_lb_listens = _db_lb
        z_lf = log_listener_z(float(lastfm_listeners or 0), album_lf_listeners or [])
        z_lb = log_listener_z(float(listenbrainz_listens or 0), album_lb_listens or [])
        if not z_lf and not z_lb:
            return 0.0
        if not z_lb:
            return z_lf
        if not z_lf:
            return z_lb
        try:
            from services.popularity.popularity_config import resolve_weights
            w_lf, w_lb, _ = resolve_weights()
        except Exception:
            w_lf, w_lb = 0.6, 0.4
        total = w_lf + w_lb
        if total <= 0:
            return z_lf
        return (w_lf * z_lf + w_lb * z_lb) / total
    except Exception as exc:
        logger.debug(
            "Composite listener z failed",
            artist=artist,
            album=album,
            error=str(exc),
        )
        return 0.0