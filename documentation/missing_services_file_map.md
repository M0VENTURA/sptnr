# Missing services delta

This pack adds the service/repository files that were still represented in the old
`popularityhelpers.py`, `popularity.py`, and `matching_utils.py` surfaces.

## Matching
- `services/matching/track_matching.py`

## Metadata / catalogue classification
- `services/metadata/title_normalization_service.py`
- `services/catalog/album_classification_service.py`

## Popularity support
- `services/popularity/popularity_cache_policy.py`
- `services/popularity/popularity_stats_service.py`
- `services/popularity/progress_tracker.py`
- `db/repositories/popularity_repository.py`

## Scanning / Navidrome
- `services/scanning/navidrome_scan_service.py`
- `services/scanning/scan_resume_service.py`
- `services/navidrome/rating_sync_service.py`

## Enrichment
- `services/enrichment/genre_aggregation_service.py`
- `services/enrichment/cover_detection_service.py`
- `services/enrichment/single_detection_context_service.py`
- `services/enrichment/spotify_metadata_service.py`
- `services/enrichment/album_art_service.py`
- `api_clients/discogs_http.py`
- `services/enrichment/discogs_service.py`

## Infrastructure / playlists / library
- `services/infrastructure/timeout_executor.py`
- `services/playlists/playlist_service.py`
- `services/library/library_sync_service.py`
- `routes/library_routes.py`

