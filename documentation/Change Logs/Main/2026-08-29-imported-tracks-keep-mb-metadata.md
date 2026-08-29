# Imported tracks keep MusicBrainz metadata (2026-08-29)

## Symptom

Downloads now copy from the downloads folder into the music folder correctly
(slskd directory misconfiguration fixed), but the imported tracks lost the
MusicBrainz metadata they were matched with — writer, cover flag / original
cover artist, MB genres, work MBID, album MBID did not appear on the library
track.

## Root cause

``_apply_stored_metadata`` (download completion) wrote the queue item's stored
MusicBrainz enrichment to the **file tags** only.  The **tracks table** row was
created later by the Navidrome import scan — and that scan's metadata
extractor never read ``is_cover`` / ``original_cover_artist`` /
``musicbrainz_genres`` back from the file tags, so those fields were dropped
from the DB row even though they were sitting in the file.  A track that was
already in the DB (a re-download) also never got the enrichment applied.

## Fix

`services/downloads/download_completion_service.py`:
- `_apply_stored_metadata` now ALSO persists the stored MB enrichment to the
  matching **tracks table row** (resolved by file path, then recording MBID,
  then artist+title) via `insert_or_update_track` — so the library shows
  writer / cover / genres / work MBID / album MBID immediately, no Navidrome
  rescan needed.  New `_resolve_track_id_for_import()` helper.
- Also propagates `musicbrainz_artistid`, `musicbrainz_albumartistid`,
  `musicbrainz_releasegroupid` and `work_mbid` (from the queue row's
  `metadata` JSONB) into both the file tags and the DB row.

`services/scanning/metadata_extractor.py`:
- `extract_track_metadata` now reads `is_cover`, `original_cover_artist`,
  and `musicbrainz_genres` back from the file tags (snake-case keys + the
  TXXX/Vorbis descriptions `IS_COVER`, `ORIGINAL COVER ARTIST`,
  `MUSICBRAINZ GENRES`), so a subsequent Navidrome import preserves them.

`services/scanning/payload_builder.py`:
- `EXTRACTED_STRING_FIELDS` gained `is_cover`, `original_cover_artist`,
  `musicbrainz_genres` so the extractor's output is persisted by
  `build_track_payload`.

Note: `musicbrainz_genres` is in `_POPULARITY_PROTECTED_COLUMNS`, so a
Navidrome sync (`_navidrome_sync=True`) intentionally does NOT overwrite it
(the popularity pipeline owns it) — the direct `_apply_stored_metadata` DB
write covers the import-time case.

## Files

- `services/downloads/download_completion_service.py`
- `services/scanning/metadata_extractor.py`
- `services/scanning/payload_builder.py`
- `tests/test_moving_recovery.py` (DB-persist + no-row-skip tests)
- `tests/test_metadata_extractor_mb_readback.py` (new)
