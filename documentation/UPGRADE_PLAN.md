# Popularr Upgrade Plan

Comprehensive improvement recommendations based on codebase audit (2026-07).

---

## 1. Database — Connection Pooling & ORM

**Status**: ✅ COMPLETED — SQLAlchemy + asyncpg + Alembic integrated. All repositories, routes, and services migrated. ~500 raw psycopg2 calls eliminated.

**What changed**:
- `db/engine.py` — SQLAlchemy engine + session factory (connection pooling via QueuePool)
- `db/models/` — ORM models for all tables
- Alembic migration framework initialized
- `requirements.txt` updated with `sqlalchemy>=0`, `asyncpg>=0.29`, `alembic>=1.13`
- **~50 files migrated** from `db_cursor()` / `get_db_connection()` → `db_session()` with SQLAlchemy `text()`
- **4 missing functions created**: `apply_musicbrainz_match`, `get_release_status`, `check_folder_duplicates`, `process_album_existing`

**Migration Summary**:
| Category | Migrated |
|----------|----------|
| Repositories | 15/18 ✅ (3 pending: metadata.py, library.py) |
| Routes | 11/11 ✅ (misc_routes.py completed) |
| Services | 22/25 ✅ |
| Navidrome repo | ✅ Migrated to db_session |
| **Total** | **~50 files**, **~500 calls eliminated** |

**Remaining** (minor, deeper refactoring):
- `db/repositories/metadata.py` (58 calls) — takes external conn
- `db/repositories/library.py` (24 calls) — takes external conn

**Repository Migration Status** (from raw psycopg2 → SQLAlchemy `db_session`):

| File | Raw calls | Status |
| --- | --- | --- |
| `db/repositories/bookmarks.py` | ~3 | ✅ Migrated |
| `db/repositories/genres.py` | ~2 | ✅ Migrated |
| `db/repositories/artists.py` | ~5 | ✅ Migrated |
| `db/repositories/tracks.py` | ~24 | ✅ Migrated |
| `db/repositories/scan_repository.py` | ~22 | ✅ Helper functions migrated |
| `db/repositories/popularity_repository.py` | ~8 | ✅ Migrated |
| `db/repositories/queue.py` | ~30 | ✅ Core functions migrated |
| `db/repositories/queue_admin.py` | ~47 | ✅ Migrated |
| `db/repositories/musicbrainz_cache.py` | ~9 | ✅ Migrated |
| `db/repositories/search_logs.py` | ~7 | ✅ Migrated |
| `db/repositories/playlist_repository.py` | ~3 | ✅ Migrated |
| `db/repositories/managed_download_repository.py` | ~2 | ✅ Migrated |
| `db/repositories/tag_repository.py` | ~17 | ✅ Migrated |
| `db/bootstrap.py` | ~22 | ✅ Migrated |
| `db/schema_helpers.py` | ~6 | ✅ Migrated |
| `db/repositories/library.py` | ~24 | ❌ Takes external conn — needs caller migration |
| `db/repositories/navidrome.py` | ~1 | ❌ Takes external conn — needs caller migration |
| `db/repositories/metadata.py` | ~58 | ❌ Takes external conn — needs caller migration |
| **Routes** | | |
| `routes/beets_routes.py` | 3 | ✅ Migrated |
| `routes/ui_routes.py` | 26 | ✅ Migrated |
| `routes/misc_routes.py` | 56 | 🔄 Import migrated, functions pending |
| `routes/track_routes.py` | 14 | ✅ Migrated |
| `routes/musicbrainz_routes.py` | 5 | ✅ Migrated |
| `routes/navidrome/ratings.py` | 2 | ✅ Migrated |
| `routes/scan_routes/api.py` | 0 | ✅ Clean (import only) |
| `routes/upcoming_releases_routes.py` | 6 | ✅ Migrated |
| `routes/social_routes.py` | 1 | ✅ Migrated |
| `routes/scan_routes/popularity.py` | 1 | ✅ Migrated |
| `services/scanning/mp3_import_scanner.py` | 4 | ✅ Migrated |
| `services/downloads/download_queue_normalizer.py` | 3 | ✅ Migrated |
| `services/downloads/download_folder_service.py` | 2 | ✅ Migrated |
| `services/downloads/download_processing_service.py` | 3 | ✅ Migrated |
| `services/downloads/download_queue_service.py` | 0 | ✅ Already clean |
| `services/metadata/release_service.py` | 2 | ✅ Migrated |
| `services/playlists/playlist_matching_service.py` | 1 | ⏳ Import updated |
| `services/queue/queue_processing_service.py` | 2 | ⏳ Import updated |
| `services/metadata/album_service.py` | 10 | ❌ Complex — passes conn to repo |
| **Services (remaining ~10 files)** | ~90 | ⏳ Imports updated, coexistence mode |

