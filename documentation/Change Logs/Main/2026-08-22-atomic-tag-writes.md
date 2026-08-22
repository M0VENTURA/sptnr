# Atomic audio-file tag writes

## Why

Mutagen rewrites the audio file **in place**.  If a browser/player is
streaming the file, or the process is killed mid-write, the original can be
left partially written / corrupt.  The audit flagged this as the main
file-integrity risk.

## Fix

`services/metadata/tag_file_service.py`:

- `write_tags_to_file` now routes MP3/FLAC writes through a new
  `_write_tags_atomic(file_path, tags)`:
  1. Create a temp sibling in the SAME directory (`.populartag_<rand>.mp3/.flac`).
  2. `shutil.copy2` the original bytes onto the temp file.
  3. Run the existing mutagen writer against the TEMP file.
  4. Preserve the original's permissions on the replacement.
  5. `os.replace(temp, original)` — atomic on POSIX and Windows (same
     filesystem), so a reader sees either the old file or the new one,
     never a torn write.
  6. On ANY failure the original is left byte-for-byte untouched and the
     temp is cleaned up.

The low-level `write_id3_tags` / `write_flac_tags` are unchanged (they just
receive the temp path), so their tests and the genre-splitting behaviour are
unaffected.  `preserve_file_timestamps` still restores mtime after the swap.

## Files

- `services/metadata/tag_file_service.py`
- `tests/test_metadata_fanout_to_files.py` (new `TestAtomicTagWrites`)

## Tests

`TestAtomicTagWrites` covers: a successful write lands on the original path
and leaves no `.populartag_*` temp sibling; a failed writer leaves the
original bytes untouched and cleans the temp; an unsupported extension
returns False without touching anything.
