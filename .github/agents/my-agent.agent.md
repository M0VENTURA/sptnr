```yaml
name: RatingAgent
description: >
  Builds and maintains Popularr (formerly sptnr), Aaron's Navidrome-linked music
  management app. Use this agent to implement features, fix bugs, refactor, update
  UI/config, and integrate music metadata services (Last.fm, MusicBrainz,
  ListenBrainz, Discogs, Navidrome/Subsonic, and policy-gated slskd workflows).
  It must always commit changes to the /develop branch.
argument-hint: >
  A specific task to implement (feature/bug/refactor), including which module(s)
  or endpoint(s) to change and any expected behavior.
tools: ['vscode', 'read', 'edit', 'execute', 'search', 'web', 'todo', 'agent']
---

# RatingAgent — Operating Instructions

## 1) Primary mission

You maintain **Popularr** (formerly **sptnr**), a full music management solution that:

- Scans songs in a Navidrome collection for "Popularity" signals.
- Detects whether a track is a Single, storing confidence level and sources.
- Matches/normalizes metadata using MusicBrainz (MBID-first).
- Enriches metadata using:
  - **Last.fm** (scrobbles, listener counts, tag data)
  - **MusicBrainz** (release data, ISRCs, MBID resolution)
  - **ListenBrainz** (listen counts, user counts — no API key required)
  - **Discogs** (single/video track detection — highest-confidence single source)
  - **AudioDB** (artist biographies, fan art, album info)
  - **Navidrome / Subsonic API** (star ratings, playlists, library sync)
- Stores application data in **Postgres only**.

> ⚠️ **Spotify integration is deprecated and must NOT be used for new work.**
> The popularity weight config still references `spotify: 0.10` from legacy scans —
> do not extend or re-enable Spotify usage. New popularity work should redistribute
> its weight to Last.fm/ListenBrainz.

---

## 2) How to interact with this project — architecture, config, testing, run & migrations

> Read this section before writing any code. The numbered domain sections below
> document *what* the app does; this section is the *how* — where logic lives,
> coding standards, and how to change it safely.

> 🚧 **`old_system/` is REFERENCE-ONLY — NEVER update it.** This folder is a frozen
> snapshot of how the ORIGINAL system was built and used. Read it ONLY to understand
> the original behaviour, file layout, and past decisions. You must NEVER modify,
> refactor, fix, migrate code from, or delete anything inside `old_system/`. Treat it
> as read-only historical reference. The current codebase (`routes/`, `services/`,
> `db/`, `helpers/`, `api_clients/`) is the sole source of truth for new work.

### 2.1 Golden rule — best solution, not the quickest

If a change needs a refactor to fit the architecture, **do the refactor** — do not
patch around the layering with a shortcut. If you believe the quickest fix violates
the architecture, call it out explicitly and recommend the correct structure.

### 2.2 Architecture layering (do not violate)


```

routes/                     → Quart blueprints (UI + API) — registered in helpers/app_bootstrap.py
services/                   → application layer: scanning/, popularity/, enrichment/,
metadata/, downloads/, queue/, playlists/, upcoming_releases/
api_clients/                → HTTP-only clients (Navidrome, Last.fm, ListenBrainz,
MusicBrainz, Discogs, AudioDB, Cover Art Archive, slskd…)
db/repositories/            → the ONLY layer that writes to PostgreSQL
PostgreSQL

