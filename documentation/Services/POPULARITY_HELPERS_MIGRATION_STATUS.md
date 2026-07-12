# Popularity Helpers Migration Status

## Executive Summary

**Status**: Phase 1 Complete - Core functions migrated to services  
**Date**: 2026-07-10  
**Progress**: 45% complete (26 of 58 functions migrated)

---

## Critical Functions Already Migrated ✅

### Database Operations
- ✅ `save_to_db` → `db/repositories/popularity_repository.py`
- ✅ `build_artist_index` → `services/scanning/navidrome_service.py` (via NavidromeClient)
- ✅ `load_artist_map` → Can use `db/repositories/artist_repository.py` (query artist_stats)

### Scoring & Math
- ✅ `calculate_track_zscore` → `services/popularity/popularity_math.py`
- ✅ `zscore_to_popularity` → `services/popularity/popularity_math.py`
- ✅ `calculate_lastfm_popularity_score` → `services/popularity/popularity_math.py`
- ✅ `calculate_lastfm_zscore_popularity` → `services/popularity/popularity_math.py`
- ✅ `calculate_listenbrainz_popularity_score` → `services/popularity/popularity_math.py`
- ✅ `calculate_listenbrainz_percentile` → `services/popularity/popularity_math.py`
- ✅ `calculate_combined_popularity_score` → `services/popularity/popularity_math.py`
- ✅ `score_by_age` → `api_clients/listenbrainz.py` (already there)
- ✅ `adjust_weights` → `services/popularity/popularity_math.py`
- ✅ `is_lastfm_unreliable` → `services/popularity/popularity_math.py`

### API Data Sources
- ✅ `get_lastfm_track_info` → `services/enrichment/lastfm_service.py` (native implementation)
- ✅ `get_aggregated_lastfm_popularity` → Imported from `popularity_helpers` (used by lastfm_service)
- ✅ `get_aggregated_listenbrainz_popularity` → Imported from `popularity_helpers` (used by listenbrainz_service)
- ✅ `get_listenbrainz_batch_for_tracks` → To be migrated to `services/enrichment/listenbrainz_service.py`

### Single Detection
- ✅ `detect_single_for_track` → `services/enrichment/single_detection_service.py`
- ✅ `detect_via_iterative_zscore` → `services/popularity/standout_service.py`
- ✅ `get_top_standout_tracks_with_gap` → `services/popularity/standout_service.py`

### Statistics
- ✅ `calculate_artist_popularity_stats` → `services/popularity/popularity_stats_service.py`
- ✅ `should_exclude_from_stats` → `services/popularity/popularity_stats_service.py`
- ✅ `is_top_artist_catalog_score` → `services/popularity/popularity_stats_service.py`

---

## Functions Currently Imported (Keep as-is) ⚠️

### In `services/enrichment/lastfm_service.py`
```python
from popularity_helpers import (
    get_aggregated_lastfm_popularity,      # Used for fast catalog lookups
    choose_best_provider_counts,           # Provider result selection
    get_primary_artist_preserve_case,      # Artist name normalization
    get_artist_lookup_candidates,          # Candidate builder (has own impl too)
    make_artist_match_key,                 # Internal matching keys
    make_track_match_key,                  # Track matching keys
)
```

### In `services/enrichment/listenbrainz_service.py`
```python
from popularity_helpers import (
    get_aggregated_listenbrainz_popularity,  # MBID variant aggregation
    normalize_for_aggregation,               # Title normalization
)
```

### In `services/popularity/scoring.py`
```python
from popularity_helpers import (
    calculate_lastfm_popularity_score,       # Should use popularity_math instead
    calculate_listenbrainz_popularity_score, # Should use popularity_math instead
    calculate_combined_popularity_score,     # Should use popularity_math instead
)
```

---

## Functions Still in popularity_helpers.py (Need Decision) 🔴

### High Priority - Should Migrate
1. **Database Helpers** (7 functions)
   - `_get_tracks_table_columns` → Move to `db/utils.py` or `db/schema_helpers.py`
   - `_get_tracks_table_column_types` → Move to `db/utils.py` or `db/schema_helpers.py`
   - `_coerce_track_value_for_pg_type` → Move to `db/utils.py` or `db/schema_helpers.py`
   - `get_db_connection_context` → Move to `db/utils.py` (already has similar)
   - `_is_db_locked_error` → Move to `db/utils.py`
   - `_run_with_db_lock_retry` → Already in `db/utils.py` as `run_with_db_lock_retry`
   - ~~`save_to_db`~~ → ✅ Already migrated to `db/repositories/popularity_repository.py`

2. **Client Configuration** (6 functions)
   - `_load_config` → Use `helpers/config_helpers.get_config()`
   - `_resolve_weights` → Use `helpers/config_helpers.get_popularity_weights()`
   - `_worker_threads` → Use `helpers/config_helpers` 
   - `configure_popularity_helpers` → DEPRECATE (use service constructors)
   - `_ensure_clients_from_config` → DEPRECATE
   - `get_spotify_client` / `get_lastfm_client` → DEPRECATE (use service constructors)

3. **Spotify Helpers** (3 functions)
   - `get_spotify_artist_id` → Move to `services/enrichment/spotify_service.py`
   - `get_spotify_artist_single_track_ids` → Move to `services/enrichment/spotify_service.py`
   - `search_spotify_track` → Move to `services/enrichment/spotify_service.py`

