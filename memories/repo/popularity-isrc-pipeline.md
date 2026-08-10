# Popularity ISRC Pipeline + RapidFuzz Map

## ISRC pipeline (implemented 2026-08-10)
- MB resolution: `musicbrainz_service.py::_first_isrc` + `_recording_to_metadata` returns `isrc`
  (search docs + recording entities expose `isrcs` array)
- Composite Last.fm fetch: `popularity_sources.py::get_aggregated_lastfm_popularity`
  — primary → ISRC arm → inverted artist arm; MAX wins; returns `sources_queried`/`variant_detail`
- `invert_featured_artist()` (feat./ft./featuring only — NOT &/x: "Hall & Oates" risk)
- `resolve_isrc_recording()` = `MusicBrainzHttpClient.lookup_by_isrc(isrc)` (exact ISRC→recording)
- Last.fm has NO ISRC API — the ISRC arm queries `track.getInfo?mbid=<recording_mbid>`
  (`LastFmClient.get_track_info(track_mbid=...)`); recording title/artist from the ISRC lookup
- Track stage: `[ISRC_POOL] Found ISRC` log; LB resolution tries ISRC before `get_suggested_mbid`
  (source=`isrc_resolved`); metadata section backfills `isrc` from mb_data when tags lack it
- Discogs inverted retry: `discogs_service.py::get_single_status` — standard fail / sim < 0.50
  → retry inverted credit, marks `inverted_match_used`, logs `[DISCOGS_MATCH]`
- Finalise: `finalise_stage.py::_sync_isrc_popularity` — PG-safe UPDATE ... FROM subquery
  (MAX over `is_single` needs `MAX(CASE WHEN is_single THEN 1 ELSE 0 END)` — PG has no max(boolean))

## CRITICAL SCHEMA GOTCHA
- `popularity_score` is a SCAN-SIDE field only — tracks table columns are `final_score` + `popularity`
  (written in lockstep; album-relative remap persists both via `_persist_album_relative_scores`)
- Any SQL referencing popularity_score fails with "column does not exist"

## RapidFuzz placement (requirements.txt already has it)
- In use: `discogs_service.py` (token_set_ratio+partial_ratio via `_discogs_title_similarity`;
  `_scan_releases` now uses it too), `matching/track_matching.py` (dead module), old_system files
- NEW: `popularity_sources.py` `_token_similarity` (token_set_ratio, difflib fallback) —
  search aggregation fallback (>=0.90 to merge split variants), LB recording match (>=0.85),
  `_resolve_release_mbid` (>=0.80)
- `single_detection_service._detect_musicbrainz` rg-title fallback: token_set >= 0.85
- `db/repositories/tracks.py::find_library_track` PASS 3: artist-exact + token_set >= 0.92
  (only adds matches the exact passes miss; album-exact tie-break)
- DO NOT use token_set for `musicbrainz_service` MBID suggestion/album batch: it destroys
  version-tag separation ("Song (Live)" ⊂ "Song" → 100), regressing the live/acoustic fix
  (bracket-preserving difflib there is deliberate)

## Quart rules reminder
- Every route reading JSON: `async def` + `(await request.get_json(silent=True)) or {}`
- PG-only: no SQLite branches; psycopg2 can't adapt lists — inline params only