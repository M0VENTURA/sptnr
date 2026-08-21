# Album Artist = primary credit; multi-artist stays on Track Artist

## Symptom

A Weezer collaboration import (artist-credit "Weezer & Rivers Cuomo") had
BOTH artists written to ALBUMARTIST, so Navidrome split the album into two
separate albums.  Album Artist must match the release's PRIMARY artist; the
full multi-artist credit belongs on the per-track Track Artist.

## Root cause

Everywhere the app derived the album artist from a MusicBrainz
``artist-credit`` array it joined ALL credits ("Weezer & Rivers Cuomo") and
used that string for the album artist:

- `build_artist_credit_string` in `musicbrainz_service.py` (used by
  `fetch_release_metadata` / `fetch_musicbrainz_release_metadata`).
- The folder-match flow (`download_folder_service.py`) joined credits with a
  space.
- The shared search route (`musicbrainz_routes.py`) joined credits into the
  result's `artist` field, which the queue-match / download flows copied to
  `album_artist`.
- `_lookup_existing_mbid` built the stored-release display artist the same
  way.

Navidrome treats "Weezer & Rivers Cuomo" as two album artists and splits the
album (one per artist).  The fix: ALBUMARTIST gets the FIRST credit's name
only; per-track ARTIST carries the full joined credit.

## Fix

`services/enrichment/musicbrainz_service.py`:

- New `primary_album_artist(artist_credit)` — returns the FIRST credit's
  name (ignoring join phrases); accepts a list or a plain string.
- `fetch_release_metadata` and `fetch_musicbrainz_release_metadata` now set
  `release_info["artist"]` (the album artist) to the primary credit, keep
  the full joined string in `release_info["artist_credit"]`, and give each
  track an `artist` = the recording's OWN artist-credit (full joined string
  for collab tracks, falling back to the release's joined credit).
- `_lookup_existing_mbid` uses the primary credit for the stored-release
  display artist.

`routes/musicbrainz_routes.py`:

- `_enrich_release_group` builds the result's `artist` from the FIRST credit
  name only (the joined string is still available via `artist-credit`), so
  the queue-match / download flows receive a single album artist.

`services/downloads/download_folder_service.py`:

- `associate_folder_to_release` and `match_folder_to_release` use
  `primary_album_artist` for the album artist; the full joined credit is
  used as the per-track fallback artist.

## Files

- `services/enrichment/musicbrainz_service.py`
- `routes/musicbrainz_routes.py`
- `services/downloads/download_folder_service.py`
- `tests/test_album_artist_multi_artist.py` (new)

## Tests

`tests/test_album_artist_multi_artist.py` covers: the primary-album-artist
helper (multi-artist, single, empty, string), `fetch_release_metadata`
returning primary album artist + per-track joined artist (with and without a
recording credit), `fetch_musicbrainz_release_metadata` the same, the search
route's artist extraction, and `associate_folder_to_release` storing the
primary credit.
