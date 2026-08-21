# Album lookup: prompt for the specific release when more than one exists

## Symptom

On the album page, **Actions → Lookup on MusicBrainz** returns one card per
release-group.  The shared search modal previously had NO concrete-release
list on the group cards (the "Choose Release (N)" dropdown never appeared
because the backend never attached `releases`), so selecting a group fell
back to the group's auto-picked "best" release — which is frequently the
wrong edition / format / country.  The user wanted to be asked which release
they mean whenever more than one concrete release exists.

## Root cause

`POST /api/musicbrainz/search` enriched each release-group result with
`category`, `cover_art_url`, `source` etc. but never attached its concrete
releases.  The album-page lookup callback therefore had two choices: apply
the release-group MBID directly (auto-picking the "best" release later) or
open the Release Picker and have it fetch `/api/album/musicbrainz/
release-group/releases` on its own.  Neither asked "which of these N
releases?" up front, and the shared modal's inline "Choose Release" list was
dead UI (no data).

## Fix

`routes/musicbrainz_routes.py`:

- New `_attach_concrete_releases(rg)` helper (inside `api_musicbrainz_search`)
  browses each release-group's releases (`browse_releases_for_group` with
  `inc=media+labels`) and normalises them to the release-picker contract:
  `id`, `title`, `date`, `country`, `status`, `disambiguation`,
  `track_count`, `disc_count`, `formats` (list), `cover_art_url`.  Attached
  as `rg["releases"]`, sorted chronologically (blank dates last).
- The search now enriches with `_enrich_release_group_with_releases` at both
  release-group call sites (release-group index + release index with a track
  term).  Best-effort: a browse failure leaves the group without a
  `releases` key, preserving the old fallback behaviour.

`static/js/album_detail.js` (`openAlbumLookupModal` callback):

- **Explicit concrete release** chosen from the card's "Choose Release" list
  → applied directly (user intent is explicit).
- **Exactly one concrete release** on the group → applied directly
  (unambiguous).
- **More than one concrete release** → the Release Picker modal opens
  immediately, preloaded with the already-fetched list (no second API call),
  so the user picks the exact edition / format / country before the album
  form is populated.
- **Unknown count** (backend couldn't attach the list) → Release Picker
  opens and fetches the list itself (unchanged fallback).

The shared modal's cards now show **"Choose Release (N)"** whenever a group
has more than one concrete release, giving an inline prompt too.

## Files

- `routes/musicbrainz_routes.py`
- `static/js/album_detail.js`
- `tests/test_mb_search_concrete_releases.py` (new)

## Tests

`tests/test_mb_search_concrete_releases.py` covers: a release-group search
result carrying its normalised concrete releases (sorted chronologically),
and a single-release group still being enriched (frontend applies it
directly).  Existing search / release-picker suites still pass.
