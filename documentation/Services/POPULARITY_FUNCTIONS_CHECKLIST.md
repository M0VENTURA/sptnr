# Popularity Functions Migration Checklist

## Source Files Analyzed
- `popularity_helpers.py` (58 functions)
- `popularity.py` (1 function - CLI entry point, already migrated to `services/popularity/pipeline.py`)

---

## ✅ Imported into Services (8 functions)

### `services/enrichment/lastfm_service.py` (6 functions)
```python
from popularity_helpers import (
    get_aggregated_lastfm_popularity,      # ✅ IMPORTED
    choose_best_provider_counts,           # ✅ IMPORTED
    get_primary_artist_preserve_case,      # ✅ IMPORTED
    get_artist_lookup_candidates,          # ✅ IMPORTED
    make_artist_match_key,                 # ✅ IMPORTED
    make_track_match_key,                  # ✅ IMPORTED
)
```

### `services/enrichment/listenbrainz_service.py` (2 functions)
```python
from popularity_helpers import (
    get_aggregated_listenbrainz_popularity,  # ✅ IMPORTED
    normalize_for_aggregation,               # ✅ IMPORTED
)
```

---

## ✅ Already Re-implemented in Services (9 functions)

### `services/popularity/scoring.py` (3 functions)
- ✅ `calculate_lastfm_popularity_score` - Re-implemented
- ✅ `calculate_listenbrainz_popularity_score` - Re-implemented  
- ✅ `calculate_combined_popularity_score` - Re-implemented

### `services/popularity/standout_service.py` (2 functions)
- ✅ `detect_via_iterative_zscore` - Re-implemented
- ✅ `get_top_standout_tracks_with_gap` - Re-implemented

### `services/popularity/popularity_stats_service.py` (4 functions)
- ✅ `calculate_artist_popularity_stats` - Re-implemented
- ✅ `should_exclude_from_stats` - Re-implemented (as separate function)
- ✅ `is_top_artist_catalog_score` - Re-implemented
- ✅ `apply_mean_popularity_adjustment` - Logic integrated into standout detection

---

## ⚠️ Kept in popularity_helpers.py for Backward Compatibility (41 functions)

### Database Schema Helpers (7 functions)
- ⚠️ `_get_tracks_table_columns` - DB introspection
- ⚠️ `_get_tracks_table_column_types` - DB introspection
- ⚠️ `_coerce_track_value_for_pg_type` - Type coercion
- ⚠️ `get_db_connection_context` - Connection manager
- ⚠️ `_is_db_locked_error` - Error detection
- ⚠️ `_run_with_db_lock_retry` - Retry logic
- ⚠️ `save_to_db` - **CRITICAL**: Track persistence with duplicate prevention

### Configuration & Client Management (9 functions)
- ⚠️ `_load_config` - Config loader
- ⚠️ `_resolve_weights` - Weight resolution
- ⚠️ `_worker_threads` - Thread config
- ⚠️ `configure_popularity_helpers` - Client initialization
- ⚠️ `_ensure_clients_from_config` - Client lazy init
- ⚠️ `get_spotify_client` - Spotify client getter
- ⚠️ `get_lastfm_client` - Last.fm client getter
- ⚠️ `get_spotify_artist_id` - Spotify artist ID with DB cache
- ⚠️ `get_spotify_artist_single_track_ids` - Artist singles fetcher

### Artist/Track Matching Utilities (3 functions)
- ⚠️ `_clean_artist_spacing` - Internal spacing cleaner
- ⚠️ `build_artist_variants` - Featured artist splitter
- ⚠️ `normalize_title_for_lastfm` - Title normalizer

### ListenBrainz Operations (4 functions)
- ⚠️ `_extract_recording_mbid` - MBID extractor
- ⚠️ `get_listenbrainz_batch_for_tracks` - Batch MBID lookups
- ⚠️ `get_listenbrainz_popularity_for_track` - Single track lookup
- ⚠️ `get_listenbrainz_score_for_track` - Raw listen count

