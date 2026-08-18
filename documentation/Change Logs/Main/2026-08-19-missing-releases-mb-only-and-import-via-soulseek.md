# Missing releases: MusicBrainz-only source + import via MB search → Soulseek

## Symptom

1. **Missing releases were polluted by Discogs.** The scan-pipeline
   `refresh_missing_releases_for_artist` read ALL of `artist_release_cache`
   (both MusicBrainz and Discogs rows) when detecting gaps.  Discogs rows
   carry format-token categories that are less reliable (a reissue/
   compilation-mislabeled row, an EP classified as an Album when the format
   token is absent), and Discogs' release list includes thousands of
   bootlegs / live audience recordings that are not real releases.  The
   result: the artist page's missing buckets were flooded and the
   Studio / Live / Remix / Compilation splitting was muddied by Discogs
   rows.

2. **The import button was a dead-end.** Clicking Import on a missing album
   called `/api/artist/import-release`, which fetched the tracklist and
   created PLACEHOLDER database rows (no audio files) — nothing playable
   was ever downloaded.  The user expected it to use the same MusicBrainz
   search flow used elsewhere, prepopulated with the missing entry, then
   download the selected release through Soulseek.

## Fix 1 — MusicBrainz-only missing-releases

`services/popularity/release_cache_service.py` —
`refresh_missing_releases_for_artist` now filters
`AND source = 'musicbrainz'` when reading `artist_release_cache`.  Discogs
rows remain in the cache (they still feed singles detection via
`get_artist_single_titles(source="discogs")`), but they can no longer seed
`missing_releases`.  MusicBrainz secondary types are the authoritative
category source for the Studio / Live / Remix / Compilation buckets.

The artist-page live check (`get_missing_releases`) was already
MusicBrainz-only — this closes the scan-pipeline path.

## Fix 2 — Import button → MusicBrainz search → Soulseek download

- `static/js/artist_detail.js` — both `importRelease` (the v2 album-row
  Import button) and `importMissingRelease` (the older inline flow) now open
  the canonical MusicBrainz search modal via `window.openGlobalMbSearch`,
  PREPOPULATED with the missing entry (artist + title).  When the user
  selects a release, `downloadMbRelease(..., 'slskd')` queues it for
  download through Soulseek (falling back to
  `downloadReleaseViaSoulseek` when `downloads.js` is not loaded).
- `templates/pages/missing_releases.html` — `importMissingRelease` uses the
  same MB-search → Soulseek flow.
- The old `/api/artist/import-release` placeholder-record flow is no longer
  wired to any button (the route stays for API/backward compatibility).

## Tests

`tests/test_release_category_persistence.py`:
- `test_refresh_excludes_discogs_rows` — MusicBrainz rows seed
  `missing_releases`; Discogs-only rows (bootleg live, compilation) never do.

## Config

No new config keys — the MusicBrainz-only filter is unconditional (Discogs
is not a reliable missing-release source).
