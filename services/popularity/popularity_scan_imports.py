"""Recommended import block for the trimmed popularity scanner.

Replace the large popularity_helpers import block with imports from this file
while migrating. This file intentionally re-exports the split services to keep
the scanner changes small.
"""

from services.popularity.popularity_math import (
    calculate_lastfm_popularity_score,
    calculate_lastfm_zscore_popularity,
    calculate_listenbrainz_popularity_score,
    calculate_listenbrainz_percentile,
    calculate_combined_popularity_score,
    score_by_age,
    adjust_weights,
    is_lastfm_unreliable,
)
from services.popularity.popularity_sources import (
    get_lastfm_track_info,
    get_listenbrainz_batch_for_tracks,
    get_aggregated_listenbrainz_popularity,
    get_aggregated_lastfm_popularity,
)
from services.enrichment.single_detection_service import detect_single_for_track
