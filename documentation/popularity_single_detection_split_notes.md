# Popularity and single-detection split notes

## Source documents reviewed

- `popularityhelpers.txt`
- `popularity1.txt`

## Main recommendation

Single detection should not live inside popularity scoring logic.

Popularity should:
- fetch provider popularity data
- normalize/scalculate popularity scores
- rank tracks
- apply star/standout rules

Single detection should:
- classify whether a track is a single
- use MusicBrainz / Discogs / Spotify evidence
- persist `is_single` if requested
- return structured source evidence

## New structure

```text
services/popularity/
├── __init__.py
├── popularity_config.py
├── popularity_math.py
├── popularity_matching.py
├── popularity_sources.py
├── popularity_adjustments.py
├── standout_service.py
└── popularity_scan_imports.py

services/enrichment/
└── single_detection_service.py

popularity_helpers.py
  Compatibility shim that re-exports the split modules.
```

## Function movement map

### popularity_math.py

- `calculate_track_zscore`
- `zscore_to_popularity`
- `calculate_lastfm_popularity_score`
- `calculate_lastfm_zscore_popularity`
- `calculate_listenbrainz_popularity_score`
- `calculate_combined_popularity_score`
- `calculate_listenbrainz_percentile`
- `adjust_weights`
- `score_by_age`

### popularity_matching.py

- `get_primary_artist_preserve_case`
- `get_artist_lookup_candidates`
- `make_artist_match_key`
- `make_track_match_key`
- `normalize_for_aggregation`
- `choose_best_provider_counts`

### popularity_sources.py

- `get_lastfm_track_info`
- `get_aggregated_lastfm_popularity`
- `get_listenbrainz_batch_for_tracks`
- `get_listenbrainz_popularity_for_track`
- `get_listenbrainz_score_for_track`
- `get_aggregated_listenbrainz_popularity`

### single_detection_service.py

- `detect_single_for_track`
- `strip_single_release_suffix`
- `should_skip_single_detection`

### standout_service.py

- `detect_via_iterative_zscore`
- `get_top_standout_tracks_with_gap`

## Scanner migration

In `popularity.py` / `popularity1.py`, remove the local
`detect_single_for_track(...)` and replace it with:

```python
from services.enrichment.single_detection_service import detect_single_for_track
```

The new `popularity_helpers.py` is a shim so old imports keep working while
you migrate call sites gradually.

## Important note

The generated files preserve the architectural split and common behaviour.
Your original popularity scanner is very large, so the safest migration is:

1. Add these files.
2. Replace `popularity_helpers.py` with the shim.
3. Update `popularity.py` to import `detect_single_for_track` from the new service.
4. Move remaining large scanner-only functions gradually into services.
