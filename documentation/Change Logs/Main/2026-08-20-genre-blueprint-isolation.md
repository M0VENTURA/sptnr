# Genre blueprint: Essentia isolation, admin-tag filter, artist safety net + MB browse 400 fix

Date: 2026-08-20

## 0. Hotfix: MusicBrainz browse 400 (from the previous commit)

`browse_artist_release_groups` was called with `inc="cover-art-archive"` —
the release-group **browse** endpoint returns **400 Bad Request** for that
`inc` (it is only valid on lookups of a specific release-group).  The
release-group entity includes the `cover-art-archive` block **by default**,
so the inc was both wrong and unnecessary.  Removed — missing-release
cover art URLs now build correctly AND the scan no longer 400s
(`[MISSING_RELEASES] MusicBrainz fetch failed for DArtagnan: … 400 Bad
Request`).

## 1. Phase 1 — Isolate Essentia (mood provider only)

Essentia's text tags no longer vote in genre consensus:

- `services/popularity/stages/track_stage.py`:
  - The per-track genre `source_map` no longer includes
    `("essentia_genres", "essentia")` (the "Parent---Child" parsing block
    is gone).
  - `_album_top_genres` no longer reads `essentia_genres`.
- `services/scanning/pipelines/essentia_scanner.py`: genre writes are now
  gated behind the existing `tag_genres` config (default **False** → the
  scan is mood/audio-features only).  When enabled, it still stores
  `essentia_genres` but the consensus-owned `genres` column is untouched by
  Essentia — genre assignment flows through the aggregator only.

## 2. Phase 1 — Administrative-tag stripping

New `is_admin_genre()` + `_strip_admin_genre_markers()` in
`services/enrichment/genre_aggregation_service.py`:

- Drops administrative labels: `cover`, `tribute`, `live`, `unplugged`,
  `remix`, `demo`, `mashup`, `soundtrack`, `score`, `karaoke`,
  `instrumental`, `bootleg`, `promo`, `sampler`, …
- Strips parenthetical literals (`(cover)`, `(album fallback)`,
  `Nu Metal (album fallback)`, `(remix)`) and remaster/bonus/edit suffix
  markers before the vote.
- Wired into `aggregate_genres`, `get_top_genres_with_navidrome` and
  `update_get_top_genres_with_navidrome` so a lone Last.fm "cover"/"live"
  tag can never create a `Cover - Top Tracks.m3u` / `Live - Top Tracks.m3u`
  playlist.

## 3. Phase 4 — Artist-level safety net

New `_artist_dominant_genres()` in `track_stage.py`: when a track yields no
consensus genres AND the album has no usable genre data either, inherit the
artist's dominant catalog genres (aggregated through the same consensus
model across the artist's stored genre columns, limited to 500 rows).  The
log now reports `(album fallback)` / `(artist fallback)`.

## Tests

- `tests/test_missing_release_art_and_tracklists.py` — the browse call now
  carries NO inc (the 400 regression); cover-url building unchanged.
- `tests/test_genre_consensus_aggregation.py` — new `TestAdminGenreFilter`
  (admin labels filtered, parenthetical literals stripped, never reach the
  vote).
- `tests/test_album_genre_fallback.py` — `essentia_genres` never vote;
  admin siblings filtered; `_artist_dominant_genres` aggregates the artist
  catalog.
