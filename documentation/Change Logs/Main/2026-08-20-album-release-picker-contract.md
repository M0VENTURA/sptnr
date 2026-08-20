# Album release-picker: no releases loaded after "not confident enough"

Date: 2026-08-20

## Symptom

On the album page, using the MusicBrainz lookup → select an album → the
release-picker modal opens with "The automatic match was not confident
enough. Please select the release…" but the releases list never loads
(empty results area).

## Root cause

`get_release_group_releases` (the fallback the picker uses when
`best-release` fails or is not confident) returned **raw MusicBrainz
release dicts** — each carries a `media` array, NOT the flat `formats`
list the renderer expects.  The picker's
`r.formats.join(' + ')` threw a TypeError on the raw shape, the render died
silently, and the results div stayed empty.

## Fixes

1. **`services/enrichment/musicbrainz_service.py::get_release_group_releases`**
   now normalises every release to the SAME contract
   `get_musicbrainz_best_release` uses: `id`, `title`, `date`, `country`,
   `status`, `disambiguation`, `track_count` (summed from media),
   `disc_count`, `formats` (flat list from media formats) and
   `cover_art_url`.
2. **`get_musicbrainz_best_release`** browses releases with
   `inc="media+labels"` (dropped `recordings` — the release BROWSE endpoint
   can reject inc combinations; recordings are fetched lazily by the
   picker's "Tracks" button).
3. **`static/js/album_detail.js`** — the picker renderer is now tolerant:
   missing `formats` falls back to deriving from `media`, and `track_count`
   guards against `undefined` (renders `?` instead of crashing).

## Tests

`tests/test_album_release_picker_contract.py`:
- `get_release_group_releases` returns the normalized shape (formats list,
  disc_count, track_count, cover_art_url);
- no-media releases yield empty formats / zero counts;
- API errors return `success: false`;
- best-release browses with `media+labels` (no `recordings`).
