# Fix stale stop flags halting scans (2026-08-31)

## Symptom

1. "When resuming, it will only do the current artist and stop when running
   from the dashboard."
2. "When pressing stop to shut down the scan, any scan that tries to run
   after is immediately stopped again."

## Root cause

A Stop click writes ``scan_states.stop_requested=True`` for every scan type
(`/scan/stop-all`).  When a NEW scan started, nothing cleared that flag for
the scan type it was about to run:

- The dashboard **"All" scan** (`_run_full_scan_as_artist_pipeline`) checks
  `is_stop_requested(full_scan)` at the top of EVERY artist iteration, but
  `full_scan`'s stop flag was never cleared at scan start (only
  `popularity_scan` was cleared inside `run_popularity_scan`).
- Result: after a Stop, the next "All" scan resumed from the checkpoint
  (the last artist), processed exactly ONE artist (the resume point), then
  hit the still-true `full_scan` stop flag and halted — the exact
  "resume only does the current artist and stops" symptom.  And EVERY scan
  after a Stop was immediately stopped again.
- The same stale-flag issue applied to the **library scan**
  (`run_full_library_scan` checks `is_stop_requested(library)`) and the
  **mp3 import**.

## Fix

Clear stale stop flags at the START of every scan (defense-in-depth):

- `services/scanning/pipelines/popularity_pipeline.py` —
  `_run_full_scan_as_artist_pipeline` clears `full_scan`'s stop flag before
  iterating artists (the primary fix for the dashboard "All" scan).
- `routes/scan_routes/api.py` — the `/api/popularity/run` worker clears
  `full_scan` / `popularity_scan` / `singles_scan` /
  `metadata_lookup_scan` stop flags before launching, so a Stop never
  leaks into the next popularity-family scan.
- `services/scanning/pipeline.py` — `run_full_library_scan` clears the
  `library` stop flag before iterating.
- `services/scanning/pipelines/mp3_import_pipeline.py` — clears the
  `mp3_import` stop flag.

Navidrome + Essentia pipelines already cleared their own flags.

## Files

- `services/scanning/pipelines/popularity_pipeline.py`
- `routes/scan_routes/api.py`
- `services/scanning/pipeline.py`
- `services/scanning/pipelines/mp3_import_pipeline.py`
- `tests/test_scan_stale_stop_flag.py` (new)
