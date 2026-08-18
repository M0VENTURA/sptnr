# Preserve missing-release categories across metadata-sync refreshes

## Symptom

Clicking "Find Missing" on the artist page correctly bucketed missing
releases (Live / Compilation / Remix etc.).  Running a metadata sync then
**flattened them all back into Albums**.

## Root cause

Two different paths write `missing_releases`:

1. **Artist page "Find Missing"** → `get_missing_releases` → the MusicBrainz
   BROWSE endpoint (secondary-types as a list) → `_categorize_release` writes
   correct categories (Live Album / Compilation / Remix).
2. **Metadata-sync prefetch** → `refresh_missing_releases_for_artist` reads
   `artist_release_cache` (populated by the SEARCH endpoint, which can omit
   secondary-types, or rows written before the comma-string parsing fix) and
   does a **DELETE + INSERT** of every missing release using the cache's
   `category` — falling back to the generic `'Album'`.  This overwrote the
   artist page's correct categories with "Album".

## Fix

`services/popularity/release_cache_service.py::refresh_missing_releases_for_artist`
now reads the artist's EXISTING `missing_releases` categories before the
rewrite, and when the cache only has the generic `'Album'` for a title, it
**preserves the more specific existing category** (Live Album / Compilation /
Remix) instead of flattening it.  A metadata sync no longer undoes the
artist-page browse scan's bucketing.

## Files

- `services/popularity/release_cache_service.py` — preserve specific
  existing categories over generic cache values.
- `tests/test_release_category_persistence.py` — new regression test.
