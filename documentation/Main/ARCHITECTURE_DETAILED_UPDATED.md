
# POPULARR ARCHITECTURE — DETAILED CURRENT STATE

> This is the detailed architecture reference for Popularr based on the project files shared so far.  
> It restores the deeper detail from `popularr_architecture_v9.md` and updates it with the newer information gathered today.

---

## 1. Status Legend

- ✅ **STABLE** — Current, clean implementation; safe to build on.
- ⚠️ **MIGRATING** — New structure exists, but legacy code or duplicate paths remain.
- ❌ **LEGACY** — Old implementation; remove or replace once safe.
- 🧩 **COMPATIBILITY** — Shim/re-export layer to keep old imports working.
- ❓ **UNKNOWN / NOT INSPECTED** — File/folder exists but contents have not been reviewed.
- 🗑️ **REMOVE / IGNORE** — Generated, accidental, duplicate, or no longer part of the supported architecture.

---

## 2. Core System Shape

Popularr is currently a Flask WebUI application with scan routes, scanning pipelines, service layers, API clients, and PostgreSQL repositories.

The important architectural principle is:

```text
Routes handle HTTP
Pipelines orchestrate workflows
Services perform business/integration logic
API clients perform external HTTP only
Repositories perform DB reads/writes
Helpers are mostly compatibility/bootstrapping
```

### Current real execution path

The application is currently still hybrid. The new staged popularity architecture exists, but the **live popularity execution path still routes into the root legacy scanner**:

```text
WebUI / route
  → routes/scan_routes/popularity.py or control.py
    → services/scanning/pipelines/popularity_pipeline.py
      → root popularity.py
```

### Target execution path

The intended target path is:

```text
WebUI / route
  → routes/scan_routes/popularity.py or control.py
    → services/scanning/pipelines/popularity_pipeline.py
      → services.popularity.pipeline.run_popularity_scan()
        → services.popularity.scan_stage_runner.run_scan()
          → services.popularity.stages.load_stage
          → services.popularity.stages.album_stage
          → services.popularity.stages.track_stage
          → services.popularity.stages.finalise_stage
```

---

## 3. Application Startup

### `entrypoint.sh`
**Status:** ✅ STABLE  
**Layer:** Container startup

Starts the application environment and launches the Flask/Gunicorn runtime. It should remain thin and should not contain application business logic.

### `app.py`
**Status:** ✅ STABLE  
**Layer:** Flask entrypoint

`app.py` is intentionally minimal and only coordinates application setup.

Current startup flow:

```text
setup_logging("WebUI")
  → create Flask app
  → configure secret key and session lifetime
  → ensure_default_log_files(CONFIG_PATH, LOG_PATH)
  → register_flash_helpers(app)
  → register_filters(app)
  → register_all_blueprints(app)
  → register_app_hooks(app)
  → init_database_and_schema()
  → initialize_app_services(app)
```

Important imports:

```python
from helpers.logging_config import setup_logging
from helpers.check_db import init_database_and_schema
from helpers.flash_manager import register_flash_helpers
from helpers.app_bootstrap import register_all_blueprints
from helpers.app_hooks import register_app_hooks
from helpers.template_filters import register_filters
from helpers.task_manager import initialize_app_services
from helpers.file_manager import ensure_default_log_files
```

### Important observation

`app.py` is architecturally clean, but it confirms that `helpers/` is not purely a facade layer. Some helper modules are still active application bootstrap/support modules:

- `helpers.app_bootstrap`
- `helpers.app_hooks`
- `helpers.flash_manager`
- `helpers.template_filters`
- `helpers.file_manager`
- `helpers.logging_config`
- `helpers.task_manager`

These are not necessarily wrong, but they should be treated as `app support` or `infrastructure bootstrap`, not generic helpers.

---

## 4. Routes Layer

Routes are responsible for HTTP entrypoints only. They should not do heavy scan logic, provider lookup, scoring, or database orchestration.

### `routes/blueprint_bootstrap.py`
**Status:** ⚠️ MIGRATING / STALE

