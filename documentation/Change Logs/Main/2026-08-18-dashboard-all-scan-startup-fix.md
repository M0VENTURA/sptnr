# Dashboard "All" scan startup fix (phantom completion)

## Symptom

Running a full scan from the dashboard "All" option went **straight to
"completed"**: the footer showed idle, recent scans showed "Full Scan
completed", and nothing appeared in either log as starting. The same scan
from a single artist page worked fine.

## Root cause

The dashboard "All" option posts `mode: "all"` to `/api/popularity/run`,
which routes to `_run_full_scan_as_artist_pipeline` (the per-artist full
pipeline). The route recorded a "started" `scan_history` row **BEFORE** the
duplicate-scan guards, then:

1. `is_popularity_scan_active()` checks the shared `scan_states` DB rows
   (`popularity_scan` + `full_scan`) for `is_running=True`. A **stale row**
   from a crashed scan (daemon thread died before its `finally`, or a
   previous process was killed mid-scan — `reset_stale_scan_states` only
   runs at startup) stays `is_running=True` forever.
2. The stale row made the guard return 409 — the worker **never started**.
3. The orphaned "started" record in `scan_history` was the only trace, and
   the dashboard's recent-scans panel renders any non-failed `_SCAN_SESSION_`
   group as "completed" → "Full Scan completed" with the footer idle and no
   worker logs.

The artist page works because it never consults the same stale row.

## Fix

`routes/scan_routes/api.py` — `api_popularity_run_compat`:

1. **Record "started" AFTER the duplicate guards** — a rejected start no
   longer leaves an orphaned "completed"-looking record.
2. **Self-heal stale scan-state rows**: when the cross-process guard trips,
   the route checks whether any LIVE worker owns the running row (via the
   in-process runtime registry + thread liveness). If no live owner exists,
   the stale `popularity_scan` / `full_scan` rows are cleared and the scan
   proceeds — a phantom "already running" can never permanently block the
   dashboard or the scheduled popularity job.
3. **Log worker exceptions** — a daemon-thread failure now surfaces in the
   unified log + app log + `scan_history` ("failed") instead of dying
   silently.

`services/scanning/pipelines/popularity_pipeline.py` — `_run_full_scan_as_artist_pipeline` now logs a start line ("[FULL_SCAN] Starting full scan — N artist(s) queued") and a diagnostic when the library has no artists, so a zero-artist run is visible instead of silent.

## Files

- `routes/scan_routes/api.py` — record ordering, stale-row self-heal,
  worker exception logging.
- `services/scanning/pipelines/popularity_pipeline.py` — full-scan start +
  empty-library log lines.
- Tests: `tests/test_full_scan_startup_fix.py` (stale row cleared + scan
  starts; live scan still rejected).
