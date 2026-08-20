# /Original archive isolation + completed-download re-download guard

Date: 2026-08-20

## Symptom

"Even though the /Original folder is meant to be ignored by the queue
items, I think it's still being seen.  Either that, or completed downloads
even though are downloaded and removed from the download queue, may still
be in the database and trying to redownload them."

Two related defects let archived FLAC conversion originals re-enter the
pipeline and trigger duplicate downloads.

## Root causes

1. **Archive exclusion was top-level-only.** `discover_audio_files` and the
   completion walk pruned `downloads/<original_subfolder>` ONLY when it was
   a direct child of the downloads root (`os.path.normpath(join(root, d))
   == _archive_dir`).  When the archive preserved the relative path
   (`downloads/<album>/Original/...` — the conversion pipeline archives to
   `Original/<relative path>`), a nested `Original` folder was NOT pruned
   and its FLACs were re-discovered / re-matched as fresh downloads.
2. **`_get_files_in_folder` recursed into `Original`.** The monitor's folder
   listing walked every subfolder including a nested archive, surfacing
   archived FLACs in Matched Folders and blocking folder pruning
   (`auto_delete_imported_folders` saw them as not-imported).
3. **`_wait_for_transfer_file` did not know about the archive.** After a
   FLAC→MP3 conversion import, the original is moved to
   `downloads/<original_subfolder>/`.  slskd's `localFilePath` still points
   at the ORIGINAL download path (now empty), so the completion service
   declared "slskd transfer succeeded but local file not found", marked the
   item failed, and the retry scheduler RE-DOWNLOADED the already-imported
   album — the duplicate download loop.

## Fixes

- **`services/infrastructure/filesystem_service.py`**:
  - `_get_files_in_folder` prunes any directory named
    `<original_subfolder>` (default `Original`) at ANY depth.
  - New `_original_archive_subfolder_name()` + `archive_dir_path()` so
    every walker prunes the SAME archive the conversion writes to.
- **`services/downloads/download_scan_service.py`** —
  `discover_audio_files` prunes the archive by subfolder NAME at any depth
  (not just the top-level path).
- **`services/downloads/download_completion_service.py`**:
  - The completion walk prunes the archive by name at any depth.
  - `_wait_for_transfer_file` now ALSO searches the archive directory for
    the basename (walking the preserved relative path) — an archived
    original is proof the download was imported, so the item is not marked
    failed and never re-downloaded.

## Tests

`tests/test_original_archive_isolation.py`:
- `_get_files_in_folder` excludes a nested `Original` subfolder;
- `discover_audio_files` excludes the archive even when nested;
- `_wait_for_transfer_file` finds an archived original (no false "file not
  found" → no re-download) and returns None when nothing exists.
