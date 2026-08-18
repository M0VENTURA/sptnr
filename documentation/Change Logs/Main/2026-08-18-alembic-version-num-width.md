# Fix Alembic version_num width (007/008 revisions too long)

## Symptom

Dashboard "All" scan completed instantly with no scan work. Postgres logged:

```
ERROR:  value too long for type character varying(32)
UPDATE alembic_version SET version_num='007_essential_playlist_artist_index' ...
UPDATE alembic_version SET version_num='008_add_missing_releases_tracklist' ...
```

## Root cause

Alembic's default `alembic_version.version_num` column is `varchar(32)`. The
revision IDs I introduced were longer:

- `007_essential_playlist_artist_index` — 35 chars
- `008_add_missing_releases_tracklist` — 34 chars

Every `alembic upgrade head` (run at boot by `entrypoint.sh`) failed to
record those revisions, so the migration chain never advanced past `006` and
the pipeline state was broken (the "All" scan completed instantly).

## Fix

`migrations/env.py::_widen_version_num` — before running migrations online,
widen `alembic_version.version_num` to `VARCHAR(64)` (inspector-guarded and
idempotent; no-op once already ≥ 64). Alembic then records 007/008
successfully — both revisions are idempotent (`CREATE INDEX IF NOT EXISTS`
and inspector-guarded `ADD COLUMN IF NOT EXISTS`), so applying them on the
existing DB is a no-op schema-wise.

## Files

- `migrations/env.py` — `_widen_version_num` called in `run_migrations_online`.
