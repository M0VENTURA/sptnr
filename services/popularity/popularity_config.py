"""Popularity config and weights.

All reads go through ``helpers.config_helpers.get_config()`` — the single
cached config source (env overrides + config.yaml).  Runtime lookups
(``resolve_weights``, ``get_zscore_thresholds``, …) therefore pick up saved
config changes immediately; only the import-time module constants below are
fixed per process.
"""

from __future__ import annotations

from typing import Tuple

from helpers.config_helpers import get_config

DEFAULT_WEIGHTS = {
    "lastfm": 0.55,
    "listenbrainz": 0.35,
    "age": 0.10,
}


def resolve_weights(config: dict | None = None) -> Tuple[float, float, float]:
    """Resolve Last.fm, ListenBrainz and age weights from config.

    ``popularity.weights`` takes precedence (used by the scan pipeline's UI
    section); falls back to the top-level ``weights`` block.
    """
    cfg = config if isinstance(config, dict) else get_config()
    weights = cfg.get("popularity", {}).get("weights") if isinstance(cfg, dict) else None
    if not isinstance(weights, dict):
        weights = cfg.get("weights") if isinstance(cfg, dict) else None
    weights = weights or {}
    try:
        lastfm = float(weights.get("lastfm", DEFAULT_WEIGHTS["lastfm"]))
        listenbrainz = float(weights.get("listenbrainz", DEFAULT_WEIGHTS["listenbrainz"]))
        age = float(weights.get("age", DEFAULT_WEIGHTS["age"]))
    except Exception:
        lastfm, listenbrainz, age = DEFAULT_WEIGHTS["lastfm"], DEFAULT_WEIGHTS["listenbrainz"], DEFAULT_WEIGHTS["age"]

    total = lastfm + listenbrainz + age
    if total <= 0:
        return DEFAULT_WEIGHTS["lastfm"], DEFAULT_WEIGHTS["listenbrainz"], DEFAULT_WEIGHTS["age"]
    return lastfm / total, listenbrainz / total, age / total


def get_zscore_thresholds(config: dict | None = None) -> dict:
    """Load single-detection z-score confidence boundaries from config.

    Returns ``{'medium': 0.6, 'high': 1.0, 'standout_gap_z': 0.75}`` by
    default, matching the legacy ``get_zscore_thresholds`` helper.
    """
    cfg = config if isinstance(config, dict) else get_config()
    sd = cfg.get("single_detection", {}) if isinstance(cfg, dict) else {}
    if not isinstance(sd, dict):
        sd = {}
    return {
        "medium": float(sd.get("zscore_medium_threshold", 0.6)),
        "high": float(sd.get("zscore_high_threshold", 1.0)),
        "standout_gap_z": float(sd.get("standout_gap_z", 0.75)),
    }


def get_single_boost(config: dict | None = None) -> float:
    """Return the confirmed-single score boost multiplier (default 1.15)."""
    cfg = config if isinstance(config, dict) else get_config()
    sd = cfg.get("single_detection", {}) if isinstance(cfg, dict) else {}
    try:
        return float(sd.get("single_boost", 1.15))
    except Exception:
        return 1.15


def get_metadata_score_floor(config: dict | None = None) -> float:
    """Return the minimum score floor for tracks with confirmed metadata (default 5.0)."""
    cfg = config if isinstance(config, dict) else get_config()
    sd = cfg.get("single_detection", {}) if isinstance(cfg, dict) else {}
    try:
        return float(sd.get("metadata_score_floor", 5.0))
    except Exception:
        return 5.0


def get_live_weight_penalty(config: dict | None = None) -> float:
    """Return the Last.fm weight penalty fraction for live tracks (default 0.5)."""
    cfg = config if isinstance(config, dict) else get_config()
    sd = cfg.get("single_detection", {}) if isinstance(cfg, dict) else {}
    try:
        return float(sd.get("live_weight_penalty", 0.5))
    except Exception:
        return 0.5


def get_single_organic_floor(config: dict | None = None) -> tuple[float, float]:
    """Return the organic popularity floor gating single-driven star elevation.

    A metadata-tagged single (Discogs/MusicBrainz) with almost no organic
    audience must not leapfrog genuinely popular album tracks: single-driven
    elevation above 3★ (5★ award, 4★ Single Floor, era album-top-N) requires
    ``popularity_score >= score_floor`` OR ``Last.fm listeners >=
    listeners_floor``.  Defaults: ``(45.0, 1000)`` — configurable via
    ``single_detection.single_organic_floor_score`` /
    ``single_organic_floor_listeners``.
    """
    cfg = config if isinstance(config, dict) else get_config()
    sd = cfg.get("single_detection", {}) if isinstance(cfg, dict) else {}
    try:
        return (
            float(sd.get("single_organic_floor_score", 45.0)),
            float(sd.get("single_organic_floor_listeners", 1000)),
        )
    except Exception:
        return 45.0, 1000.0


DEFAULT_LOG_RATIO = {
    "enabled": True,
    "divergence_threshold": 0.85,
    "reject_lf_min_lb": 50,
    "reject_lb_min_lf": 100,
}


def get_log_ratio_config(config: dict | None = None) -> dict:
    """Return the Log-MAD cross-platform playcount audit settings.

    Reads ``single_detection.log_ratio_*`` keys:
    - ``log_ratio_enabled`` (default True) — master switch for the audit
    - ``log_ratio_divergence_threshold`` (default 0.85 ≈ 7x relative
      divergence) — a track's log10 LF/LB ratio must deviate by more than
      this from the album's median log ratio before it is rejected
    - ``log_ratio_reject_lf_min_lb`` (default 50) — minimum ListenBrainz
      listens before Last.fm is distrusted (LB must be healthy to trust)
    - ``log_ratio_reject_lb_min_lf`` (default 100) — minimum Last.fm
      listeners before ListenBrainz is distrusted

    A threshold of 1.00 corresponds to a 10x relative divergence (safe,
    conservative); 0.70 corresponds to ~5x.
    """
    cfg = config if isinstance(config, dict) else get_config()
    sd = cfg.get("single_detection", {}) if isinstance(cfg, dict) else {}
    if not isinstance(sd, dict):
        sd = {}
    try:
        enabled = bool(sd.get("log_ratio_enabled", DEFAULT_LOG_RATIO["enabled"]))
    except Exception:
        enabled = DEFAULT_LOG_RATIO["enabled"]
    try:
        threshold = float(sd.get("log_ratio_divergence_threshold", DEFAULT_LOG_RATIO["divergence_threshold"]))
    except Exception:
        threshold = DEFAULT_LOG_RATIO["divergence_threshold"]
    try:
        reject_lf_min_lb = int(sd.get("log_ratio_reject_lf_min_lb", DEFAULT_LOG_RATIO["reject_lf_min_lb"]))
    except Exception:
        reject_lf_min_lb = DEFAULT_LOG_RATIO["reject_lf_min_lb"]
    try:
        reject_lb_min_lf = int(sd.get("log_ratio_reject_lb_min_lf", DEFAULT_LOG_RATIO["reject_lb_min_lf"]))
    except Exception:
        reject_lb_min_lf = DEFAULT_LOG_RATIO["reject_lb_min_lf"]
    return {
        "enabled": enabled,
        "divergence_threshold": threshold,
        "reject_lf_min_lb": reject_lf_min_lb,
        "reject_lb_min_lf": reject_lb_min_lf,
    }
