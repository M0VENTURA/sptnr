# Save paths write metadata to BOTH files and DB (2026-08-30)

## Confirmed: file-tag writes on every save surface

Audited all save paths across the Track / Album / Artist pages.  The
following already write the audio file tags (FLAC/MP3) in addition to the
database — Navidrome picks up the changes on its next scan:

- **Track page edit modal** → `/api/track/update-metadata` with
  `sync_to_file: true` → `sync_track_tags_to_file()` → `write_tags_to_file()`.
- **Album page per-track "Edit Metadata" modal** → same `/api/track/
  update-metadata` + `sync_to_file: true`.
- **Artist page per-track edit modal** → same endpoint + `sync_to_file: true`.
- **Album page Save (whole-album edit)** → `build_tag_updates(payload)` +
  `update_file_tags()` per track, plus cover-art download/embed when a
  `cover_art_url` is present.
- **Album "Use This Album" / Apply MBID** → `apply_mbid_to_album` fans the MB
  album/release-group IDs + cover art out to every track file.
- **Album "Update All Tracks" from MB compare** → per-track
  `/api/track/update-metadata` (title/track/year/mbid/disc + composer/
  lyricist/writer/genres/cover/work) + album-level fields + cover embed.
- **Artist "Apply Genres"** → `apply_genres` writes `genres` to DB + file tags.
- **Artist corrections "Apply Album MBID"** → `apply_album_mbid` writes MB
  album ID + release-group ID to DB + file tags.
- **Artist corrections "Clear Disc Number"** → now clears the disc frame in
  the file tags too (see fix below).
- **Artist corrections "Merge Albums"** → now rewrites the album tag in the
  file tags (see fix below).

## Gaps fixed

Two artist-correction paths updated the DB but NOT the audio file tags:

- `db/repositories/metadata.py::merge_album_names` (Merge Albums) — now also
  rewrites the `album` tag on every affected audio file.
- `db/repositories/metadata.py::clear_album_disc_numbers` (Clear Disc Number)
  — now also clears the `disc_number` frame in the file tags.

## Notes

- ID-only writes that Navidrome does not read from file tags (Discogs album
  ID, `is_single`/`stars` toggles) remain DB-only by design — Navidrome
  serves star ratings via Subsonic, not tags.
- The auto-triggered remote Navidrome scan after tag writes was removed in
  the previous change; the single sync-and-wait now runs before the full
  Navidrome import.

## Files

- `db/repositories/metadata.py` — merge-albums + clear-disc file-tag sync
