# Fix: missing releases persist across page reloads (selective cache refresh)

## Symptom

Clicking "Check Missing" on the artist page populated all the missing
releases in the UI, but they did not survive leaving and re-entering the
page — the missing rows "reset" even though the live scan had clearly found
them.

## Root cause

Two paths write `missing_releases` and one of them was destructive:

1. **Artist page "Check Missing"** → `get_missing_releases` → live
   MusicBrainz BROWSE lookup → `_persist_missing_releases` (DELETE +
   INSERT for the artist).  The frontend renders the rows it gets from the
   API response, so the click always looks successful.
2. **Metadata/popularity scan prefetch** →
   `refresh_missing_releases_for_artist` (runs for every artist during a
   scan) read `artist_release_cache` (SEARCH endpoint) and did a blind
   `DELETE FROM missing_releases WHERE LOWER(artist) = LOWER(:artist)`
   before re-inserting only cache-derived rows.

The cache's SEARCH endpoint does not return every release the BROWSE
endpoint does (and it is only populated when a scan prefetches the artist).
So whenever a scan's prefetch ran for the artist after the user clicked
"Check Missing", the freshly-persisted rows were deleted and not re-inserted
— they reset on the next page load.

## Fix

`services/popularity/release_cache_service.py::refresh_missing_releases_for_artist`
no longer wipes the artist's whole `missing_releases` bucket.  It now:

- Reads the artist's existing rows (id + title).
- Deletes **only** rows whose normalized title is being re-inserted from the
  cache (they get replaced with fresh cache data), or whose release is now
  in the library (stale-cleanup parity with the old DELETE-all).
- Leaves rows the artist-page live scan (or the album page's manual add)
  persisted that the cache does not know about **intact**.

Additionally, `services/metadata/artist_scan_service.py::get_missing_releases`
now logs a persist failure at **error level with traceback** instead of a
warning, so a DB write failure is never invisible (the API response is built
from the in-memory items regardless, which is why the UI could look
populated while the DB was not).

## Files

- `services/popularity/release_cache_service.py` — selective replace in
  `refresh_missing_releases_for_artist` + docstring update.
- `services/metadata/artist_scan_service.py` — persist failure logged at
  error level.
- `tests/test_release_category_persistence.py` — two new regression tests:
  artist-page rows survive a cache-only refresh; cache-owned rows are still
  replaced.
