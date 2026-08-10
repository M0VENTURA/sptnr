# Popularr adjustments (deluxe-baseline / adaptive MAD / MB batch)

## #1 Core-album baseline filtering — where it lives

- ALREADY: `finalise_stage.post_album_star_ratings` (album_scores + LF/LB distributions drop
  `exclude_from_stats`; DB merges drop `is_bonus_track_title`), `_load_artist_db_scores`,
  `build_artist_scores`, runner `_album_reference_scores` (score re-map reference).
- NEWLY ADDED: `popularity_stats_service.calculate_album_stats` /
  `calculate_album_listener_stats` (SELECT title, filter `is_bonus_track_title`, fallback to
  full set when <3 core remain — live album scored against itself) + `_filter_bonus_rows` helper.
- NEWLY ADDED: `track_stage` `_album_lf_listeners` prefetch build drops `exclude_from_stats`/
  `is_live`/`is_live_or_alternate_track_title` titles, full-set fallback at <3.
- Deliberately NOT filtered: `calculate_artist_stats` (artist z gate stability).

## #2 Adaptive MAD floor

- popularity_math.calculate_robust_zscore: spread = max(mad*1.4826, min_spread,
  ADAPTIVE_MIN_SPREAD_FRACTION (0.10) * ref_median). At median ~50 -> 5.0 < 8.0, no change.
- Single detection inline spreads: same 0.10 * med term added.
- `popularity_zscore.log_listener_z`: sigma floored at `LOG_LISTENER_Z_MIN_SIGMA` (0.05) plus
  `LOG_LISTENER_Z_RELATIVE_SIGMA` (0.02 x |mu|) — suppresses noise amplification on uniform albums.

## #3 MusicBrainz album batch

- `MusicBrainzService.lookup_album_metadata(entries)` — Lucene OR groups, chunk `_MB_BATCH_CHUNK`
  = 20, one `search_recordings` per chunk (limit len*5 cap 100), difflib match per entry
  (same norm as `get_suggested_mbid`), writes `_mbid_cache`, returns keyed metadata.
- `_recording_to_metadata()` shared by per-track lookup + batch (identical output shape).
- `get_shared_mb_client()` singleton in musicbrainz_service.py — all track_stage MB construct
  sites now reuse it (LB fallback, detect_single mb_client, metadata lookup, genre search).
- Runner: album-level batch before track loop, gated `not _singles_pass and not popularity_only`;
  entries skip tracks with existing recording_mbid; results in `options["mb_batch_metadata"]`
  keyed `artist.lower()::title.lower()`. track_stage consults batch first at both consumption
  sites (metadata section + LB fallback).
- `MusicBrainzHttpClient` import REMOVED from track_stage (unused now).

## Refactor status — ALL 4 ITEMS COMPLETE (2026-08-10)

1. Dedupe z helpers: DONE — finalise_stage local `_listener_z`/`_composite_listener_z`
   removed; imports shared `composite_listener_z` from popularity_zscore. Call site keeps
   None-guard (distributions None -> _verify_z stays 0.0, flag honoured, NO shared DB fallback).
2. Config loader unify: DONE — popularity_config uses `helpers.config_helpers.get_config()`
   (cached; env overrides applied). Removed own load_config/CONFIG_PATH/DEFAULT_FEATURES/
   SPOTIFY_WEIGHT/STANDOUT_CONFIG/apply_standout_config_overrides. `library_sync_service`
   `get_navidrome_config` now lazy-imports get_config. Import-time constants
   LASTFM_WEIGHT/etc. remain evaluated once per process (note: get_config is cached, so
   "no restart" claim only holds for runtime lookups like resolve_weights).
3. MB client reuse: DONE (earlier) — `get_shared_mb_client()` singleton; track_stage has
   ZERO `MusicBrainzHttpClient()` constructs. Other services still construct their own
   (album_stage, popularity_sources, metadata services — out of scope).
4. Purge: DONE — standout_service.py now only holds `STANDOUT_CONFIG =
   get_standout_config()` (LIVE, finalise imports it); `detect_via_iterative_zscore` +
   `get_top_standout_tracks_with_gap` deleted (zero callers; only old_system/docs).
   popularity_config STANDOUT_CONFIG dead dict deleted — get_standout_config() fully
   supersedes it (has all star_* keys + star_epsilon_score_points + listener_5star_z_threshold).