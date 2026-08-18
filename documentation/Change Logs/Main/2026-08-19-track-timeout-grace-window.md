# Per-track collection deadline: grace window + configurable timeout

## Symptom

Slow-but-legitimate tracks were silently dropped from scoring during
popularity scans of well-documented artists:

```
WARNING scan_runner.702  Track result collection failed for 'A Perfect Circle - Eat the Elephant': 4 (of 12) futures unfinished
INFO    TRACK_STAGE.702  Track timed out or failed for 'A Perfect Circle - Eat the Elephant' — skipping (4 (of 12) futures unfinished)
```

Eat the Elephant: 10 of 12 tracks scored, 2 dropped.  eMOTIVe: 8 of 12
futures unfinished at the deadline.  The per-track pipeline on a
well-documented artist spans multiple rate-limited providers (MusicBrainz +
Discogs + Last.fm + ListenBrainz), so a 12-track album with 4 worker threads
can legitimately take >300s for the slowest track.

## Root cause

`services/popularity/scan_stage_runner.py` hardcoded a **300s album-wide
collection deadline** (`as_completed(timeout=300)`) on the per-track thread
pool.  When the deadline expired, the collector marked every still-running
future `None` and the album finalised without those tracks — and because the
worker threads kept running in the background (the pool is
`shutdown(wait=False)`), their deferred sink writes happened AFTER the
album's flush, so their scores were never persisted either.

## Fix

1. **Configurable deadline** — `popularity.track_timeout_seconds` (default
   **600**, clamped 120-1800) via the new
   `helpers/config_helpers.get_track_timeout_seconds()`.
2. **Two-phase collect** — the album waits the main deadline, then a bounded
   **60s grace window** for workers that finish just past it (rate-limited
   lookups landing a few seconds late are preserved, not dropped).
3. **Second sink drain** — after the grace window, the deferred-persist sink
   is drained again so late-finishing workers' writes are flushed (their
   scores are never lost).
4. **Dropped-track visibility** — when workers are still running after both
   phases, the warning + unified log now list the actual track titles that
   were skipped (not just the album + count).
5. The singles-only skip-pass collector uses the same deadline + grace
   window.

## Config

```yaml
popularity:
  track_timeout_seconds: 600  # per-album per-track collection deadline (default 600, 120-1800)
  scan_threads: 4             # concurrent per-track workers (default 4, 1-8)
```

Surfaced on the Config page (Popularity Weights → Scan Parallelism &
Timeouts) and saved via `static/js/config.js`.

## Tests

`tests/test_track_timeout_config.py`:
- default 600, custom value, clamping (120 floor / 1800 ceiling), zero and
  missing-section fall back to default.
