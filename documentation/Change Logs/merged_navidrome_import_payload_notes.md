# Merged Navidrome import + payload builder notes

## Files included

```text
services/scanning/navidrome_import.py
services/scanning/payload_builder.py
services/scanning/metadata_extractor.py
```

## What was preserved from your current files

- `scan_artist_to_db(...)` can still be called without passing a client.
- `fetch_artist_albums(...)` and `fetch_album_tracks(...)` wrappers remain.
- `extract_and_backfill_track_metadata(...)` remains.
- `save_to_db(...)` remains, but it now routes to PostgreSQL-safe repository upsert.
- `build_track_payload(...)` still supports `extracted=` and `writer_json=`.

## What changed

- Metadata extraction moved into `metadata_extractor.py`.
- `payload_builder.py` can now extract metadata itself if `extracted` is omitted.
- `navidrome_import.py` now uses `upsert_track_payload(..., conn=conn)` instead of falling back to `popularity_helpers.save_to_db`.
- The import path is PostgreSQL-only.

## Recommended next step

Update newer call sites to pass a real configured `NavidromeClient` into:

```python
scan_artist_to_db(..., client=client)
```

The old no-client path still works for compatibility, but explicit client injection is cleaner.
