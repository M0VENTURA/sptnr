# Dashboard full-scan logs were invisible (phantom "nothing in logs")

## Symptom

Running a full scan from the dashboard "All" option showed **"Full Scan
(All) completed — 0s ago"** in Recent Scans while the unified log appeared
to have **nothing** — no start line, no artist lines, no finish line.  The
same scan from a single artist page worked and logged normally.

## Root cause

The dashboard "All" worker emits `[FULL_SCAN] ...` lines (start, per-artist
progress, per-artist completion, finish) and the per-artist pipeline emits
`[SCAN_PIPELINE] ...` lines.  Every read path for `unified_scan.log` — the
dashboard scanning panel, `/api/unified-log`, the /logs page, and SSE
streaming — filters lines through `_scan_activity_filter()`, and that
pattern did **not** match `[FULL_SCAN]` or `[SCAN_PIPELINE]`.  The worker
was running (and could be failing instantly, e.g. on an empty artist list),
but every line it wrote was silently dropped from every log view — hence
"nothing in logs" alongside a fresh Recent Scans entry.

The Recent Scans panel renders any non-failed `_SCAN_SESSION_` group as
"completed", so an instant (or silently-failing) full scan showed
"completed — 0s ago" with no visible activity.

## Fix

- `services/log_service.py` — `_scan_activity_filter()` now also matches
  `[FULL_SCAN]` and `[SCAN_PIPELINE]` (plus the new worker start/finish
  lines), so full-scan progress is visible in the dashboard scanning panel
  and the /logs unified view.
- `services/scanning/pipelines/popularity_pipeline.py` —
  `_run_full_scan_as_artist_pipeline` now:
  - wraps `get_all_artists()` in a try/except that logs
    `[FULL_SCAN] Failed to load artist list: …` and records a "failed"
    scan instead of silently completing;
  - logs `[FULL_SCAN] Artist i/N done:` / `FAILED:` around each artist so a
    silently-swallowed per-artist error is visible instead of the loop
    appearing to succeed instantly.
- `routes/scan_routes/api.py` — the popularity worker logs
  `[POPULARITY] Worker starting mode=…` and `[POPULARITY] Worker finished
  mode=…` so a worker that starts but does nothing is distinguishable from
  a worker that never launched.

## Files

- `services/log_service.py` — scan-activity filter includes full-scan lines.
- `services/scanning/pipelines/popularity_pipeline.py` — artist-list error
  handling + per-artist outcome logging.
- `routes/scan_routes/api.py` — worker start/finish diagnostics.
- `tests/test_scan_log_filter.py` — regression tests that the full-scan
  session lines pass the filter (and queue/watcher noise still does not).
