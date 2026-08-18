# Fix per-source genre backfill (Last.fm / MusicBrainz / Discogs / ListenBrainz)

## Symptom

The album page's "Detected Genres" section only showed **Essentia** and
**Navidrome** tabs — Last.fm, MusicBrainz, Discogs and ListenBrainz genres
were missing even though the album's tracks should have them.

## Root cause

The metadata scan's genre fetches were gated on a SINGLE shared flag
(`_has_genres`, from `_has_real_genres`), which is True when **ANY** of the
five source columns (`musicbrainz_genres`, `discogs_genres`,
`listenbrainz_genres`, `spotify_genres`, `lastfm_tags`) carries data.  Once a
track had ONE source populated (e.g. Last.fm tags from an earlier scan), the
gate `not _has_genres` became False and **ALL** genre fetches were skipped on
every later scan — so MusicBrainz / Discogs / ListenBrainz genres were never
backfilled for those tracks.

The album page only renders a source tab when its column has data
(`get_album_genre_sources` reads the per-source columns), so the missing
fetches showed up as "only Essentia + Navidrome" (Essentia and Navidrome are
populated by different code paths and were unaffected).

## Fix

`services/popularity/stages/track_stage.py` — the MusicBrainz, Discogs and
ListenBrainz genre fetches each now check **their own column** via a new
`_has_source_genres(column)` helper (parses the column the same way
`_has_real_genres` did, treating `[]`/`{}`/null as empty):

- MB genres fetched when `musicbrainz_genres` is empty
- Discogs genres fetched when `discogs_genres` is empty
- ListenBrainz genres fetched when `listenbrainz_genres` is empty

Already-populated sources are left alone; a forced scan still refetches
everything.  The genre data then flows into `update_payload` → persisted to
the DB → the album page's per-source tabs render.

## Tests

No automated test (network-dependent metadata fetch) — verified via
`get_errors` and review of the per-source gate logic.

## Config

No new config keys.  Existing tracks need one metadata / full scan (or a
forced scan) to backfill the missing source columns.
