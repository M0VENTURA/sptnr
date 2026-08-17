# Release-title album naming + missing-release category buckets

## What changed

Three artist-page / scan fixes: album names now follow the release title
(edition markers stripped), missing releases are bucketed into their proper
sections, and every section has a show/hide toggle for missing releases.

## Fix 1 — album naming based on the release title (`helpers/normalization_service.py`, `services/scanning/navidrome_import.py`)

### Symptom

Navidrome album folders carry an edition marker ("Slipknot (Clean)",
"Weezer (Deluxe Edition)") and the scan stored it verbatim as the album name.

### Fix

- New `strip_album_edition_marker()` in `helpers/normalization_service.py` —
  strips a trailing bracketed edition marker (Clean, Explicit, Deluxe,
  Special Edition, Expanded, Anniversary, Limited, Collector's, Super
  Deluxe, Standard, Digital, Remastered...). Live/Remix/Acoustic markers
  are preserved — they change what the album IS, not just which edition.
- Applied at the album-name read in `scan_artist_to_db` (Navidrome import)
  and in `artist_album_name_diff` so DB-vs-Navidrome comparison uses the
  same release-title form.
- Migration-safe: a legacy DB row still stored as "Slipknot (Clean)" is
  flagged CHANGED (not removed) on the first post-migration diff scan, so
  the import re-runs and the upsert rewrites the album column to
  "Slipknot". `prefetch_artist_state` keys `existing_album_tracks` by the
  stripped name so existing tracks are not treated as brand-new.

## Fix 2 — missing releases bucketed by sub-type (`services/metadata/artist_scan_service.py`, `services/popularity/release_cache_service.py`)

### Symptom

ALL missing releases appeared under Albums on the artist page — live albums
under Albums, EPs under Albums, compilations under Albums.

### Root cause

The MusicBrainz search API returns `secondary-types` as a comma-joined
STRING ("Live,Compilation"); the browse API returns a list. The category
classifiers (`_categorize_release` / `_derive_musicbrainz_category`)
iterated the field with `for s in (...)`, so a string was iterated
character-by-character and never matched `"live"` / `"compilation"` —
everything fell through to "Album".

### Fix

Both classifiers now normalise `secondary-types` to a list first (string →
split on comma, list → passthrough), mirroring
`musicbrainz_service._parse_secondary_types`. Live albums land under Live,
EPs under EPs, compilations under Compilations, remixes under Remix.

## Fix 3 — per-section show/hide missing toggle (`static/js/artist_detail.js`)

- `categoryToSection` used the stale `studio-albums` id for plain albums —
  JS-injected missing albums were silently dropped. Now maps to `albums`.
- `ensureToggleMissingButton()` creates a Show/Hide-Missing toggle when
  `checkMissingReleases()` injects missing rows into a section that had
  none (the server only renders the button when the DB already has missing
  rows). All six sections (Albums, Compilations, Live, Remix, EPs, Singles)
  get the toggle; preference persists in localStorage (`showMissing-<cat>`).

## Files

- `helpers/normalization_service.py` — `strip_album_edition_marker`.
- `services/scanning/navidrome_import.py` — release-title album naming in
  the import loop + diff + prefetch state.
- `services/metadata/artist_scan_service.py` — `_categorize_release`
  secondary-types list normalisation.
- `services/popularity/release_cache_service.py` — same fix in
  `_derive_musicbrainz_category`.
- `static/js/artist_detail.js` — `categoryToSection.album` → `albums`,
  `ensureToggleMissingButton`, improved `getMissingCategory` title fallback.
- Tests: `tests/test_album_release_title_naming.py`,
  `tests/test_missing_release_category_buckets.py`.
