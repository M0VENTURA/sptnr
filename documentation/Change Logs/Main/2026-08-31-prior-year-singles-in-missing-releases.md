# Prior-year singles in missing releases (2026-08-31)

## Request

With "missing releases" and "singles only" showing the current year, also
show singles that aren't matched to any track on an album release from prior
years.  I.e. the full singles catalogue should appear as missing — except a
single whose track is already owned on a library album (e.g. the "Queen Dies"
single when the album "The Realms of Fire and Death" — which contains it — is
in the library).

## Root cause

Two gates limited singles to the current year, and neither checked whether
the single's TRACK was already owned:

1. `services/popularity/release_cache_service.py::refresh_missing_releases_for_artist`
   (the popularity-scan prefetch path) skipped **every** single whose year !=
   current year (`current_year` gate).
2. `services/metadata/artist_scan_service.py::_build_missing_release_items`
   (artist-page "Check Missing" live scan) already included old singles, but
   only filtered against library ALBUM titles — a single whose title matches
   a track on an owned album (but not the album title itself) still showed as
   missing.

## Fix

- **Removed the current-year gate** in the cache-driven prefetch — singles
  from ANY year are now persisted as missing (the `current_year` local was
  removed).
- **Added a library-track-title check** in both builders: a SINGLE whose
  normalised title matches a track already in the library (on any album) is
  skipped — it is not missing.  New helper
  `_library_track_titles(artist)` in `release_cache_service.py`; the
  artist-scan service fetches the same set inline and passes it via the new
  optional `library_track_titles` param on `_build_missing_release_items`
  (used by BOTH callers: `get_missing_releases` and
  `_run_missing_releases_scan`).
- **Stale-row cleanup** so previously-persisted covered singles disappear:
  - `refresh_missing_releases_for_artist` selective-replacement now also
    deletes rows whose title is covered by a library track.
  - `release_service.get_cached_missing_releases` (artist-page read path)
    now also drops/deletes SINGLE rows whose track is in the library.
  - `_cleanup_imported_releases` (background scan) now also deletes SINGLE
    rows whose normalised title matches a library track title.

## Files

- `services/popularity/release_cache_service.py` (prefetch path + helper)
- `services/metadata/artist_scan_service.py` (live-scan path, both callers,
  cleanup)
- `services/metadata/release_service.py` (read-path cleanup)

## Tests

- `tests/test_missing_releases_population.py`:
  `test_missing_releases_prior_year_singles_not_covered_by_library_track`
- `tests/test_release_category_persistence.py`:
  `test_refresh_includes_prior_year_singles_but_not_library_covered` +
  `test_refresh_includes_prior_year_single_not_in_library`

Cannot be run locally (no python on this machine) — validated via
`get_errors` + syntax only.
