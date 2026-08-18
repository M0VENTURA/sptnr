# Album lookup: ask for the specific release (Release Picker) instead of auto-picking

## Symptom

On the album page, "Lookup on MusicBrainz" shows release-GROUPS, but
selecting one silently applied the group's auto-picked "best" release —
which is often the WRONG version of the album (wrong edition / format /
country / year).  The user wanted to be asked which specific release they
meant before anything was applied.

## Root cause

`openAlbumLookupModal`'s selection callback always called
`populateAlbumFields` with the release-group MBID (and an empty specific
release id), relying on whatever the auto-pick logic guessed.  The Release
Picker modal (which lists every concrete release of the group with date,
country, format, track count) existed but was only reachable via a secondary
"Pick Release" button on the results — never offered automatically when a
release-group was selected.

## Fix

`static/js/album_detail.js`:

- The lookup callback now branches:
  - **Specific release selected** (an explicit release id from the group's
    release list) → applied directly (unchanged).
  - **Release-group selected** (the common case) → the **Release Picker
    modal opens automatically**, listing every concrete release of the
    group (date / country / format / track count / status) so the user
    picks the exact version.  Only then is the album form populated with
    the chosen release MBID + the group MBID.
- `openReleasePickerModal` gained a `coverArtUrl` parameter (forwarded from
  the selected group's cover art) so the picker can prefill the cover when
  the specific release has no artwork of its own.

## Tests

No automated test (frontend-only change) — manually verified the modal flow:
search → select a release-group → Release Picker opens → pick the exact
release → album fields populate with the release MBID + group MBID.

## Config

No new config keys.