Central blueprint registration. It is meant to register one blueprint per API/UI domain.

Current problem:

- It still references a large number of route files that were lost and need to be rebuilt or removed from registration.
- This is a runtime risk because import failures will stop app startup if missing modules are still referenced.

Recommended direction:

```text
Only register existing/rebuilt blueprints
Do not recreate every old route unless the UI actually needs it
```

### `routes/scans.py`
**Status:** 🧩 COMPATIBILITY

Compatibility shim that keeps older imports working by forwarding to `routes.scan_routes`.

```python
from routes.scan_routes import scans_bp
```

### `routes/library_routes.py`
**Status:** ✅ STABLE

Exposes library sync endpoints:

```text
POST /api/library/sync
GET  /api/library/status
```

Expected flow:

```text
route
  → request_library_sync()
  → worker thread
  → perform_library_sync()
```

There is still a canonical ownership question between:

```text
services/library/library_sync_service.py
services/scanning/library_sync.py
```

The later integrated scan hooks suggest `services/scanning/library_sync.py` is currently more closely tied to post-Navidrome scan behaviour.

### `routes/popularity_routes.py`
**Status:** ❌ LEGACY

Old popularity route file. It should not be restored as a separate active route system unless absolutely necessary.

Former role:

```text
/api/popularity/status
/api/popularity/run
```

Problem:

- Competes with `routes/scan_routes`.
- Historically tied to old progress tracking.
- Creates duplicate popularity execution paths.

Recommended replacement:

```text
/api/popularity/run
  → compatibility endpoint in routes/scan_routes/api.py
  → services/scanning/pipelines/popularity_pipeline.py
```

---

## 5. Scan Routes Package

### `routes/scan_routes/__init__.py`
**Status:** ✅ STABLE

Defines the shared `scans_bp` blueprint and imports sub-route modules so route decorators attach.

### `routes/scan_routes/_common.py`
**Status:** ✅ STABLE

Shared utilities for scan routes:

- `form_bool()`
- `run_async()`
- `is_process_alive()`
- artist/album redirect helpers

This keeps route modules thin and consistent.

### `routes/scan_routes/control.py`
**Status:** ✅ STABLE / GOLD STANDARD

Primary scan controller. Starts and stops major scan workflows.

Typical flow:

```text
POST /scan/start
  → resolve scan_type
  → run_async(...)
  → services/scanning/pipelines/*
```

This is the pattern future routes should follow: route → pipeline, with no heavy logic in the route.

### `routes/scan_routes/api.py`
**Status:** ⚠️ MIGRATING

Dashboard/API support routes. Expected to support scan status, progress polling, recent scans, and compatibility endpoints.

Likely consumers:

```text
templates/dashboard.html
frontend JavaScript
```

### `routes/scan_routes/popularity.py`
**Status:** ✅ ROUTE STABLE / ⚠️ BACKEND LEGACY-COUPLED

Starts popularity-related modes from the WebUI:

```text
POST /scan/popularity
POST /scan/singles
```

Flow:

```text
route
  → run_popularity_mode()
  → services/scanning/pipelines/popularity_pipeline.py
```

The route itself is acceptable. The issue is the pipeline it calls still goes to root `popularity.py`.

### `routes/scan_routes/artist_album.py`
**Status:** ✅ STABLE

Targeted artist/album/track scan entrypoints.

Main role:

```text
artist scan
album scan
track rescan trigger
```

Future improvement:

```text
Track rescan should eventually use a true track-level pipeline instead of triggering a broader artist flow.
```

### `routes/scan_routes/essentia.py`
**Status:** ✅ STABLE

Starts and stops Essentia/audio-feature scans.

### `routes/scan_routes/mp3_import.py`
**Status:** ✅ STABLE

Starts and stops MP3 metadata import workflows.

---

## 6. Navidrome Routes

### `routes/navidrome/__init__.py`
**Status:** ✅ STABLE

Defines `navidrome_bp` and provides `get_navidrome_client()`.

Client selection:

