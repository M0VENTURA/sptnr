# Fix missing_releases.tracklist column + scan futures fallout

## Symptom

Every popularity scan logged Postgres errors on the server:

```
ERROR:  column "tracklist" does not exist at character 196
STATEMENT:
    SELECT release_id, title, primary_type, category
    FROM missing_releases
    WHERE LOWER(artist) = LOWER('Korn')
      AND (tracklist IS NULL OR tracklist = '[]')
    ORDER BY last_checked ASC NULLS FIRST
    LIMIT 3
```

The dashboard "All" scan also threw many "futures unfinished" errors, and
previous work on the full-scan startup / stale-row self-heal was obscured by
these DB failures.

## Root cause

`services/popularity/release_cache_service.py::populate_missing_release_tracklists`
reads and writes a `missing_releases.tracklist` column that **was never added
to the schema** (only the SELECT/UPDATE in code referenced it). The per-artist
prefetch stage calls it for every artist, so every scan hit the missing-column
error — caught at DEBUG level, but the repeated failed DB sessions under the
per-track thread pool contributed to connection contention and "N (of N)
futures unfinished" timeouts.

## Fix

- `db/schema.py` — `missing_releases` DDL now includes `tracklist TEXT`; the
  `COLUMN_REGISTRY` gains `"missing_releases": {"tracklist": "TEXT"}` so
  bootstrap (`ensure_full_schema`) adds the column on existing installs.
- `db/models.py` — `MissingRelease.tracklist` column.
- `migrations/versions/001_initial_schema.py` — `tracklist` added to the
  initial `missing_releases` table for fresh installs.
- New migration `008_add_missing_releases_tracklist` — `ADD COLUMN IF NOT
  EXISTS tracklist TEXT` (inspector-guarded, idempotent for both fresh and
  existing installs).
- `entrypoint.sh` runs `alembic upgrade head` so existing deployments get
  the column on next boot (fallback `stamp head` + bootstrap also covers it).

## Files

- `db/schema.py`, `db/models.py`
- `migrations/versions/001_initial_schema.py`,
  `migrations/versions/008_add_missing_releases_tracklist.py`
