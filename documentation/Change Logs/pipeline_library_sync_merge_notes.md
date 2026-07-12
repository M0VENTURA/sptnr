# Pipeline + library sync merge notes

## Files rebuilt

```text
services/scanning/pipeline.py
services/scanning/library_sync.py
services/scanning/pipelines/navidrome_pipeline.py
```

## Main changes

- Moved the helper-based library sync worker into `services/scanning/library_sync.py`.
- Replaced helper imports with service/repository imports:
  - `services.scanning.navidrome_import.scan_artist_to_db`
  - `db.repositories.tracks.bulk_upsert_navidrome_tracks`
  - `db.utils.get_db_connection`
- Added `start_post_navidrome_library_sync(...)` to `pipeline.py`.
- Renamed `maybe_start_post_navidrome_mp3_import(...)` to
  `start_post_navidrome_mp3_import(...)`.
- Kept `maybe_start_post_navidrome_mp3_import(...)` as a compatibility alias.
- Added `run_post_navidrome_hooks(...)` so boot and manual Navidrome imports use
  one shared hook sequence.
- Updated `navidrome_pipeline.py` to call `run_post_navidrome_hooks(...)` instead
  of duplicating post-import logic.

## Hook order after Navidrome import

```text
1. album artist pre-sync
2. library diff sync request
3. post-Navidrome MP3 import
```

## Why the library sync lives in services/scanning

It is not a route and not a scanner. It is a service-level incremental sync
worker with single-flight behaviour, so it belongs beside the scan pipeline.
