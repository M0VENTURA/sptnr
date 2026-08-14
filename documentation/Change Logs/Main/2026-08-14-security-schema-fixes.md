# Security & schema fixes (2026-08-14)

## 1. Drop plaintext password fallback in login

The login route (`routes/ui_routes.py`) previously fell back to comparing the
submitted password against the **stored Navidrome password in `config.yaml`**
(plaintext) whenever the live Navidrome check failed.  This kept a plaintext
credential comparison in the auth path and let login succeed while Navidrome
was unreachable.

Credentials are now verified **live against Navidrome only**.  If Navidrome
is down, login fails with a clear message — correct, because nothing in the
app works without Navidrome anyway.

## 2. Sync migration 002 with db/schema.py

`migrations/versions/002_add_upcoming_releases.py` created a **minimal**
`upcoming_releases` table (a handful of columns + a three-column unique
constraint `(artist_name, album_name, source)`).  The runtime writers use
many more columns (`mbid_*`, `artist_in_collection`, `status`,
`last_seen_at`, `updated_at`, …) and rely on
`ON CONFLICT (artist_name, album_name)` — which requires a unique key on
exactly those two columns.  A fresh migration-only build produced the wrong
schema (bootstrap repaired it at runtime, but the migration was stale).

- Migration `002` now creates the **canonical** table matching `db/schema.py`
  (full column set + `uq_upcoming_artist_album` unique on
  `(artist_name, album_name)`).
- New migration `006_sync_upcoming_releases` upgrades existing installs that
  applied the old minimal `002`: adds the missing columns idempotently,
  dedupes per-album rows (MusicBrainz rows win), swaps the unique key and
  backfills `last_seen_at`.  Safe on fresh installs too (guarded with
  `IF NOT EXISTS` / `IF EXISTS`).

## Tests

`tests/test_login_no_plaintext_fallback.py`:
- Login rejects the stored config password when Navidrome is unreachable
  (no plaintext fallback).
- Login succeeds with a working live Navidrome verification.
- Unknown usernames are rejected.
- Migration 002 covers the full canonical column set; migration 006 adds the
  canonical columns; the migration chain is linear 001 → 006.