```text
session username + navidrome_users config
  → user-specific NavidromeClient
else legacy navidrome config
  → fallback NavidromeClient
```

### `routes/navidrome/playlists.py`
**Status:** ✅ STABLE

Endpoints:

```text
GET /api/navidrome/playlists
GET /api/navidrome/playlist/<playlist_id>
```

Flow:

```text
route
  → get_navidrome_client()
  → NavidromeClient.fetch_all_playlists() / fetch_playlist()
```

### `routes/navidrome/ratings.py`
**Status:** ⚠️ MIGRATING

Endpoint:

```text
POST /api/navidrome/ratings/sync-now
```

Current flow:

```text
route
  → session auth check
  → get_navidrome_client()
  → DB query tracks with stars
  → NavidromeClient.set_rating()
```

Problem:

```text
route → DB → API client
```

Target:

```text
route → services.navidrome.rating_sync_service → DB/API client
```

### `routes/navidrome/scan.py`
**Status:** ⚠️ MIGRATING

Handles both remote Navidrome scan APIs and local import pipeline routes.

Endpoints:

```text
POST /api/navidrome/scan/start
GET  /api/navidrome/scan/status
POST /scan/navidrome
POST /scan/stop-navidrome
```

Problem:

- Still uses `services.scanning.progress` directly.
- Should move toward `services.scanning.scan_state` as the canonical progress/checkpoint system.

---

## 7. Scanning Layer

### `services/scanning/pipeline.py`
**Status:** ⚠️ CORE ORCHESTRATOR / LEGACY-COUPLED

Coordinates high-level scan workflows:

- artist scan orchestration
- Navidrome import coordination
- metadata/popularity calls
- Essentia calls
- post-Navidrome hooks
- boot-time Navidrome import

Current problem:

```python
from popularity import popularity_scan
from popularity_helpers import build_artist_index
```

This means root legacy popularity code is still part of real runtime execution.

### `services/scanning/pipelines/popularity_pipeline.py`
**Status:** ❌ ACTIVE LEGACY BRIDGE

This file is currently the active popularity bridge from routes into the root legacy scanner.

Current flow:

```text
run_popularity_mode()
  → from popularity import popularity_scan
  → popularity_scan(...)
```

This bypasses the rebuilt `services.popularity.pipeline.py`.

Target change:

```python
from services.popularity.pipeline import run_popularity_scan
```

Then replace `popularity_scan(...)` calls with `run_popularity_scan(...)`.

### `services/scanning/scan_state.py`
**Status:** ⚠️ SHOULD BECOME CANONICAL

Currently handles:

- Navidrome progress path
- Navidrome checkpoint path
- checkpoint load/save/clear
- progress file writes
- first full import marker

Still missing behaviours from `progress.py`:

- stop request helper
- progress reader
- generic path helpers
- timestamps / update metadata

### `services/scanning/progress.py`
**Status:** ❌ LEGACY BUT STILL USED

Old progress/stop helper. Some route/pipeline code still imports it.

Target:

```text
scan_state.py absorbs progress.py
progress.py becomes shim or is removed
```

### `services/scanning/runtime_state.py`
**Status:** ⚠️ MIGRATING

Tracks in-process scan references. Important limitation:

```text
Flask workers do not share Python memory
```

Therefore, runtime state is only per-process. Progress/checkpoint files are the cross-process source of truth.

### `services/scanning/library_sync.py`
**Status:** ⚠️ MIGRATING / GOOD DESIGN

Incremental Navidrome library sync with single-flight execution and UI-visible state.

Flow:

```text
request_library_sync()
  → worker thread
  → perform_library_sync()
    → NavidromeClient.get_scan_status()
    → candidate artists
    → sync_artist_with_diff()
    → bulk DB commit
```

### Other scanning modules

