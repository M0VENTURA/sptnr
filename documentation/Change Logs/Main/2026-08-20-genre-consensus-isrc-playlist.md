# Genre consensus aggregation + ISRC list parsing + playlist POST hardening

Date: 2026-08-20

## 1. ISRC list-bracket parsing bug

**Symptom**: Tracks with the ISRC stored as a *list* (Navidrome/OpenSubsonic
`tags` map, MusicBrainz `isrcs` array) rendered as the literal
`"['NLA321400382/NLA321400448']"` — brackets and quotes included.  Every
downstream ISRC lookup (ListenBrainz by-recording, `resolve_isrc_recording`)
received the bracketed junk string, failed, and the scan fell back to the
slow album-tracklist LB match instead of an instant ISRC hit.  `Snuff`
parsed clean (`NLA321292284`) while `Custer`/`I Am Hated` printed
`['NLA...']` because the raw list hit `str()`.

**Fix**:
- `helpers/normalization_service.py::normalize_isrc` — unpacks list/tuple/
  set values and returns the FIRST valid 12-char code (e.g.
  `["NLA321400382/NLA321400448"]` → `NLA321400382`).
- `services/popularity/popularity_sources.py::resolve_isrc_recording` —
  refuses bracketed input before calling the API (a `[junk]` parse artifact
  can never reach MusicBrainz).
- `services/popularity/stages/track_stage.py` — the ISRC pool normalises a
  bracketed tag before the Last.fm / ListenBrainz arms consume it.

## 2. Last.fm junk genre pollution → consensus aggregation

**Symptom**: raw crowdsourced Last.fm top-tags (release years, moods) were
treated as official genres — `Top genres for "Custer" (Slipknot): 2014,
2015, alternative metal` — and with `genre=create/delete` enabled the
playlist writer would generate literal `2014 - Top Tracks.m3u` /
`beautiful - Top Tracks.m3u` files.

**Fix** — consensus voting in `services/enrichment/genre_aggregation_service.py`:

1. **Junk-tag filter** (`is_junk_genre`, default on, `genres.junk_filter`):
   years, embedded digits, and a blacklist of moods/adjectives/noise
   (`beautiful`, `romantic`, `seen live`, `favourite`, …) are blocked
   BEFORE the vote — they can never reach the `genres` column or a playlist
   name.
2. **Split-vote stacking** (`normalize_genre_for_vote`): `nu metal`,
   `nu-metal` and `NuMetal` collapse onto one vote key (`numetal`) so
   weights accumulate instead of splitting — a genuinely heavy track no
   longer falls out of its genre playlist because three sources spelled it
   differently.  Display name resolves back to the most readable form
   (space-separated > hyphenated > CamelCase-split).
3. **Consensus threshold** (`genres.min_weight`, default **0.25**): a genre
   must reach the combined weight to survive.  A lone Last.fm tag (0.10) is
   discarded; a lone Discogs genre (0.25) passes; Last.fm + Essentia (0.30)
   passes.  Set 0 to disable.
4. **New source weights**: `listenbrainz` (0.15), `navidrome` (0.30),
   `manual` (0.30) added to `get_genre_weights` defaults so local/curated
   sources can pass alone.

## 3. Album top-genres fallback for sparse tracks

**Requirement**: "If tracks don't have 3 genres on them it should use the
top Genres from the album."

`services/popularity/stages/track_stage.py` — after per-track aggregation,
a track with fewer than 3 genres is topped up from `_album_top_genres()`
(aggregates the album's sibling genre columns through the same consensus
model).  A track with NO passing genre inherits the album's top genres
entirely (logged as `(album fallback)`).

## 4. Navidrome playlist create — POST body hardening

**Symptom**: the old `create_navidrome_playlist` wrapper called
`_get_subsonic_response("createPlaylist", params=…)` — a GET with the
`songId` list passed to httpx's query serialiser, producing the
`sequence item 1: expected a bytes-like object, tuple found` TypeError for
large playlists.

**Fix**: `services/playlists/playlist_navidrome_service.py` —
`create_navidrome_playlist` now delegates to the client's
`create_playlist` (form-encoded POST body, repeated `songId=` fields, no
URL-length limit).  The active pipeline (`sync_playlist_by_name` →
`client.create_playlist`) was already correct; this closes the legacy
wrapper.

## Config page

`templates/pages/config.html` + `static/js/config.js` — Genre Aggregation
section gains:
- ListenBrainz / Navidrome / Manual source weights
- Consensus Threshold (`genres.min_weight`, default 0.25)
- Junk-Tag Filter toggle (`genres.junk_filter`, default on)

## Tests

- `tests/test_genre_consensus_aggregation.py` — junk filter, split-vote
  stacking (`nu metal`/`nu-metal`/`NuMetal`), consensus threshold,
  playlist-name safety.
- `tests/test_isrc_list_parsing.py` — `normalize_isrc` list handling +
  bracketed-ISRC API guard.
- `tests/test_album_genre_fallback.py` — `_album_top_genres` aggregation.
