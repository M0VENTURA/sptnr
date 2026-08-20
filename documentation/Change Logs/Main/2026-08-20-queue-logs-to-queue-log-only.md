# Queue logs go to queue.log only — not info.log / debug.log

## Symptom

Queue activity polluted the general app logs.  `[QUEUE] Another queue cycle
holds the lock`, `[SLSKD] Cleared stale search`, `[ORGANIZE_GROUP] ...`,
`[RETRY_SCHEDULER] ...` and similar lines appeared in `info.log` and
`debug.log`, making them hard to read and mixing download-queue internals
into the app's general logging.

## Root cause

The queue lifecycle code (`services/queue/*` plus the queue-lifecycle
`services/downloads/*` modules) logs through its **module logger**
(`logging.getLogger(__name__)`), which propagates to the ROOT logger and
therefore to `unified_scan.log`, `info.log` and `debug.log`.  Only
`log_queue()` (used by `queue_diagnostics_service.log_queue_event`) routed
to `queue.log` — the module-logger lines were the noise.

## Fix

`helpers/logging_config.py` — added dedicated logger entries that route the
queue namespaces to `queue_file` (+ `error_file`) with `propagate: False`:

- `services.queue` (whole namespace: orchestrator, worker, processing,
  scoring, matching, diagnostics, lock, signal, finalizer, migration,
  cleanup, config)
- `services.downloads.download_completion_service`
- `services.downloads.download_pipeline_service`
- `services.downloads.download_processing_service`
- `services.downloads.download_queue_normalizer`
- `services.downloads.download_queue_service`
- `services.downloads.download_retry_service`
- `services.downloads.slskd_service`
- `services.downloads.download_organize_helpers`
- `services.downloads.download_verification_service`

Queue INFO/DEBUG/WARNING now lands ONLY in `queue.log`; ERROR records also
reach `error.log` (the `error_file` handler filters at ERROR).  None
propagate to the root, so `unified_scan.log`, `info.log` and `debug.log`
stay clean of queue noise.

Also de-duplicated the queue modules that logged BOTH a module-logger line
AND a `log_queue()` line for the same event (`download_queue_normalizer`,
`download_retry_service`, `download_pipeline_service.start_release_download`)
— the module logger now handles the queue.log write, so the explicit
`log_queue()` twin was removed to avoid double lines in queue.log.

## Files

- `helpers/logging_config.py` — queue namespace loggers (propagate=False).
- `services/downloads/download_queue_normalizer.py` — removed `log_queue`
  duplicates.
- `services/downloads/download_retry_service.py` — removed `log_queue`
  duplicate.
- `services/downloads/download_pipeline_service.py` — consolidated
  `start_release_download` logging to one queue-log line.
