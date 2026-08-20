# Artist page: missing-release album art + expandable tracklists

Date: 2026-08-20

## Symptom

Missing releases on the artist page had **no album art** and **could not be
expanded** to see the track list below each release.

## Root causes

1. **No album art** — `_fetch_musicbrainz_release_groups` called
   `browse_artist_release_groups` WITHOUT `inc`, so each release-group's
   `cover-art-archive` block was absent and `_release_cover_art_url`
   always returned `""`.  The server-rendered missing rows then rendered
   an `<img>` with an empty `src` (broken image), and the JS-injected rows
   fell back to the placeholder.
2. **No expandable tracklist** —
   - The JS `checkMissingReleases` injector built FLAT rows (no accordion
     chevron, no collapse body) — nothing to expand.
   - The server-rendered missing rows DID have an accordion (via the
     `_album_category_section` macro), but their `tracks` was empty (the
     release is not in the library and not in the downloads cache), so the
     body always showed "No track details available".

## Fixes

1. **`services/metadata/artist_scan_service.py`** — browse release-groups
   with `inc="cover-art-archive"` so real Cover Art Archive URLs are built
   and persisted (`cover_art_url`).
2. **`routes/artist_routes.py`** — new endpoint
   `GET /api/artist/missing-release-tracks?release_id=<release-group MBID>`:
   browses the group's releases, fetches the first release's recordings,
   and returns a flat tracklist (`position`, `title`, `length`,
   `disc_number`).  Falls back to the raw `recordings` list when `media`
   is absent.
3. **`static/js/artist_detail.js`** —
   - The `checkMissingReleases` injector now builds proper accordion rows
     (chevron + collapse body) matching the server-rendered album rows,
     with cover-art fallback to a music-note placeholder.
   - New `loadMissingReleaseTracklist()` lazy-fetches the MusicBrainz
     tracklist on first expansion.
   - `initArtistSingleExpansion` now lazy-loads SERVER-rendered missing
     rows too (via `data-missing-release-id` on the chevron).
4. **`templates/components/_album_category_section.html`** —
   - Missing rows with no cover art render the music-note placeholder
     instead of an empty `<img>`.
   - The chevron button carries `data-missing-release-id` / artist / title
     so the JS can lazy-load its tracklist.

## Tests

`tests/test_missing_release_art_and_tracklists.py`:
- the browse call includes `inc=cover-art-archive`;
- `_release_cover_art_url` builds a URL when artwork exists and returns ""
  otherwise;
- the tracklist endpoint flattens release recordings, errors on a missing
  release, and falls back to the recordings list.
