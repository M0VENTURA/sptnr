# Metadata fan-out: album/track edits now write to the audio files

Date: 2026-08-20

## Symptom

Updating metadata on the album or track pages saved to the **database** but
wasn't reliably written to the **MP3/FLAC files**.  The album edit page only
wrote `genres` to the files — title / artist / year / MusicBrainz IDs /
release-info edits went to the DB alone.  The track page worked (it resolved
the path and wrote tags), but the file sync was duplicated and drifted
across the four edit paths.

## Root causes

1. **Album page POST** (`routes/ui_routes.py::album_detail`) — only genres
   were written to files, and with a raw `os.path.exists(file_path)` check
   that misses stored paths (even absolute ones when the container's cwd
   differs); every other edited field was DB-only.
2. **Album-service paths** (`services/metadata/album_service.py`) —
   `apply_genres_to_album` and `bulk_tag_tracks` used the raw stored
   `file_path` + `os.path.exists()` instead of the shared resolver.
3. **`update_album_ids`** was DB-only — the Edit IDs modal never wrote the
   MBIDs to the audio files (violating the metadata fan-out rule).
4. **FLAC writer** (`services/metadata/tag_file_service.py::write_flac_tags`)
   wrote a genres *list* as the literal string `"['Rock', 'Metal']"` and
   lacked the standard Vorbis MBID field-name mapping (`MUSICBRAINZ_*`,
   `date`, `genre`).
5. **Duplicated column→tag mapping** — the track page had a private
   `build_tags_to_write` that drifted from the writer's expectations (e.g.
   it mapped genres→`genre` only, missing the FLAC/MBID aliases).

## Fixes

- **`tag_file_service.py`**:
  - `resolve_music_file_path` is now public (`resolve_music_file_path`
    alias) and is the SINGLE shared path resolver used by the album page,
    album service and track page.
  - New `build_tag_updates(payload)` — the shared DB-column → tag-writer
    mapper (album_artist → TPE2/albumartist, year → TDRC/date,
    musicbrainz_albumid → TXXX:MUSICBRAINZ ALBUM ID / MUSICBRAINZ_ALBUMID,
    writer → TXXX:WRITER, …).  One source of truth for every edit path.
  - `write_flac_tags` — genres lists become multiple Vorbis `GENRE` values;
    Vorbis field map extended (`genres→genre`, `year→date`, MBID aliases →
    standard `MUSICBRAINZ_*` names).
  - `write_id3_tags` — added a `writer` → `TXXX:WRITER` frame handler
    (clears stale WRITER/LYRICIST variants first).
- **`routes/ui_routes.py::album_detail`** — the album POST now writes EVERY
  changed, file-mappable field to each track's audio file via the shared
  mapper + resolver (previously DB-only apart from genres).
- **`routes/ui_routes.py::track_detail`** — the private `build_tags_to_write`
  now delegates to the shared `build_tag_updates`.
- **`album_service.py`**:
  - `apply_genres_to_album` / `bulk_tag_tracks` resolve paths via
    `resolve_music_file_path`.
  - `update_album_ids` writes the MusicBrainz release/release-group IDs to
    each track's file tags (metadata fan-out), returning `files_updated`.

## Config

No new config — the existing `tagging` toggles (`write_tags_to_file`,
`ratings_only`, `fill_missing_only`, `preserve_file_timestamps`) still gate
every write.

## Tests

`tests/test_metadata_fanout_to_files.py`:
- `build_tag_updates` maps album payload fields to the correct tag names.
- `write_flac_tags` writes list genres as multiple values + standard Vorbis
  MBID/date names.
- `apply_genres_to_album` resolves a relative path via the shared helper.
- `update_album_ids` writes MBIDs to the files (fan-out).
