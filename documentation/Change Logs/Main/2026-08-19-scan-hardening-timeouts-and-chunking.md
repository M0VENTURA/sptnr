# Scan pipeline hardening: timeout enforcement, chunked concurrency, bigger budgets

## Symptom

A 17-track album's per-track batch hung: every future stalled until the
master 240-second failsafe killed the whole batch ("N (of N) futures
unfinished").  When an entire batch of concurrent tasks hangs with zero
completions, the cause is almost always:

1. **Rate-limit retry storms** — submitting every track at once slams
   Last.fm / ListenBrainz / Discogs / MusicBrainz.  A 429 re-arms the shared
   session's retry backoff (up to 60s per wait × 3 retries = 180s of pure
   sleeping in ONE worker), which eats the entire per-album budget.
2. **A too-tight master budget** — the per-artist prefetch and post-singles
   enrichment ran under a hardcoded 240s `_bounded_call`; heavy catalogs
   (dozens of live / compilation / re-release cross-release LB tallies)
   need more room to breathe.

## Fix 1 — Chunked concurrency (no more full-album bursts)

`services/popularity/scan_stage_runner.py` — the per-track pool now gates
each worker with a `threading.BoundedSemaphore` capped at
`min(scan_threads, 5)`.  All futures are still submitted (so `as_completed`
collects them), but at most 5 actually RUN at once; the rest block inside
the worker until a slot frees.  A 17-track album now drains in chunks of 5
instead of slamming the APIs with 17 concurrent requests.

## Fix 2 — Cumulative retry-wait budget (kills the 429 storm)

`api_clients/http_utils.py` — the shared session's `_RetryTransport` now
tracks a cumulative retry-wait budget (`_TOTAL_RETRY_WAIT_BUDGET = 40s`).
Once the sum of all backoff/Retry-After waits exceeds 40s, the request stops
retrying and returns the last retryable response.  A sustained 429 storm can
no longer sleep a worker for 180s; the request gives up and the track moves
on.

## Fix 3 — Configurable, larger budgets

- `helpers/config_helpers.py` — new `get_prefetch_budget_seconds()`:
  `popularity.prefetch_budget_seconds` (default **360**, clamped 120-1800).
- `services/popularity/scan_stage_runner.py` — both `_bounded_call` sites
  (per-artist prefetch + post-singles enrichment) use the configurable
  budget instead of the hardcoded 240s.
- Config page (Popularity Weights → Scan Parallelism & Timeouts) + `config.js`
  surface the new key.

## Config

```yaml
popularity:
  prefetch_budget_seconds: 360  # per-artist prefetch / post-singles budget (default 360, 120-1800)
  scan_threads: 4               # pool size; the per-track pool chunks to at most 5 concurrent
```

## Tests

`tests/test_track_timeout_config.py` — `TestGetPrefetchBudgetSeconds`
(default 360, custom, clamping, zero-fallback).

## Notes

Per-call HTTP timeouts were already enforced (Last.fm 10s, ListenBrainz
15-20s, shared session 30s default) — the hang was the retry WAIT, not the
transport timeout.  The cumulative-wait budget caps the wait; the chunked
semaphore prevents the burst that triggers the storm in the first place.
