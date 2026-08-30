# Album-page MB lookup fixes: release matching, album type, cover URL, genres, missing releases (2026-08-31)

## 1. Selecting a release matched the wrong edition / tracks

**Root cause:** `populateAlbumFields` compared tracks via
`mbCompareId = releaseGroupMbid || mbid` — the release-GROUP took precedence
over the concrete release the user picked.  `compare_musicbrainz_release`
re-resolved the group's auto-picked "best release", which can be a DIFFERENT
edition (EP / instrumental / live vs the album) → the wrong tracklist was
compared ("1 missing, 18 not in release, picked a song not on the release").

**Fix (`static/js/album_detail.js`):** prefer the CONCRETE release the user
chose: `mbCompareId = mbid || releaseGroupMbid`.

## 2. Selecting a release / "Update All Tracks" didn't update the album

**Root cause:** `compare_musicbrainz_release` didn't carry the album TYPE, and
"Update All Tracks" didn't send it.

**Fix:**
- `fetch_musicbrainz_release_metadata` now captures `album_type` from the
  release-group primary/secondary types (via new `_compose_album_type`).
- `compare_musicbrainz_release` returns `mb_albumtype`.
- `updateAllTracksFromMB` applies `musicbrainz_albumtype` +
  `spotify_album_type` to every track.
- `/api/track/update-metadata` allowed `musicbrainz_albumtype` +
  `spotify_album_type`.

## 3. Cover image set to a URL didn't reach the album page / files

**Root cause:** the album-save cover embed only handled `data:` URLs and CAA
via MBID — a plain `http(s)://` cover URL was stored as a string but never
downloaded.

**Fix (`routes/ui_routes.py`):** a plain HTTP(S) cover URL is now downloaded
and embedded into the track files + saved to `album_art`.

## 4. Detected Genres only showed Navidrome

**Root cause:** "Update All Tracks" sent MB genres to the combined `genres`
column only — never the per-source `musicbrainz_genres` column, so the album
page's per-source genre display stayed empty.

**Fix:** `updateAllTracksFromMB` sends `musicbrainz_genres` separately;
`/api/track/update-metadata` now accepts and persists it.  The metadata
scan phase already writes `musicbrainz_genres` / `discogs_genres` /
`lastfm_tags` into their own columns (verified in `track_stage.py`), so those
sources populate on pages once a track is enriched.

## 5. Missing releases not disappearing after being added (artist page)

**Root cause:** the artist page served CACHED `missing_releases` rows via
`get_cached_missing_releases` WITHOUT filtering out rows whose album now
exists in the library — so an added single ("Queen Dies", "Alga") stayed in
the Missing list alongside its now-present library copy (duplicate copies).

**Fix:**
- `get_cached_missing_releases` now filters (and deletes) rows whose release
  title matches a library album (punctuation/case-normalised).
- `_cleanup_imported_releases` (background scan) now matches on normalised
  titles too, so "Queen Dies (Single)" is removed once "Queen Dies" exists.

## Files

- `static/js/album_detail.js`
- `services/enrichment/musicbrainz_service.py`
- `routes/track_routes.py`
- `routes/ui_routes.py`
- `services/metadata/release_service.py`
- `services/metadata/artist_scan_service.py`
