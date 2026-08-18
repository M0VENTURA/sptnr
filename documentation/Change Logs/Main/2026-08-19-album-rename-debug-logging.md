# Album file rename: debug-log visibility

## Symptom

Renaming an album's files from the album page (Actions → Rename Files) ran
completely silently: the per-file moves, FLAC→MP3 conversions, DB path
updates, collisions, errors and the empty-dir cleanup existed ONLY in the
HTTP JSON response returned to the UI.  If the page was reloaded, the panel
dismissed, or a partial failure occurred, there was no trace in the logs to
diagnose what happened (e.g. files "missing" after a rename with conversion
enabled — the FLAC→MP3 converter deletes the original FLAC, so a later
failure in the same run can leave files that no longer exist under their old
paths).

## Fix

`services/metadata/album_service.py` — `rename_album_files_service` now logs
every step through the module logger (`logger`):

- DEBUG (goes to `debug.log` when `logging.level: DEBUG` is set):
  - run start (track count, format, conversion enabled)
  - per-file conversion (FLAC→MP3, original deleted by converter)
  - collision-resolution of an existing target
  - successful move (src → target)
  - DB `file_path` update
  - empty-dir cleanup
  - run summary (renamed / updated_db / errors)
- WARNING (always captured, including the unified/info logs):
  - "file not found on disk" (per-file)
  - FLAC→MP3 conversion failure
  - move failure
  - DB update failure
  - end-of-run error summary

The WARNING lines are captured even at the default INFO level, so a failed
rename is immediately visible in the Logs page; the DEBUG lines give the
full per-file audit trail when debug logging is enabled.

## Tests

No automated test (logging-only change) — verified via `get_errors` and
manual review of the log flow.

## Config

No new config keys.  To see the DEBUG audit trail, set
`logging.level: DEBUG` (Config page → Logging, or `LOG_LEVEL=DEBUG`).