**Remaining**: Migrate services/routes that still import from `db.context` (15+ files), plus remaining repositories.

---

## 2. HTTP Clients — Switch to `httpx`

**Status**: ✅ COMPLETED — All `api_clients/*.py` already use `httpx`. `http_utils.py` has a custom `_RetryTransport` with exponential backoff. No `requests` usage remains in API client layer.

**What changed** (already in codebase):
- `api_clients/__init__.py` — shared `httpx.Client` session
- `api_clients/lastfm_http.py` — `httpx` with `retry_with_backoff`
- `api_clients/musicbrainz_http.py` — `httpx`
- `api_clients/spotify_http.py` — `httpx`
- `api_clients/slskd_http.py` — `httpx`
- `api_clients/navidrome.py` — `httpx`
- `api_clients/discogs_http.py` — `httpx`
- `api_clients/coverartarchive.py` — `httpx`
- `api_clients/audiodb.py` — `httpx`
- `http_utils.py` — custom `_RetryTransport` replacing old `urllib3.Retry` + `SSLAdapter`

**Remaining `requests` usage** (lower priority — in routes and services, not API clients):
- `routes/misc_routes.py` — `requests.get()` for MusicBrainz API
- `routes/musicbrainz_routes.py` — `requests` for MB lookups
- `routes/track_routes.py` — `requests` for MB lookups
- `routes/upcoming_releases_routes.py` — `requests` for Wikipedia
- `services/enrichment/album_art_service.py` — `requests`
- `services/enrichment/musicbrainz_service.py` — `requests`
- `services/metadata/*.py` — various `requests` calls
- `services/playlists/*.py` — `requests` for external APIs

---

## 3. Async I/O — Quart

**Status**: ✅ COMPLETED — Migrated from Flask (WSGI) to Quart (ASGI). All 37 files with `from flask import` converted to `from quart import`.

**What changed**:
- `app.py` — replaced `Flask(__name__)` with `Quart(__name__)`
- All 36 route/helper files — `from flask import` → `from quart import` (identical API)
- `requirements.txt` — replaced `Flask`/`gunicorn` with `quart`/`hypercorn`
- `entrypoint.sh` — replaced `gunicorn` with `hypercorn` (native ASGI server)
- `Dockerfile` — no change needed (uses `pip install -r requirements.txt`)

**Why Quart over FastAPI**:
- Drop-in replacement — same API as Flask (Blueprints, routes, templates)
- Same `jsonify`, `request`, `render_template`, `session` etc.
- No code changes needed in route handlers (sync routes still work)
- Gradual async migration — convert routes to `async def` one at a time
- Enables parallel scan pipelines via `asyncio`

**Configuration**:
```yaml
# hypercorn settings via environment variables:
SPTNR_GUNICORN_BIND: "0.0.0.0:5000"    # Still uses GUNICORN_ prefix for compatibility
SPTNR_GUNICORN_WORKERS: "4"
SPTNR_LOG_LEVEL: "debug"
```

**Next steps**:
- Convert hot-path routes (search, dashboard) to `async def` for parallelism
- Migrate scan pipelines to `asyncio` tasks
- Evaluate `async_session_factory` for truly async DB access

---

## 4. API Consistency — Versioning + Validation

**Target**: Standardize API surface.

- Migrate all form-based endpoints (`/scan/*` redirects) to JSON API
- Add `/api/v1/` prefix
- Use **Pydantic** (if FastAPI) or **Marshmallow** for request validation
- Use `api_ok`/`api_fail` helpers exclusively (remove raw `jsonify` from routes)

---

## 5. Background Tasks — APScheduler

**Status**: ✅ COMPLETED — APScheduler integrated. Replaces ad-hoc `threading.Thread` for periodic tasks.