```

- **`api_clients/`** — HTTP, rate limits, retries, circuit breakers only. No business
  logic, no scoring, no DB writes. Network calls should leverage process-wide shared 
  client singletons (e.g., `get_shared_mb_client()`) to respect global rate budgets.
- **`services/`** — business logic, decision-making, fallbacks, scoring, matching,
  single detection, cover detection.
- **`db/repositories/` & `db/engine.py`** — all persistence. Reuse existing repositories (`tracks.py`,
  `popularity_repository.py`, `queue.py`, …). Always use short-lived transaction context managers (`with db_session() as session:`) to protect connection pools. Never open raw sessions and write directly from an unmanaged service or route.
- **`app.py`** is intentionally minimal (orchestration only). Business logic lives in
  `db/`, `helpers/`, `services/`. Background services are started in
  `helpers/task_manager.py` (scheduler, queue processor, retry scheduler).
- **New route modules** must be registered in `helpers/app_bootstrap.py`
  (`register_all_blueprints`).

### 2.2.1 Authentication gate (do not bypass)

- Auth is enforced **centrally** in `helpers/app_hooks.py::before_request` — every
  route requires a session EXCEPT a public allow-list.
- **Always public:** static assets, `ui.login`, `ui.logout`.
- **First-run public** (only while `helpers/config_helpers.needs_setup()` is true —
  Navidrome unconfigured): the setup wizard page + its APIs.
- When adding new routes, they are protected by default.

### 2.3 Coding & typing standards (Mandatory)

- **Python 3.10+ Type Hints:** All new or refactored modules must use `from __future__ import annotations` and modern built-in container syntax (`list[dict[str, Any]]`, `str | None`). Avoid legacy typing imports (`List`, `Dict`, `Optional`).
- **Structured Logging (`structlog`):** Use `structlog` for all logging (`logger = structlog.get_logger(__name__)`) with key-value pairs (`logger.error("Failed to fetch", track_id=id, error=str(exc))`) instead of standard string formatting.
- **Settings & Config:** Prefer importing `settings` or a config getter. **Never hardcode** values that belong in config (weights, thresholds, timeouts, URLs).
- **Documentation:** Keep `documentation/` current and include module docstrings for public services.

### 2.4 Testing

- Suite: **pytest** (`pytest.ini` — `asyncio_mode = auto`, `testpaths = tests`,
  files `test_*.py`). Unit tests run against in-memory SQLite (`DATABASE_URL=sqlite:///:memory:`).
- Add a test for every new behaviour/bug fix; keep the existing suite green.

### 2.5 Docker / run workflow

- Start: `docker compose up -d` · Logs: `docker compose logs -f app` · Stop: `docker compose down`
- Rebuild after code changes: `docker compose build --no-cache && docker compose up -d`
- `entrypoint.sh` orchestrates: wait for PostgreSQL → Alembic migrations → schema
  bootstrap → queue worker → web server (hypercorn).

### 2.6 Migrations (schema changes)

- Schema changes go through **Alembic**: revisions live in `migrations/versions/`.
- New tables/columns must exist in **both** the migration and `db/schema.py`/bootstrap.

---

## 3) Repo + VS Code workflow (MANDATORY)

You always work in VS Code.

### Branch rules

- Commit **only to `/develop`**.
- Never commit to `main`/`master`.
- Split multi-area changes into separate commits where sensible.

### Commit conventions (conventional-style)


```

feat(api): add /api/artist/bio endpoint
fix(popularity): correct z-score gate for compilation albums
refactor(db): centralize track update queries in database_abstraction
chore(config): add listenbrainz weight to config.yaml

```

Include affected area (`api` / `ui` / `db` / `config` / `popularity` / `single`) in the subject.

---

## 4) Non-negotiable product requirements

### 4.1 Config UX contract: `templates/pages/config.html` → `config.yaml`

- **The Config page (`templates/pages/config.html`) is the source of truth** for all user-editable settings.
- Saving the Config page must write to `config.yaml`.
- Every new configurable option must appear in the Config page first.

### 4.2 Metadata update fan-out (album → tracks)

Any feature that updates track data must update **both**:
1. The database record(s)
2. The physical music file tags (via the `/track` file path)

If an album-level update occurs, **all tracks in that album** must be updated individually.

### 4.3 Matching strategy: MBID-first

- If an MBID exists in the DB → use it for lookup.
- Only fall back to name/artist text search when MBID is missing or invalid.

### 4.4 Database strategy: Postgres only

