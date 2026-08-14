# Album page MusicBrainz lookup returns owned releases again (2026-08-14)

## Symptom

On the album page, "Lookup on MusicBrainz" (Actions menu) returned **no
MusicBrainz results for albums already in the collection** — searching for an
album the user owns showed "No results found" even though MusicBrainz had it.
The same complaint came up for the matched-folder search.

## Root cause

The shared `POST /api/musicbrainz/search` endpoint strips release-groups the
user already owns (the "library dedupe") so the MusicBrainz tab in discovery
mode reflects only missing albums. The dedupe ran for **every** search,
including targeted artist+album lookups. The album page, folder-match and
re-match flows all search for a *specific* release the user usually already
owns — so the owned release-group was filtered out and the search looked
broken. The old_system album lookup (`/api/album/musicbrainz`) never deduped,
which is why it worked there. The `include_owned` flag existed as a fragile
one-shot global workaround on the frontend, but the search should not depend
on it.

## Fix

- `routes/musicbrainz_routes.py` — the library dedupe now runs only for
  **discovery-style** searches: artist-only browsing or free-text queries.
  Any search carrying an explicit `album` or `track` term always keeps owned
  release-groups (that is "match/locate this release", and the owned edition
  is exactly what the user is looking for). `include_owned` remains as an
  explicit opt-out when discovery semantics are wanted on a targeted search.
- `templates/components/_musicbrainz_search_component.html` +
  `static/js/downloads.js` — when the search endpoint returns an error
  (e.g. MusicBrainz throttling mid-scan), the modal now shows the actual
  error instead of a misleading "No results found".
- `services/enrichment/musicbrainz_service.py` — `_get_local_track_count` now
  matches case-insensitively (like the album-detail page and the compare
  query). URL-decoded artist/album names frequently differ in case from the
  stored values; the exact match returned 0 tracks, collapsing the album
  page's auto-match confidence to "not confident enough".

## Tests

- `tests/test_mb_search_owned_releases.py` (new): explicit artist+album and
  track searches keep owned release-groups and do not invoke the dedupe;
  artist-only discovery still dedupes; `include_owned` still opts out.
- `tests/test_album_musicbrainz_matching.py`: fixed a syntax error and the
  `db_session` monkeypatch target so the compare/confidence tests actually
  run; seeded library tracks with recording MBIDs so exact matches are not
  flagged as needing an MBID update; the case-insensitive local-track-count
  test now passes.
- `tests/conftest.py`: point `LOG_PATH` at a writable scratch path so the
  app can import in test environments without `/config`.