- `album_scanner.py` → album-level scanning.
- `artist_scanner.py` → artist-level scanning.
- `bootstrap.py` → scan setup/bootstrap.
- `cleanup.py` → scanning cleanup helpers.
- `filters.py` → scanning skip/filter rules.
- `metadata_extractor.py` → local metadata extraction.
- `navidrome_import.py` → core Navidrome import logic.
- `navidrome_scan_service.py` → Navidrome scan/config wrapper; may overlap with other Navidrome helpers.
- `navidrome_service.py` → scan-oriented Navidrome service helpers.
- `payload_builder.py` → builds DB-ready payloads from Navidrome/local metadata.
- `scanner.py` → scanner wrapper/legacy orchestrator.
- `scan_resume_service.py` → scan resume helper.
- `track_scanner.py` → track-level scanning.

---

## 8. Popularity Layer

### `popularity.py` root file
**Status:** ❌ CURRENT PRIMARY ENGINE

This is still the live scanner because `services/scanning/pipelines/popularity_pipeline.py` imports it.

Expected responsibilities based on current coupling:

- candidate loop
- provider lookups
- scoring
- single detection
- DB writes
- progress checks

This file is the highest-value remaining code to inspect for scoring/debugging.

### `popularity_helpers.py` root file
**Status:** ❌ LEGACY SUPPORT

Still referenced by scanning code for functions like artist indexing.

### `services/popularity/pipeline.py`
**Status:** ✅ REBUILT / READY BUT NOT YET WIRED

This was rebuilt as the canonical staged-runner-first entrypoint.

It now prefers:

```text
services.popularity.scan_stage_runner.run_scan()
```

and falls back to:

```text
services.popularity.legacy_scanner.popularity_scan()
```

### `services/popularity/scan_stage_runner.py`
**Status:** ⚠️ TARGET ENGINE / PARTIALLY IMPLEMENTED

Intended staged execution:

```text
load_candidates
  → enrich_album
  → process_track
  → finalise_scan
```

### Stage files

- `load_stage.py` → currently placeholder; should load scan candidates.
- `album_stage.py` → currently placeholder; should handle album-level enrichment/stat prep.
- `track_stage.py` → currently placeholder; should handle provider lookup/scoring/persistence generation.
- `finalise_stage.py` → currently placeholder; should handle final commits/post-scan hooks.

### Supporting popularity services

- `scan_hooks.py` → prepares normalized album/track context.
- `popularity_sources.py` → Last.fm/ListenBrainz provider data acquisition.
- `popularity_math.py` → pure scoring math.
- `popularity_matching.py` → artist/title matching helpers.
- `popularity_stats_service.py` → artist/album stat helpers.
- `standout_service.py` → standout/star helpers.
- `popularity_adjustments.py` → DB-backed score adjustments.
- `popularity_cache_policy.py` → cache/freeze rules.
- `popularity_config.py` → popularity weights/config.
- `progress_tracker.py` → in-memory popularity progress; should not be route-level canonical state.

### Duplicate/legacy files

- `pipeline (2).py` → duplicate; logic merged into canonical `pipeline.py`; remove/archive.
- `scan_hooks (2).py` → duplicate; remove after confirming canonical file.
- `scan_stage_runner (2).py` → duplicate; remove after confirming canonical file.
- `legacy_scanner.py` → fallback only.
- `scanner.py` → old orchestrator.
- `scoring.py` → old scoring wrapper.
- `single_detection.ps1` → legacy/accidental PowerShell file in Python service folder.

---

## 9. Database Layer

### DB architecture rule

```text
Only repository modules should own DB read/write logic.
```

### `db/bootstrap.py`
**Status:** ✅ STABLE

Owns schema bootstrap and startup verification.

### `db/schema.py`
**Status:** ✅ STABLE

Owns table/column/index definitions.

### `db/utils.py`
**Status:** ✅ STABLE

Owns DB connection, retries, row access, and PostgreSQL utility helpers.

### `db/context.py`
**Status:** ✅ STABLE

Context manager for safe cursor handling.

### `db/cleanup.py`
**Status:** ✅ STABLE

Cleanup routines used by scan/import operations.

### `db/database.py`
**Status:** 🧩 COMPATIBILITY

Old import facade. Prefer direct imports from `db.*` or `db.repositories.*`.

### Repositories

