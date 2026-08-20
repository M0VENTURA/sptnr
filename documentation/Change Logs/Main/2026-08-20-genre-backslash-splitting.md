# Genre backslash splitting — no more literal `metal\nu metal\rock` folders

Date: 2026-08-20

## Symptom

Navidrome's genre list showed one broken genre folder literally named
`metal\nu metal\rock` instead of three genres (`metal`, `nu metal`,
`rock`).  The genre string reached the audio files and the DB as a SINGLE
backslash-joined value — ID3v2.3 / Navidrome convention joins multiple
genres with `\`, and the writers were not splitting on backslash.

## Root causes

1. **`write_id3_tags`** split genres with `re.split(r"[,;/]+", …)` — the
   regex omitted `\`, so `metal\nu metal\rock` became ONE TCON value with
   literal backslashes.  Navidrome reads that frame as a single genre.
2. **`write_flac_tags`** wrote a string genre as `audio[field] =
   [str(value)]` — one Vorbis `GENRE` value containing backslashes.
3. **`_album_top_genres`** (track_stage) fell back to `raw.split(",")` for
   plain-text columns — `navidrome_genres` (backslash-separated) produced
   one broken item.
4. The **edit APIs** stored whatever the frontend sent: the edit modals
   join genres with `\` (`editTrackCurrentGenres.join('\\')`), so a
   backslash-joined string was persisted to the `genres` DB column and
   then flowed into the file writers, the genre playlist pools and the
   Navidrome genre sync unchanged.

## Fixes

- **`tag_file_service.py`**:
  - `write_id3_tags` genre split regex now includes `\\`:
    `re.split(r"[,;/\\]+", …)` — three TCON values, not one literal string.
  - `write_flac_tags` splits a string `genre` value on the same separators
    into multiple Vorbis `GENRE` values.
- **`routes/track_routes.py::api_track_update_metadata`** — normalises a
  genres payload (list or delimited string) to a clean comma-joined string
  before storing.
- **`routes/ui_routes.py`**:
  - `track_detail` POST — normalises the form's genres value the same way.
  - `album_detail` POST — splits the album genres on all separators and
    stores/writes the clean list.
- **`services/popularity/stages/track_stage.py::_album_top_genres`** —
  plain-text genre columns split on `[,;/\\]+` (navidrome_genres etc.).

## Tests

`tests/test_genre_backslash_splitting.py`:
- the ID3 split regex turns `metal\nu metal\rock` into three parts;
- `write_flac_tags` writes three Vorbis `GENRE` values;
- `_album_top_genres` splits a backslash `navidrome_genres` column into
  three genres (and never a literal backslash string);
- the track-update API normalisation produces `metal, nu metal, rock`.
