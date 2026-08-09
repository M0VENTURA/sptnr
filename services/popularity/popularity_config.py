"""Popularity config and weights."""

from __future__ import annotations

import os
import yaml
from typing import Any, Tuple

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/config.yaml")

DEFAULT_WEIGHTS = {
    "lastfm": 0.55,
    "listenbrainz": 0.35,
    "age": 0.10,
}

DEFAULT_FEATURES = {
    "scan_worker_threads": 4,
}


def load_config() -> dict:
    """Load config.yaml safely."""
    if not os.path.exists(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            return yaml.safe_load(handle) or {}
    except Exception:
        return {}


def resolve_weights(config: dict | None = None) -> Tuple[float, float, float]:
    """Resolve Last.fm, ListenBrainz and age weights from config.

    ``popularity.weights`` takes precedence (used by the scan pipeline's UI
    section); falls back to the top-level ``weights`` block.
    """
    cfg = config if isinstance(config, dict) else load_config()
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


LASTFM_WEIGHT, LISTENBRAINZ_WEIGHT, AGE_WEIGHT = resolve_weights()
SPOTIFY_WEIGHT = 0.0  # legacy alias retained


def get_zscore_thresholds(config: dict | None = None) -> dict:
    """Load single-detection z-score confidence boundaries from config.

    Returns ``{'medium': 0.6, 'high': 1.0, 'standout_gap_z': 0.75}`` by
    default, matching the legacy ``get_zscore_thresholds`` helper.
    """
    cfg = config if isinstance(config, dict) else load_config()
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
    cfg = config if isinstance(config, dict) else load_config()
    sd = cfg.get("single_detection", {}) if isinstance(cfg, dict) else {}
    try:
        return float(sd.get("single_boost", 1.15))
    except Exception:
        return 1.15


def get_metadata_score_floor(config: dict | None = None) -> float:
    """Return the minimum score floor for tracks with confirmed metadata (default 5.0)."""
    cfg = config if isinstance(config, dict) else load_config()
    sd = cfg.get("single_detection", {}) if isinstance(cfg, dict) else {}
    try:
        return float(sd.get("metadata_score_floor", 5.0))
    except Exception:
        return 5.0


def get_live_weight_penalty(config: dict | None = None) -> float:
    """Return the Last.fm weight penalty fraction for live tracks (default 0.5)."""
    cfg = config if isinstance(config, dict) else load_config()
    sd = cfg.get("single_detection", {}) if isinstance(cfg, dict) else {}
    try:
        return float(sd.get("live_weight_penalty", 0.5))
    except Exception:
        return 0.5


# ---------------------------------------------------------------------------
# Standout / star-rating threshold config
# ---------------------------------------------------------------------------

STANDOUT_CONFIG: dict[str, Any] = {
    "album_zscore_threshold": 0.8,
    "artist_zscore_threshold": 2.2,
    "artist_top_percentile": 0.10,
    "artist_medium_bump_percentile": 0.20,
    "artist_min_tracks": 10,
    "star_5": {"album_z": 1.0, "artist_z": 1.2, "artist_pct": 0.10},
    "star_4": {"album_z": 0.5, "artist_z": 1.0, "artist_pct": 0.20},
    "star_5_single": {"artist_pct": 0.25},
    "popularity_5star_z_threshold": 2.0,
    "lb_unreliable_5star_threshold": 0.50,
    "star_3": {"album_z": -0.5},
    "star_2": {"album_z": -1.2},
    "star_1": {"album_z": -1.2, "default": True},
}


def apply_standout_config_overrides(config: dict | None = None) -> None:
    """Apply standout/star-rating config overrides from config.yaml.

    Reads the ``single_detection`` section from config and updates
    ``STANDOUT_CONFIG`` in-place.  Safe to call multiple times.
    """
    cfg = config if isinstance(config, dict) else load_config()
    sd = cfg.get("single_detection", {})
    if not isinstance(sd, dict):
        return

    for key in ("album_zscore_threshold", "artist_zscore_threshold",
                 "artist_top_percentile", "artist_medium_bump_percentile",
                 "artist_min_tracks"):
        if key in sd:
            STANDOUT_CONFIG[key] = sd[key]

    for star_key in ("star_5", "star_4", "star_3", "star_2"):
        overrides = sd.get(star_key)
        if isinstance(overrides, dict):
            STANDOUT_CONFIG.setdefault(star_key, {}).update(overrides)

    # Single-detection specific overrides
    standout_gap_z = sd.get("standout_gap_z")
    if standout_gap_z is not None:
        STANDOUT_CONFIG.setdefault("star_5", {})["standout_gap_z"] = float(standout_gap_z)