- `artists.py` → artist insert/lookups and collection checks.
- `bookmarks.py` → favorite/bookmark checks.
- `genres.py` → genre aggregation, capitalization, audit logging.
- `library.py` → library stats and artist listing queries.
- `navidrome.py` → Navidrome track upsert helper.
- `popularity_repository.py` → popularity/scoring persistence with type-aware upsert.
- `scan_repository.py` → scan DB helpers, validation, cleanup.
- `tag_repository.py` → editable metadata tag reads/writes.
- `tracks.py` → core track insert/update/query/delete helpers.
- `tracks_scan_additions.py` → optional additions already duplicated into tracks in some areas.

---

## 10. API Clients Layer

API clients should perform external HTTP work only. Business interpretation belongs in services.

Raw/low-level clients:

- `discogs_http.py`
- `lastfm_http.py`
- `musicbrainz_http.py`
- `spotify_http.py`
- `slskd_http.py`
- `wikidata_http.py`

Provider wrappers / compatibility facades:

- `discogs.py`
- `lastfm.py`
- `musicbrainz.py`
- `spotify.py`
- `slskd.py`
- `wikidata.py`
- `audiodb_and_listenbrainz.py`
- `musicbrainz_utils.py`

Other clients:

- `navidrome.py` → active Navidrome/Subsonic client.
- `listenbrainz.py` → ListenBrainz API integration.
- `applemusic.py` → Apple artwork/search integration.
- `audiodb.py` → AudioDB integration.
- `coverartarchive.py` → Cover Art Archive integration.
- `acousticbrainz.py` → AcousticBrainz integration.

---

## 11. Services Layer

### `services/catalog/album_classification_service.py`
**Status:** ✅ STABLE

Pure classification logic for live albums, compilations, greatest hits, alternate takes, and stat exclusions.

### `services/downloads/slskd_service.py`
**Status:** ✅ STABLE

Soulseek/slskd workflow service above the raw HTTP client.

### `services/enrichment/*`
**Status:** ✅ STABLE WITH SOME DB COUPLING

Provider interpretation and enrichment services:

- album art
- artwork selection
- biography lookup
- cover detection
- Discogs interpretation
- genre aggregation
- Last.fm enrichment
- ListenBrainz enrichment
- MusicBrainz enrichment
- Spotify enrichment
- single detection

Long-term target: services should call repositories for DB writes rather than writing directly.

### `services/infrastructure/*`
**Status:** ✅ STABLE

- `api_rate_limiter.py` → API throttling.
- `timeout_executor.py` → timeout-limited execution helper.

### `services/music/*`
**Status:** 🧩 COMPATIBILITY

Re-export layer for enrichment services. Remove when imports are updated.

### `services/navidrome/rating_sync_service.py`
**Status:** ⚠️ STABLE BUT UNDERUSED

Syncs track ratings to Navidrome user(s). Should be used by `routes/navidrome/ratings.py` instead of the route doing DB + client work directly.

### `services/playlists/playlist_service.py`
**Status:** ✅ STABLE

Generates `.nsp` playlists from DB-rated tracks.

### `services/matching/track_matching.py`
**Status:** ✅ STABLE

Shared matching and normalization logic.

### `services/metadata/*`
**Status:** ✅ STABLE

- `tag_constants.py` → editable metadata field definitions.
- `tag_file_service.py` → MP3/FLAC physical tag writes.
- `title_normalization_service.py` → lookup/title cleanup.

### `services/web/api_response.py`
**Status:** ✅ STABLE

Canonical API response helper, re-exported by `helpers/api_response.py`.

---

## 12. Helpers Layer

Helpers are currently mixed:

### Compatibility/facade helpers

- `api_rate_limiter.py`
- `api_response.py`
- `check_db.py`
- `db_cleanup.py`
- `db_context.py`
- `db_queries.py`
- `db_utils.py`
- `scan_bootstrap.py`
- `scan_db_helpers.py`
- `scan_helpers.py`
- `scan_payload.py`
- `scan_tasks.py`
- `scan_utils.py`
- `tag_manager.py`
- `album_art_manager.py`

