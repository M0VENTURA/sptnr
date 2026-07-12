# Navidrome API/client refactor map

## New ownership

```text
api_clients/navidrome.py
  HTTP endpoint wrappers only.

services/scanning/navidrome_service.py
  Scan-oriented orchestration helpers built on top of the API client.

services/scanning/metadata_extractor.py
  Track metadata/tag extraction from Navidrome track payloads.

services/scanning/payload_builder.py
  DB payload construction from extracted metadata.

services/scanning/navidrome_import.py
  Artist/album/track import workflow into PostgreSQL.
```

## What moved out of api_clients/navidrome.py

- `fetch_all_tracks_concurrently` -> `services/scanning/navidrome_service.py`
- `build_artist_index_from_albums` -> `services/scanning/navidrome_service.py`
- `build_artist_index` orchestration -> `services/scanning/navidrome_service.py`
- `get_library_stats` orchestration -> `services/scanning/navidrome_service.py`
- `extract_track_metadata` -> `services/scanning/metadata_extractor.py`

## Compatibility retained

`NavidromeClient.build_artist_index`, `NavidromeClient.get_library_stats`, and
`NavidromeClient.extract_track_metadata` remain as forwarding wrappers for now.

## PostgreSQL note

DB writes are not performed by the API client. DB writes go through
`db.repositories.tracks`.
