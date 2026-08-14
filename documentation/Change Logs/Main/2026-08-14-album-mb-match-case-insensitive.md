# Album MB match: case-insensitive library lookup (regression fix)

## Symptom

On the album page, matching to MusicBrainz reported:

1. "The automatic match was not confident enough. Please select the release
   that best matches your local tracks."
2. After selecting a release: "Could not compare tracks: No library tracks
   found for this album"

## Root cause

The album-detail page itself finds tracks with a **case-insensitive** query
(`LOWER(COALESCE(NULLIF(album_artist,''),artist)) = LOWER(:artist) AND
LOWER(COALESCE(album,'')) = LOWER(:album)`), but the MusicBrainz matching
engine used **exact-match** lookups:

- `services/enrichment/musicbrainz_service.py::_get_local_track_count`
  (`SELECT COUNT(*) ... WHERE album_artist = :artist AND album = :album`)
- `services/enrichment/musicbrainz_service.py::compare_musicbrainz_release`
  library-track load (`WHERE album_artist = :artist AND album = :album`)

When the URL-decoded artist/album names differ in case from the stored
values (very common — URLs preserve the case the user typed), the exact
match returned 0 tracks:

- `_get_local_track_count` → 0 → `local_track_count=None` → confidence 0.5
  → "not confident enough" (frontend threshold is `confidence >= 0.8`).
- `compare_musicbrainz_release` → empty library list → "No library tracks
  found for this album".

## Fix

Both lookups now use the same case-insensitive + NULL-safe matching as the
album-detail page query, so any album the page can display is also found by
the MB matcher.  The auto-match confidence now reflects the real local track
count (exact count match → confidence 1.0 → match applies directly), and the
release-picker compare finds the local tracks to build the per-track
comparison.

## Tests

`tests/test_album_musicbrainz_matching.py`:
- `test_local_track_count_is_case_insensitive` — count identical for
  `Artist`/`artist`/`ARTIST`, 0 for a genuinely missing album.
- `test_compare_musicbrainz_release_case_insensitive_lookup` — comparison
  succeeds with lower-cased URL-derived names and matches tracks.
