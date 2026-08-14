# Album "Lookup on MusicBrainz" → shared MB search component (unified)

## Symptom

On the album page, Actions → "Lookup on MusicBrainz" → picking a release
reported the compare found "no tracks matching release", and the compare
request was slow (`POST /api/album/musicbrainz/compare` ~2.7s).

The album page used a **bespoke** lookup (`_album_lookup.html` +
`/api/album/musicbrainz`) that handed the compare engine either a concrete
RELEASE MBID (when `mbid_type === 'release'`) or a release-GROUP MBID. When a
release MBID was passed as `release_group_mbid`, `compare_musicbrainz_release`
wasted a group browse on it (MusicBrainz 404s browsing a release id as a
release-group) before falling back — slow and fragile, and the picked release
did not resolve the tracklist the user expected.

## Change

Unified the album lookup onto the **shared** MusicBrainz release search
component (the same one wired into the download queue's matched-folders flow):

- `static/js/album_detail.js::openAlbumLookupModal()` now opens the global
  `#musicBrainzModal` (base.html), pre-fills artist/album via
  `populateMusicBrainzSearch()` and applies the selection via
  `window._mbSearchCallback`:
  - `rgMbid = selected.release.id || selected.id` (release-GROUP MBID from the
    shared component), `releaseMbid = selected.id` when a concrete release was
    chosen.
  - `populateAlbumFields(..., releaseMbid, ..., rgMbid)` → triggers
    `compareMBTracksWithLibrary(rgMbid)` — compare always gets the group MBID
    now, so best-release resolution picks the correct edition's tracklist.
- `templates/pages/album_detail.html` — removed the bespoke
  `components/modals/_album_lookup.html` include (orphaned; shared modal is
  global). `populateAlbumFields` guards the old modal hide.
- `services/enrichment/musicbrainz_service.py::compare_musicbrainz_release` —
  hardened the release resolution: a concrete RELEASE MBID is used directly
  (no group browse → fixes the slow request); a release-GROUP MBID still goes
  through `get_musicbrainz_best_release` so the best edition is compared.

## Tests

`tests/test_album_musicbrainz_matching.py`:

- `test_compare_uses_concrete_release_directly` — a concrete release MBID is
  used directly (`browse_releases_for_group` not called) and the comparison
  still produces the matched tracks.
- Existing compare tests (group MBID path) remain green.
