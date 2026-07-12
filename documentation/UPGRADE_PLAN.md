# Popularr Upgrade Plan

Comprehensive improvement recommendations based on codebase audit (2026-07).

---

## 1. Database — Connection Pooling & ORM

**Status**: ✅ COMPLETED — SQLAlchemy + asyncpg + Alembic integrated.

**What changed**:
- `db/engine.py` — SQLAlchemy engine + session factory
- `db/models/` — ORM models for all tables
- Alembic migration framework initialized
- `requirements.txt` updated with `sqlalchemy>=2.0`, `asyncpg>=0.29`, `alembic>=1.13`

**Remaining**: Migrate individual repositories (`db/repositories/*.py`) from raw psycopg2 to SQLAlchemy sessions.

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
