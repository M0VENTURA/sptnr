# Popularity scan: startup crash fix + 8-hour stall cascade fixes

## What changed

Two distinct production failures on the popularity scan are fixed:

### 1. Letter/artist-page scan crash (scan never started)

`POST /api/scan/from-artist` (the artist-page "scan from letter" flow) passed
the progress dictionary **positionally** into
`write_progress_with_current_artist(...)`'s `current_artist` parameter. The
dict landed on the `scan_states.current_artist` VARCHAR column and psycopg2
raised `ProgrammingError: can't adapt type 'dict'` — the scan thread never
started and the dashboard showed nothing.

- `routes/scan_routes/popularity.py` — the caller now passes the dict via
  `extra=` and `current_artist=artist` (the keyword form all other callers
  use).
- `services/scanning/scan_state.py` — `write_progress_with_current_artist`
  now **defensively coerces** any non-string `current_artist` (dict/list)
  into a display string, so no future caller can re-introduce the crash.

### 2. The 8-hour "N of N futures unfinished" stall cascade

Starting at the 10th album, every album's per-track workers timed out (all
tracks dropped) and the scan ground on for 8 hours. Root causes:

- **Rate-limiter lock-while-sleep**: `APIRateLimiter.throttle_musicbrainz /
  throttle_lastfm / throttle_listenbrainz` slept for the inter-request
  interval **while holding the provider lock**. Four concurrent scan workers
  therefore serialised on the same provider — a 1 req/s budget became
  "each worker waits for every other worker's sleep", and no album could
  finish within its 600s deadline. The sleep now happens **outside the lock**
  (the slot is claimed atomically under the lock; concurrent workers sleep in
  parallel). `services/infrastructure/api_rate_limiter.py`.
- **No per-track wall-clock timeout**: a stuck track (rate-limited to death)
  held its semaphore slot for the whole album budget, starving the other
  tracks. Each track worker now has a hard wall-clock cap
  (`min(track_timeout, 300s)`), after which it is abandoned and the slot
  frees. `services/popularity/scan_stage_runner.py`.
- **Post-singles / cover resource-exhaustion guard**: once an album's track
  workers were badly starved (>=50% failed/stalled), the follow-up serial
  enrichment + cover passes only deepened the contention. They now skip when
  the album's track failure ratio is >= 0.5, letting the next album's workers
  get the rate-limit budget.
- **Album-stall heartbeat**: the album collector now emits a per-minute log
  line listing the in-flight tracks while waiting, so a stalled album is
  diagnosable in real time instead of after the full deadline.
- **Queue processor no longer blocks the web worker**: the APScheduler
  `download_queue_processor` tick ran `process_cycle` inline, so a long
  maintenance pass (filesystem walk + metadata reads + slskd polling) blocked
  the hypercorn worker's event loop and starved the scan's own threads. The
  tick now spawns a daemon thread (`services/scheduler/scheduler_service.py`).

## Files

- `services/scanning/scan_state.py`
- `routes/scan_routes/popularity.py`
- `services/infrastructure/api_rate_limiter.py`
- `services/popularity/scan_stage_runner.py`
- `services/scheduler/scheduler_service.py`
- `tests/test_scan_stall_fixes.py` (new)

## Tests

`tests/test_scan_stall_fixes.py` covers: dict-coercion in
`write_progress_with_current_artist`, route keyword-form usage, rate-limiter
lock-free-during-sleep for all three providers, the bounded track worker
wiring, the resource-exhaustion guard, and the non-blocking scheduler tick.
