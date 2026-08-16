# Scheduler: stop the "Job already exists in jobstore" warning at every boot

## What changed

Every hypercorn worker (plus the entrypoint's `from app import app` preflight
import) boots its own APScheduler against the SAME PostgreSQL-backed job store.
At the start of every scan / container boot the logs showed:

```
[WARNING] [scheduler] Job popularity_scan already exists in jobstore
(concurrent worker race); applying as update: 'Job identifier
(popularity_scan) conflicts with an existing job'
```

Root cause: `_register_default_jobs` checked for an existing job via
`scheduler.get_job(job_id)` — but while the scheduler is **stopped** (which is
the case during registration, before `scheduler.start()`), APScheduler's
`get_job` only inspects in-memory *pending* jobs and is blind to the persisted
DB job store. Every worker therefore "saw" the default jobs as missing and
re-registered them. On `start()` the pending jobs were flushed to the DB, and
all but the first worker hit the `apscheduler_jobs_pkey` unique constraint.

Fixes:

1. **Store-aware existence check.** `_put` and `_remove_job` now use a new
   `_existing_job` helper that first consults `scheduler.get_job` and, when the
   scheduler is stopped, falls back to querying each configured job store
   (`store.lookup_job`). A job already persisted by a previous boot / sibling
   worker is now detected and skipped — no duplicate INSERT, no warning.

2. **Persisted removal.** When a job's trigger/callable changes (or a job is
   disabled in config), `_remove_persisted_job` now also drops the DB row.
   `scheduler.remove_job` only touches pending jobs while stopped, so the
   old row would otherwise linger and re-trip the duplicate-key race on the
   next registration.

3. **Log level.** The remaining duplicate-key fallback (a genuine simultaneous
   boot race that still resolves as an upsert) is downgraded from WARNING to
   INFO, since it is expected, handled behaviour in multi-worker deployments.

## Files touched

- `services/scheduler/scheduler_service.py`
- `tests/test_scheduler_jobstore_race.py`