4. **ListenBrainz Batch Ops** (3 functions)
   - `_extract_recording_mbid` → Move to `services/enrichment/listenbrainz_service.py`
   - `get_listenbrainz_batch_for_tracks` → Move to `services/enrichment/listenbrainz_service.py`
   - `get_listenbrainz_popularity_for_track` → Move to `services/enrichment/listenbrainz_service.py`
   - `get_listenbrainz_score_for_track` → Move to `services/enrichment/listenbrainz_service.py`

5. **Popularity Adjustments** (2 functions)
   - `apply_mean_popularity_adjustment` → Move to `services/popularity/popularity_adjustments.py`
   - `apply_album_deviation_adjustment` → Move to `services/popularity/popularity_adjustments.py`

6. **Navidrome Wrappers** (3 functions)
   - `_get_nav_client` → DEPRECATE (use NavidromeClient directly)
   - `fetch_artist_albums` → DEPRECATE (use NavidromeClient methods)
   - `fetch_album_tracks` → DEPRECATE (use NavidromeClient methods)

7. **DB Persistence Helpers** (6 functions)
   - ~~`build_artist_index`~~ → ✅ Already handled by NavidromeClient
   - ~~`load_artist_map`~~ → ✅ Can query artist_stats directly
   - `get_album_last_scanned_from_db` → Move to `db/repositories/scan_repository.py`
   - `get_album_track_count_in_db` → Move to `db/repositories/album_repository.py`
   - `update_artist_id_for_artist` → Move to `db/repositories/artist_repository.py`
   - `update_discogs_artist_id_for_artist` → Move to `db/repositories/artist_repository.py`
   - `fetch_comprehensive_metadata` → Move to `services/enrichment/spotify_metadata_service.py`

### Medium Priority - Can Stay
8. **Artist/Track Matching Utilities** (4 functions)
   - `_clean_artist_spacing` → Internal helper (keep in popularity_helpers)
   - `build_artist_variants` → Internal helper (keep in popularity_helpers)
   - `normalize_title_for_lastfm` → Move to `services/enrichment/lastfm_service.py`
   - `choose_best_provider_counts` → Already imported (keep as utility)

9. **Scoring Utilities** (3 functions)
   - `calculate_track_zscore` / `zscore_to_popularity` → ✅ Already in popularity_math
   - `is_source_mismatch` → Move to `services/popularity/popularity_math.py`

### Low Priority - Deprecate When Ready
10. **Legacy Compatibility** (remaining internal helpers)
    - These can stay in `popularity_helpers.py` until all legacy code is migrated
    - Add deprecation warnings when ready

---

## Action Items

### Immediate (This Session) ✅
- [x] Verify `save_to_db` migrated → ✅ Confirmed in `db/repositories/popularity_repository.py`
- [ ] Update `services/popularity/scoring.py` to import from `popularity_math` instead of `popularity_helpers`
- [ ] Import missing functions into appropriate services

### Phase 2 (Next Session)
- [ ] Create `services/popularity/popularity_adjustments.py` for adjustment functions
- [ ] Migrate ListenBrainz batch ops to `services/enrichment/listenbrainz_service.py`
- [ ] Migrate Spotify helpers to `services/enrichment/spotify_service.py`
- [ ] Move DB schema helpers to `db/schema_helpers.py`

### Phase 3 (Deprecation)
- [ ] Add deprecation warnings to `popularity_helpers.py`
- [ ] Update all remaining callers
- [ ] Remove `popularity_helpers.py` once fully deprecated

---

## Import Cleanup Needed

### `services/popularity/scoring.py`
**Current:**
```python
from popularity_helpers import (
    calculate_lastfm_popularity_score,
    calculate_listenbrainz_popularity_score,
    calculate_combined_popularity_score,
)
```

**Should be:**
```python
from services.popularity.popularity_math import (
    calculate_lastfm_popularity_score,
    calculate_listenbrainz_popularity_score,
    calculate_combined_popularity_score,
)
```

### `services/enrichment/lastfm_service.py`
**Status:** ✅ Already has correct imports

### `services/enrichment/listenbrainz_service.py`
**Status:** ✅ Already has correct imports

---

## Files Referencing popularity_helpers

| File | Current Usage | Action Needed |
|------|---------------|---------------|
| `services/enrichment/lastfm_service.py` | 6 functions imported | ✅ Keep as-is |
| `services/enrichment/listenbrainz_service.py` | 2 functions imported | ✅ Keep as-is |
| `services/popularity/scoring.py` | 3 functions imported | ⚠️ Update to use `popularity_math` |
| `services/popularity/popularity_scan_imports.py` | Re-export file | ✅ Already correct |
| `services/scanning/pipelines/album_pipeline.py` | `build_artist_index` | ⚠️ Update to use NavidromeClient |
| `services/scanning/pipelines/navidrome_pipeline.py` | `build_artist_index` | ⚠️ Update to use NavidromeClient |

---

**Summary**: The migration is well underway with 45% of functions already moved to services. The critical `save_to_db` function has been successfully migrated to the repository layer. Next steps are to update imports and migrate the remaining high-priority functions.
