# Soulseek Windows-backslash path sanitisation + conversion original-handling fix

Date: 2026-08-20

## Symptom

Every download from a Windows Soulseek peer deadlocked the queue cycle:

> `Voice of Baceprot - School Revolution... downloading from VacuumCollapse
> (nicotine\Voice of Baceprot - What's The Holy (Nobel) Today.flac)`
> … `Another queue cycle holds the lock — skipping this batch`

Root cause: Soulseek peers on Windows share paths with **backslash**
separators (`nicotine\file.flac`).  The container runs Linux, where `\` is
NOT a path separator — `os.path.basename()` returned the whole string,
`os.path.isfile()` rejected it, so the completion service could never locate
the file.  The item stayed `downloading` forever and the orchestrator thread
deadlocked on its own lock.

A second bug surfaced during FLAC→MP3 conversion:

> `Conversion succeeded but original handling failed... No such file or
> directory`

ffmpeg succeeded (it resolved the source), but the original-FLAC archive
move used the literal backslashed path string, which does not exist on
Linux.

## Fixes

1. **`download_pipeline_service.py`** — the remote filename is normalised
   (`\` → `/`) BEFORE it is stored as the queue's `found_filename`, so
   every later consumer sees a Linux-friendly path.
2. **`download_completion_service.py`**:
   - `_wait_for_transfer_file` normalises `found_filename` before
     `os.path.basename`, and normalises the slskd `localFilePath`.
   - `check_completed_downloads` normalises slskd `localFilePath` /
     `filename` before the `os.path.isfile` gate and before selecting
     `abs_path` (the slskd-completed path may still carry backslashes).
   - The per-item matching loop normalises the stored `found_filename` and
     any `abs_path` before filesystem checks.
3. **`download_organize_helpers.py`** — `move_track_to_library` and
   `_convert_flac_and_handle_original` normalise the source path before
   `os.path.splitext` / ffmpeg / `os.remove` / `shutil.move`, so the
   original-FLAC archive/delete step finds the file ffmpeg just read.
4. **`download_processing_service.py::queue_delete`** — normalises
   `found_filename` before `os.path.isfile`/`os.remove`.
5. **`db/repositories/queue_admin.py`** — the orphan-file sweep compares
   against the forward-slash-normalised basename of `found_filename`.

## Tests

`tests/test_slskd_backslash_sanitisation.py`:
- pipeline stores backslash-normalised `found_filename`.
- `_wait_for_transfer_file` extracts the real basename from a backslashed
  remote filename and normalises the slskd `localFilePath`.
- `check_completed_downloads` normalises slskd `localFilePath` before the
  file check.
- `move_track_to_library` / `_convert_flac_and_handle_original` normalise
  the source path before ffmpeg / the archive move.