### Active app support helpers

- `task_manager.py` → background jobs / schedulers.
- `logging_config.py` → logging setup.
- `flash_manager.py` → UI flash helpers.
- `template_filters.py` → Jinja filters.
- `file_manager.py` → required file/log setup.
- `app_hooks.py` → Flask hooks.
- `app_bootstrap.py` → blueprint registration bridge, if present locally.

Important correction:

```text
helpers is not purely facade while app.py still imports active bootstrap helpers.
```

---

## 13. Templates / WebUI Layer

Templates are the UI rendering layer and likely call scan/API endpoints through forms or JavaScript.

Important templates:

- `dashboard.html` → primary dashboard and scan/status API consumer.
- `base.html` → layout shell.
- `artist.html` → artist page.
- `album.html` → album page.
- `downloads.html` and related downloads templates → download/search workflows.
- `config.html` → configuration UI.
- `bookmarks.html`, `playlists_*`, `smart_playlists.html` → feature UIs.

Recommended next documentation enhancement:

```text
template → endpoint → route → service/pipeline
```

This requires scanning each template for:

```text
fetch(
axios
form action=
/api/
/scan/
```

---

## 14. Current Critical Problem Areas

### 1. Active popularity path still uses root `popularity.py`

This is the biggest current architecture gap.

### 2. `services/scanning/pipelines/popularity_pipeline.py` must be rewired

Replace root import:

```python
from popularity import popularity_scan
```

with canonical service import:

```python
from services.popularity.pipeline import run_popularity_scan
```

### 3. Stage files are placeholders

The staged architecture exists, but candidate loading, album processing, track scoring, and finalisation still need real logic moved in.

### 4. Progress system is split

`progress.py` is still used. `scan_state.py` should become canonical.

### 5. Blueprint bootstrap references missing routes

Simplify registration or rebuild only routes actually needed by the UI.

### 6. Duplicate files remain

- `pipeline (2).py`
- `scan_hooks (2).py`
- `scan_stage_runner (2).py`

### 7. Helpers are overloaded

Some helper files are shims; others are active app infrastructure.

### 8. Navidrome ratings route bypasses service layer

Move batch sync to `services/navidrome/rating_sync_service.py`.

---

## 15. Work Remaining

### Critical

1. Rewire `services/scanning/pipelines/popularity_pipeline.py` to call `services.popularity.pipeline.run_popularity_scan()`.
2. Confirm staged runner works or safely falls back to legacy scanner.
3. Remove/archive `services/popularity/pipeline (2).py`.

### Popularity migration

1. Move candidate loading from root `popularity.py` into `load_stage.py`.
2. Move album-level logic into `album_stage.py`.
3. Move provider lookup/scoring/persistence into `track_stage.py`.
4. Move post-scan commits/playlist/rating sync into `finalise_stage.py`.

### Progress migration

1. Add stop/read helpers to `scan_state.py`.
2. Replace `progress.py` imports.
3. Turn `progress.py` into compatibility shim or remove it.

### Cleanup

1. Remove duplicate `(2)` files after canonical validation.
2. Remove `services/music` once imports point to `services.enrichment`.
3. Decide canonical library sync implementation.
4. Move real helper logic into app support/infrastructure modules.

---

## 16. Final Target Architecture

```text
WebUI template
  → route
    → pipeline/service
      → domain service
        → API client / repository / file service
          → external API / PostgreSQL / filesystem
```

Popularity target:

```text
routes/scan_routes
  → services/scanning/pipelines/popularity_pipeline.py
    → services/popularity/pipeline.py
      → services/popularity/scan_stage_runner.py
        → services/popularity/stages/*
          → services/popularity/popularity_sources.py
          → services/popularity/popularity_math.py
          → services/popularity/popularity_stats_service.py
          → services/popularity/standout_service.py
          → db/repositories/*
```

Navidrome import target:

```text
routes/navidrome or routes/scan_routes
  → services/scanning/pipelines/navidrome_pipeline.py
    → services/scanning/navidrome_import.py
      → services/scanning/payload_builder.py
      → db/repositories/*
```