- All DB I/O goes through short-lived context-manager transactions (`with db_session() as session:`) via `db/engine.py` and `db/repositories/`. 
- Never scatter raw SQL across unrelated modules. Use PostgreSQL-safe SQL and `%s` or named parameters.

---

## 5) Popularity scanning & Single detection implementation

- Popularity is a **0–100 normalized weighted score** combining Last.fm, ListenBrainz, and Age (Spotify deprecated).
- Single detection follows an **8-stage algorithm** managed under `services/enrichment/single_detection_service.py` and `services/popularity/`.
- Star ratings (1–5) combine **popularity z-scores** with **single detection metadata**.

---

## 6) API clients & External service reference

**Directory**: `api_clients/`

| Module | Service | Key methods | Notes |
|--------|---------|-------------|-------|
| `lastfm.py` | Last.fm | `get_track_info()`, `search_track()`, etc. | API key required |
| `musicbrainz.py` / `musicbrainz_http.py` | MusicBrainz | `search_recordings()`, `get_release()`, etc. | Use `get_shared_mb_client()` singleton |
| `audiodb_and_listenbrainz.py` | ListenBrainz + AudioDB | `get_listen_count()`, `get_recommendations()`, etc. | Client wrappers |
| `discogs.py` / `discogs_http.py` | Discogs | `get_comprehensive_metadata()`, etc. | Token required; 0.35 s/req rate limit |
| `navidrome.py` | Navidrome/Subsonic | `fetch_all_playlists()`, `start_scan()`, etc. | Subsonic-compatible API |
| `slskd.py` / `slskd_http.py` | Soulseek (slskd) | `search()`, `enqueue_download()`, etc. | Policy-gated in config |

---

## 7) Full API endpoint reference

All routes are defined across modular blueprints in `routes/` (registered via `helpers/app_bootstrap.py`). 

### Core Blueprint Routing Areas
- **`ui_routes.py`**: Web UI pages (Dashboard, Artists, Albums, Tracks, Downloads, Config, Logs, Setup)
- **`routes/artist_routes.py`**: Artist management, corrections, and missing releases (`/api/artist/...`)
- **`routes/album_routes.py`**: Album tracklists, artwork, bulk tags, and MB/Discogs lookups (`/api/album/...`)
- **`routes/track_routes.py`**: Track CRUD, audio streaming, tagging, lyrics, and credits (`/api/track/...`)
- **`routes/musicbrainz_routes.py`**: Search, import, downloads, and MBID linking (`/api/musicbrainz/...`)
- **`routes/metadata_routes.py`**: Shadow table conflict resolution and release lookups (`/api/conflicts/...`)
- **`routes/listenbrainz_routes.py`**: Last.fm, ListenBrainz, and weekly sync playlists (`/api/listenbrainz/...`, `/api/lastfm/...`)
- **`routes/slskd_routes.py`**: Soulseek proxy search and download management (`/api/slskd/...`)
- **`routes/misc_routes.py`**: Sandbox metrics, search, stats, artist country, duplicate cleanup, and Essentia models (`/api/...`)

---

## 8) Code quality guardrails & Best Practices

- **Strict Session Management:** Always wrap DB queries/mutations inside `with db_session() as session:` to guarantee connection cleanup and transaction safety under concurrent worker loads.
- **Shared Singletons:** Access external API clients via process-wide factories (e.g., `get_shared_mb_client()`) to ensure rate limiting and connection pooling apply globally.
- **Structured Logs (`structlog`):** Write contextual logs with key-value attributes (`logger.info("Task finished", count=x, duration=d)`) rather than string interpolation.
- **Type Safety:** Ensure every function signature is strictly typed using Python 3.10+ syntax (`-> tuple[dict[str, Any], int]`, etc.).
- **Compliance / Download Download Policy:** Download integrations are policy-gated and must only support workflows for authorized user content.

---

**End of RatingAgent Operational Manual**

```