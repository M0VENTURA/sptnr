# Popularity Helpers Import Migration

## Summary
Imported key functions from `popularity_helpers.py` into the appropriate service files in the `services/enrichment/` directory.

## Imports Added

### 1. `services/enrichment/lastfm_service.py`

Added imports for Last.fm-specific aggregation and artist matching utilities:

```python
from popularity_helpers import (
    get_aggregated_lastfm_popularity,      # Fast catalog-based Last.fm lookups (90% faster)
    choose_best_provider_counts,           # Select best result across artist variants
    get_primary_artist_preserve_case,      # Strip featured artists while preserving casing
    get_artist_lookup_candidates,          # Build provider lookup candidates
    make_artist_match_key,                 # Create internal matching keys (lowercase)
    make_track_match_key,                  # Create canonical track matching keys
)
```

**Rationale**: These functions support Last.fm API interactions, artist matching, and data aggregation. While `LastFmService` has its own `build_artist_lookup_candidates()` method, the imported functions provide additional utilities for cross-service consistency and the powerful `get_aggregated_lastfm_popularity()` function that caches entire artist catalogs for 90% faster lookups.

**Usage Examples**:
- `get_aggregated_lastfm_popularity()` - Use for bulk popularity scans to cache artist's top 1000 tracks in one API call
- `choose_best_provider_counts()` - Use when comparing results from multiple artist name variants
- `make_artist_match_key()` / `make_track_match_key()` - Use for consistent internal caching/grouping

---

### 2. `services/enrichment/listenbrainz_service.py`

Added imports for ListenBrainz-specific aggregation utilities:

```python
from popularity_helpers import (
    get_aggregated_listenbrainz_popularity,  # Aggregate stats across MBID variants
    normalize_for_aggregation,               # Normalize titles for variant matching
)
```

**Rationale**: ListenBrainz data is often fragmented across multiple MusicBrainz recording MBIDs for the same song (different versions, features, etc.). The `get_aggregated_listenbrainz_popularity()` function discovers all variant MBIDs and sums the listen counts, providing accurate total popularity.

**Usage Example**:
```python
# Instead of single MBID lookup:
result = client.get_recording_popularity(mbid)

# Use aggregation to capture all variants:
result = get_aggregated_listenbrainz_popularity(
    title="Song Title",
    artist="Artist Name",
    primary_mbid="xxx-xxx-xxx",
    lb_client=listenbrainz_client,
    mb_client=musicbrainz_client
)
# Returns summed listen_count across all discovered variants
```

---

## Functions Already Migrated (No Action Needed)

These functions were already re-implemented in the services layer:

### `services/popularity/scoring.py`
- ✅ `calculate_lastfm_popularity_score`
- ✅ `calculate_listenbrainz_popularity_score`
- ✅ `calculate_combined_popularity_score`

### `services/popularity/standout_service.py`
- ✅ `detect_via_iterative_zscore`
- ✅ `get_top_standout_tracks_with_gap`

### `services/popularity/popularity_stats_service.py`
- ✅ `calculate_artist_popularity_stats`
- ✅ `should_exclude_from_stats`
- ✅ `is_top_artist_catalog_score`

---

## Functions Kept in `popularity_helpers.py`

The following functions remain in `popularity_helpers.py` for backward compatibility with legacy code but could be considered for future migration:

### Database Helpers
- `save_to_db` - Track persistence with duplicate prevention
- `build_artist_index` - Navidrome artist index builder
- `load_artist_map` - Load artist stats from DB
- `get_album_last_scanned_from_db` - Scan history lookup
- `get_album_track_count_in_db` - Album track count
- `update_artist_id_for_artist` - Bulk Spotify ID update
- `update_discogs_artist_id_for_artist` - Bulk Discogs ID update

### Client Configuration
- `configure_popularity_helpers` - Initialize API clients
- `get_spotify_client` - Get configured Spotify client
- `get_lastfm_client` - Get configured Last.fm client

### Spotify Helpers
- `get_spotify_artist_id` - Get Spotify artist ID with DB caching
- `get_spotify_artist_single_track_ids` - Fetch artist singles
- `search_spotify_track` - Search Spotify tracks

### ListenBrainz Batch Operations
- `get_listenbrainz_batch_for_tracks` - Batch MBID lookups
- `get_listenbrainz_popularity_for_track` - Single track lookup
- `get_listenbrainz_score_for_track` - Raw listen count

### Scoring Utilities
- `calculate_lastfm_zscore_popularity` - Album-relative z-score
- `calculate_listenbrainz_percentile` - Track percentile within album
- `is_source_mismatch` - Detect Last.fm vs ListenBrainz conflicts
- `is_lastfm_unreliable` - Flag unreliable Last.fm data
- `adjust_weights` - Dynamic weight adjustment

### Popularity Adjustments
- `apply_mean_popularity_adjustment` - Artist-context z-score adjustment
- `apply_album_deviation_adjustment` - Album-level deviation adjustment

### Navidrome Wrappers
- `fetch_artist_albums` - Navidrome album fetcher
- `fetch_album_tracks` - Navidrome track fetcher

---

## Next Steps

### Immediate
- ✅ Imports added to `lastfm_service.py`
- ✅ Imports added to `listenbrainz_service.py`
- ✅ No breaking changes to existing code

### Future Considerations
1. **Deprecation Plan**: Consider deprecating `popularity_helpers.py` functions once all legacy code is migrated
2. **Service Expansion**: Move remaining database helpers to appropriate services:
   - `save_to_db` → `services/catalog/track_persistence_service.py` (new)
   - `build_artist_index` → `services/infrastructure/indexing_service.py` (new)
3. **Testing**: Add unit tests for newly imported functions in their service contexts
4. **Documentation**: Update service docstrings to reference imported capabilities

---

## Validation

All imports validated:
- ✅ No syntax errors introduced
- ✅ Pre-existing type checking warnings unchanged
- ✅ Backward compatibility maintained (legacy code can still import from `popularity_helpers`)
- ✅ Services now have access to optimized aggregation functions

---

**Date**: 2026-07-10  
**Migration Status**: Phase 1 Complete (Service imports added)
