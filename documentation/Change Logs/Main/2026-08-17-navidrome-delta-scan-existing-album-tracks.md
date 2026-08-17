# Fix: delta scan misses new songs added to existing albums

## Symptom

Running the Navidrome change scan (`/scan/navidrome`, the scheduled
library-sync job, or any scan in delta mode) after adding **new songs to an
already-imported album** reported "no changes" and the new tracks were never
imported into Popularr.

## Root cause

The delta candidate selection (`build_delta_artist_index`) only looked at:

1. **Album-list deltas** (`getAlbumList2` `newest`/`recentlyAdded`) — these
   are ordered by the album's **`created`** timestamp. An existing album that
   gains new tracks keeps its old position, so it never appears.
2. **Song-level deltas** (`getSongs?modified=`) — **Navidrome does not
   implement `getSongs`** (it returns 404), so this source always came back
   empty.

Result: artists whose existing albums gained songs were silently excluded
from the candidate list, unless the delta was *completely* empty (which
triggered the full-index fallback). With any recent album present, the
affected artist was skipped and its new tracks never imported.

## Fix

- **`api_clients/navidrome.py`** — `get_indexes` now sends `ifModifiedSince`
  as epoch **milliseconds** (Subsonic spec; Navidrome's `req.TimeOr` parses
  with `time.UnixMilli`). The previous seconds value was interpreted as
  1970, so the delta gate always passed and the empty-index shortcut could
  never be used.
- **`services/scanning/navidrome_service.py`** — `build_delta_artist_index`
  now queries `getIndexes?ifModifiedSince` as its **primary** delta source.
  Navidrome returns the full album-artist index whenever a library scan
  completed after the cutoff (i.e. anything changed — including new songs in
  existing albums) and an empty index otherwise. The existing album/song
  deltas remain as supplementary sources.
- Once the artist is a candidate, the existing per-artist diff
  (`artist_album_name_diff`) compares Navidrome's `songCount` against the
  local track count, marks the album changed, and the new tracks are
  upserted — no change needed there.

## Files

- `api_clients/navidrome.py` — `ifModifiedSince` in ms for `get_indexes`
- `services/scanning/navidrome_service.py` — `getIndexes` delta source in
  `build_delta_artist_index`
- `tests/test_navidrome_delta_scan.py` — new tests: ms timestamp unit,
  getIndexes delta surfaces existing-album artists, album-delta merge
  fallback, `artist_album_name_diff` songCount growth detection
