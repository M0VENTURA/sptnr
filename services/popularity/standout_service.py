"""Standout Detection and Star Rating Service

Config holder for standout detection and star rating thresholds, sourced from
the centralized config (``helpers.config_helpers.get_standout_config()``) so
the star-rating finalisation stage (``finalise_stage``) reads live values
merged from ``config.yaml`` ``single_detection`` — including the
``star_epsilon_score_points`` buffer and ``listener_5star_z_threshold``.

The legacy iterative-zscore / gap-detection functions that previously lived
here were purged — the scan pipeline uses ``popularity_math`` robust z-scores
and the unified star-rating logic in ``finalise_stage``.

Configuration:
    All thresholds are configurable via config.yaml under `single_detection`:

    ```yaml
    single_detection:
      album_zscore_threshold: 0.8
      artist_zscore_threshold: 2.2
      artist_top_percentile: 0.10
      artist_min_tracks: 10
      star_5:
        album_z: 1.0
        artist_z: 1.2
        artist_pct: 0.10
      star_4:
        album_z: 0.8
        artist_z: 1.0
        artist_pct: 0.20
    ```
"""

from __future__ import annotations

from helpers.config_helpers import get_standout_config

# Load configuration from centralized config (live source for finalise_stage)
STANDOUT_CONFIG = get_standout_config()