Repository rule:

```text
Only repository modules should write to PostgreSQL.
```

API client rule:

```text
API clients perform HTTP only.
```


---

## 14. Library Sync Layer (NEWLY DOCUMENTED)

### services/library/library_sync_service.py

**Status:** ✅ STABLE / ADVANCED WORKER MODEL

This service is the canonical **incremental Navidrome → PostgreSQL sync engine**.

### Key behavioural flow

```text
/api/library/sync
  → routes/library_routes.py
    → request_library_sync()
      → background worker thread
        → perform_library_sync()
          → get_candidate_artists()
          → sync_artist_with_diff()
            → scan_artist_to_db()
```

### Important architectural characteristics

- Uses coalescing model (`running` + `pending`) to avoid duplicate sync runs
- Uses Navidrome scan marker (`scan_status.count`) to detect changes
- Uses **diff_mode ingestion** rather than full re-imports
- Batches track writes (`bulk_upsert_navidrome_tracks`)
- Tracks detailed progress:
  - artists_processed
  - artists_failed
  - tracks_attempted

### Critical insight

```text
library_sync_service → scan_artist_to_db → payload_builder
```

This is the **primary ingestion path feeding the popularity system**.

---

## 15. Navidrome Import Layer (CRITICAL PATH)

### services/scanning/navidrome_import.py

**Status:** ✅ CORE PIPELINE

This is the **central ingestion engine** for all Navidrome data.

### Execution flow

```text
scan_artist_to_db()
  → fetch_artist_albums()
  → fetch_album_tracks()
  → extract_track_metadata()
  → build_track_payload()
  → upsert_track_payload()
```

### Key responsibilities

- Diff-based album/track comparison
- Metadata extraction
- Payload construction
- DB writes via repository layer
- Cleanup of stale tracks

### Critical finding

```text
payload_builder injects default popularity values
```

This means **popularity data is initially reset during ingestion** and must be re-written later by scoring.

---

## 16. Payload Builder Layer

### services/scanning/payload_builder.py

**Status:** ✅ STABLE / DATA SHAPE OWNER

Responsible for building the canonical DB payload.

### Key behaviour

- Merges raw Navidrome data + extracted metadata
- Injects scoring defaults:

```python
NAVIDROME_SCORE_DEFAULTS
```

### Architectural implication

```text
Ingestion always overwrites score fields → scoring pipeline must reapply values
```

This is a **known source of scoring inconsistencies**.

---

## 17. Navidrome Service Layer

### services/scanning/navidrome_service.py

**Status:** ✅ STABLE

Provides orchestration helpers (NOT raw HTTP):

- Artist index building (album-first strategy ✅)
- Concurrent track fetching
- Library stats aggregation

### Design rule

```text
api_clients → raw HTTP
navidrome_service → orchestration logic
```

---

## 18. Navidrome Scan Service Layer

### services/scanning/navidrome_scan_service.py

**Status:** ✅ STABLE (overlap risk)

Responsibilities:

- Config loading
- Client caching
- DB → Navidrome artist mapping

### Issue

Possible overlap with:

```text
navidrome_service
navidrome_import
routes/navidrome
```

Needs consolidation decision.

---

## 19. Enrichment Layer (FULLY CLASSIFIED)

Clear separation now exists:

### API Clients (raw HTTP)
```text
api_clients/*
```

### Enrichment services (interpretation)
```text
services/enrichment/*
```

### Categories

#### Core enrichment
- musicbrainz_service.py
- discogs_service.py
- spotify_service.py
- wikidata_bio_service.py

#### Persistence bridge
- musicbrainz_persistence_service.py (DB writes ✅)

#### Single detection
- single_detection_service.py
- single_detection_content_service.py

#### Metadata & media
- album_art_service.py
- artist_bio_service.py
- artwork_lookup_service.py
- spotify_metadata_service.py

### Key rule now validated

```text
Enrichment services DO NOT write DB (except explicit persistence services)
```

---