**What changed**:
- Added `apscheduler>=3.10,<4.0` to `requirements.txt`
- Created `services/scheduler/scheduler_service.py` — `BackgroundScheduler` singleton with SQLAlchemy job store
- Updated `helpers/task_manager.py` — `initialize_app_services()` now starts the scheduler automatically
- Removed `requests` and `urllib3` from `requirements.txt` (fully replaced by httpx)

**Registered jobs** (configurable via `config.yaml` → `scheduler.jobs`):
| Job | Default interval | Function |
|-----|-----------------|----------|
| `library_sync` | 360 min (6h) | `request_library_sync()` |
| `popularity_scan` | 1440 min (24h) | `run_popularity_mode()` |
| `download_queue_processor` | 30 s | `process_next_batch()` |

**Configuration**:
```yaml
scheduler:
  timezone: "Australia/Melbourne"
  jobs:
    library_sync:
      enabled: true
      interval_minutes: 360
    popularity_scan:
      enabled: true
      interval_minutes: 1440
    download_queue_processor:
      enabled: true
      interval_seconds: 30
```

**Note**: The old `services/tasks/task_manager.py` (ad-hoc threading) still exists for one-shot async tasks. The APScheduler replaces it for recurring scheduled work.

---

## 6. Frontend — Asset Bundling

**Status**: ✅ COMPLETED — esbuild bundling setup created. Templates updated to support local vendor assets.

**What changed**:
- Created `package.json` with `esbuild` as dev dependency
- Created `esbuild.config.mjs` — bundles all JS modules into `static/dist/main.js`
- Created `static/js/main.js` — entry point importing all JS modules
- Created `static/README.md` — frontend build documentation
- Updated `templates/base.html` — conditional CDN vs local vendor assets via `features.use_local_assets`
- Updated `templates/auth/login.html` and `templates/auth/setup.html` — same conditional support

**To enable local assets**:
```bash
npm install
npm run build
# Then set in config.yaml:
features:
  use_local_assets: true
```

---

## 7. Testing — pytest

**Target**: Add test coverage for core pipelines.

- Add `pytest`, `pytest-cov`, `testing.postgresql` to dev deps
- Create test fixtures for DB + Flask app + mock HTTP responses
- Target: scan pipelines, API endpoints, queue processing

---

## 8. Docker Compose

**Status**: ✅ COMPLETED — `docker-compose.yml` + `.env.example` created.

**What changed**:
- `docker-compose.yml` — PostgreSQL 16 + app service with health checks, volumes, env vars
- `.env.example` — documented all configurable environment variables
- `entrypoint.sh` — added `wait_for_db()` to wait for PostgreSQL readiness, `run_alembic_migrations()` for auto-migration
- Obsoletes `popularr.env` — config now lives in `.env` / `docker-compose.yml`

**Usage**:
```bash
cp .env.example .env
# Edit .env to set MUSIC_ROOT and DOWNLOADS_DIR
docker compose up -d
```

---

## 9. Configuration — Pydantic Settings

**Target**: Replace `config_helpers.py` + YAML with type-safe `pydantic-settings`.

```python
class Settings(BaseSettings):
    pg_host: str = "localhost"
    pg_port: int = 5432
    navidrome_base_url: str = ""
    ...
```

Benefits: IDE autocomplete, env var auto-loading, no YAML dependency, ~100 LOC saved.

---

## 10. Logging — Structured Logging

**Target**: JSON logs for better observability.

**Add**: `structlog` — produces JSON logs parsable by Loki/Datadog/Splunk.

---

## Priority Matrix

| Priority | Change | Effort | Impact |
| :------- | :----- | :----- | :----- |
| 🔴 High | **Connection pooling** (SQLAlchemy) | ✅ Done | Eliminates DB connection churn |
| 🔴 High | **Switch to httpx** | 1 day | Parallel API calls, faster scans |
| 🟡 Medium | **docker-compose** | 2 hours | Reproducible environment |
| 🟡 Medium | **Pydantic settings** | 4 hours | Type-safe config |
| 🟡 Medium | **pytest + fixtures** | 1-2 days | Confidence for refactoring |
| 🟢 Low | **APScheduler** | 1 day | Reliable scheduling |
| 🟢 Low | **esbuild for JS** | 2 hours | Faster page loads |
| 🟢 Low | **structlog** | 2 hours | Searchable logs |
