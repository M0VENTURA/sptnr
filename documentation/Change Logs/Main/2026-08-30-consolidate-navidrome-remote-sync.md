# Remove frequent remote Navidrome syncs; sync once before full import (2026-08-30)

## Symptom

Remote Navidrome library scans were being triggered VERY often (after every
audio-file tag write / genre apply / genre remove / album MBID fan-out),
pausing the Navidrome server and locking the database.

## Root cause

Three call sites fired `NavidromeClient.start_scan()` in a daemon thread after
every metadata/tag mutation:
- `routes/track_routes.py::_trigger_navidrome_scan` (after every
  `/api/track/update-metadata` with `sync_to_file`),
- `routes/misc_routes.py::_trigger_scan_after_tag_write` (album genre writes),
- `routes/misc_routes.py` inline `_trigger_scan` threads (genre apply/remove).

Each `startScan` starts a full Navidrome library scan, so repeated tag edits
queued scan after scan — hammering the server.

## Fix

**Removed the automatic triggers** — `_trigger_navidrome_scan` and
`_trigger_scan_after_tag_write` are now no-ops (return True without firing);
the two inline genre-apply/remove threads no longer call `start_scan`.
Unused `NavidromeClient` imports dropped.

**Added ONE consolidated sync-and-wait** (`api_clients/navidrome.py`):
`NavidromeClient.trigger_and_wait_for_scan(...)` —
1. Calls `startScan` (GET `/rest/startScan.view`).
2. Polls `getScanStatus` (GET `/rest/getScanStatus.view`) every
   `poll_interval_seconds` (default 5s), up to `max_wait_seconds` (default
   30 min), until `scanning` is False.
3. Returns True when the remote scan completed (or was already idle), False
   on timeout / API failure — so callers never spin forever.

**Wired into the full Navidrome import** (`services/scanning/pipelines/
navidrome_pipeline.py::run_navidrome_import_scan`): BEFORE the marker check /
artist-index build, the import triggers the remote Navidrome scan and WAITS
for it to finish — so Navidrome has fully ingested the freshly-written
MusicBrainz tags / new files before the app reads them back for the local
import.

The **manual** endpoint (`POST /api/navidrome/scan/start`) is kept (user-
initiated) and now also uses `trigger_and_wait_for_scan`.

## Files

- `api_clients/navidrome.py` — `trigger_and_wait_for_scan()`
- `services/scanning/pipelines/navidrome_pipeline.py` — sync+wait before import
- `routes/track_routes.py` — auto-trigger removed
- `routes/misc_routes.py` — auto-triggers removed
- `routes/navidrome/scan.py` — manual endpoint uses sync+wait
- `tests/test_navidrome_remote_sync_consolidation.py` (new)
