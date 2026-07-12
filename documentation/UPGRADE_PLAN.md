# Popularr Upgrade Plan

Comprehensive improvement recommendations based on codebase audit (2026-07).

---

## 1. Database — Connection Pooling & ORM

**Status**: 🔄 PARTIALLY COMPLETED — SQLAlchemy + asyncpg + Alembic integrated. Repository migration in progress.

**What changed**:
- `db/engine.py` — SQLAlchemy engine + session factory
- `db/models/` — ORM models for all tables
- Alembic migration framework initialized
- `requirements.txt` updated with `sqlalchemy>=2.0`, `asyncpg>=0.29`, `alembic>=1.13`

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

**Target**: Replace `requests` with `httpx` across all API clients.

**Why**:
- Built-in connection pooling (removes `urllib3.Retry` + `SSLAdapter` ~60 LOC)
- Native async support via `httpx.AsyncClient` → parallel API calls for scan pipelines
- HTTP/2 support
- Better typing

**Files affected**: `api_clients/*.py`, `http_utils.py` (can be deleted)

**Effort**: ~1 day

---

## 3. Async I/O — Quart or FastAPI

**Target**: Move from Flask (WSGI) to an async framework.

**Options**:

| Option | Effort | Benefit |
|---|---|---|
| **Quart** | Medium | Drop-in Flask replacement, same API |
| **FastAPI** | High | Auto OpenAPI docs, Pydantic, async native |

**Recommendation**: Start with Quart — `from quart import Quart` instead of `from flask import Flask`. Blueprints, routes, templates all work identically. Then enable async scan pipelines.

---

## 4. API Consistency — Versioning + Validation

**Target**: Standardize API surface.

- Migrate all form-based endpoints (`/scan/*` redirects) to JSON API
- Add `/api/v1/` prefix
- Use **Pydantic** (if FastAPI) or **Marshmallow** for request validation
- Use `api_ok`/`api_fail` helpers exclusively (remove raw `jsonify` from routes)

---

## 5. Background Tasks — APScheduler

**Target**: Replace ad-hoc `threading.Thread` for periodic scans.

**Why**: Threads are invisible to other gunicorn workers, not persisted, no retry logic.

**Add**: `APScheduler` for scheduled tasks (library sync, popularity scan).

**Optionally**: `Huey` for the download queue (lightweight Redis-backed task queue).

---

## 6. Frontend — Asset Bundling

**Target**: Reduce page load time and eliminate CDN dependency.

- Use **esbuild** or **Vite** to bundle `static/js/*.js` → single minified file
- Move inline `<script>` blocks from templates to dedicated JS files
- Consider **HTMX** for dashboard polling (replaces manual `fetch()` + DOM updates)
- Vendor Bootstrap CSS instead of loading from CDN (works offline)

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