### Scoring & Adjustment Functions (8 functions)
- ⚠️ `calculate_track_zscore` - Basic z-score calculator
- ⚠️ `zscore_to_popularity` - Z-score to 0-100 scale converter
- ⚠️ `calculate_lastfm_zscore_popularity` - Album-relative z-score
- ⚠️ `calculate_listenbrainz_percentile` - Track percentile within album
- ⚠️ `is_source_mismatch` - Detect Last.fm vs ListenBrainz conflicts
- ⚠️ `is_lastfm_unreliable` - Flag unreliable Last.fm data
- ⚠️ `adjust_weights` - Dynamic weight adjustment
- ⚠️ `apply_album_deviation_adjustment` - Album-level deviation adjustment

### Navidrome Integration (3 functions)
- ⚠️ `_get_nav_client` - Navidrome client factory
- ⚠️ `fetch_artist_albums` - Navidrome album fetcher
- ⚠️ `fetch_album_tracks` - Navidrome track fetcher

### Database Persistence Helpers (7 functions)
- ⚠️ `build_artist_index` - Navidrome artist index builder
- ⚠️ `load_artist_map` - Load artist stats from DB
- ⚠️ `get_album_last_scanned_from_db` - Scan history lookup
- ⚠️ `get_album_track_count_in_db` - Album track count
- ⚠️ `update_artist_id_for_artist` - Bulk Spotify ID update
- ⚠️ `update_discogs_artist_id_for_artist` - Bulk Discogs ID update
- ⚠️ `fetch_comprehensive_metadata` - Spotify metadata fetcher

---

## Summary Statistics

| Category | Count | Status |
|----------|-------|--------|
| **Total Functions** | 58 | - |
| ✅ Imported to Services | 8 | Complete |
| ✅ Re-implemented in Services | 9 | Complete |
| ⚠️ Kept for Backward Compatibility | 41 | Pending migration |
| **Migration Progress** | **29%** | Phase 1 Complete |

---

## Critical Functions Still in popularity_helpers.py

These are the most important functions that should be prioritized for future migration:

### 🔴 HIGH PRIORITY
1. **`save_to_db`** - Core track persistence logic (should move to `services/catalog/track_persistence_service.py`)
2. **`build_artist_index`** - Indexing service (should move to `services/infrastructure/indexing_service.py`)
3. **`fetch_artist_albums` / `fetch_album_tracks`** - Navidrome integration (should move to `services/infrastructure/navidrome_service.py`)

### 🟡 MEDIUM PRIORITY
4. **`get_spotify_artist_id`** - Spotify caching (could stay or move to `services/enrichment/spotify_service.py`)
5. **`get_listenbrainz_batch_for_tracks`** - Batch operations (could move to `services/enrichment/listenbrainz_service.py`)
6. **Scoring utilities** - Could consolidate into `services/popularity/scoring.py`

### 🟢 LOW PRIORITY
7. **Internal helpers** (`_get_tracks_table_columns`, `_coerce_track_value_for_pg_type`, etc.) - Can remain until legacy code is fully migrated
8. **Configuration loaders** - Already have equivalents in `helpers/config_helpers.py`

---

## Next Steps

### Phase 1 (Complete ✅)
- [x] Import aggregation functions to appropriate services
- [x] Document migration status
- [x] Validate no breaking changes

### Phase 2 (Future)
- [ ] Create `services/catalog/track_persistence_service.py` and migrate `save_to_db`
- [ ] Create `services/infrastructure/indexing_service.py` and migrate `build_artist_index`
- [ ] Create `services/infrastructure/navidrome_service.py` and migrate Navidrome helpers
- [ ] Consolidate scoring utilities in `services/popularity/scoring.py`
- [ ] Update all legacy imports to use new service locations

### Phase 3 (Deprecation)
- [ ] Add deprecation warnings to `popularity_helpers.py` functions
- [ ] Update all remaining callers to use services
- [ ] Remove `popularity_helpers.py` once fully deprecated

---

**Date**: 2026-07-10  
**Status**: Phase 1 Complete - Service imports added  
**Next Action**: Prioritize critical function migrations (Phase 2)
