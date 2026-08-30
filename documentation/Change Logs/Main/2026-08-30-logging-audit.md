# Logging audit — locations + coverage (2026-08-30)

## Log file map (all under the configured log dir, default `/config`)

| File | Source | Writes |
|---|---|---|
| `unified_scan.log` | `log_unified()` → `popularr.unified` logger + root structlog via bridge | Scan pipeline (popularity/singles/metadata), Navidrome import, MP3 import, artist/album scans, Essentia, `[QUEUE]`/`[AUTOMATIC]` unified lines |
| `info.log` | root logger, INFO+ (structlog bridge) | All structured service logs at INFO |
| `debug.log` | root logger, DEBUG+ | Full DEBUG detail (enabled via config level) |
| `error.log` | root logger, ERROR+ + `services.queue`/`services.downloads` loggers | All errors/exceptions across the app |
| `queue.log` | `log_queue()` → `popularr.queue` + `services.queue`/`services.downloads`/`db.repositories.queue*` loggers | Download-queue events (`[QUEUE]`), download lifecycle, slskd transfers, `[SEARCHING]`/`[DOWNLOADING]`/`[IMPORTED]`/`[FAILED]` via `log_queue_event` |
| `search.log` | `log_search()` → `popularr.search` | Soulseek search events (`[AUTOMATIC]`/`[MANUAL]` → N results in Xs) |
| `access.log` | hypercorn `--access-logfile` (entrypoint) | HTTP access lines |
| `client.log` | `append_client_log()` (main.js alert→toast shim) | Converted UI alerts |
| `queue_processor.log` | entrypoint stdout redirect of the queue worker process | Queue worker process output |

## Coverage confirmed

- **Queue events** → `queue.log` (via `log_queue_event` → `log_queue`) AND the
  in-memory store served at `/api/queue/events`.  Every state transition
  (searching → downloading → imported/failed/manual_review) is logged.
- **Soulseek searches** → `search.log` (automatic via `_log_search_event` +
  manual via `_log_manual_search_event`), plus `[QUEUE]` lines in
  unified_scan.log.
- **Errors** everywhere → `error.log` via structlog `logger.error/exception`
  (routes, services, clients all use it — verified across musicbrainz_routes,
  track_routes, completion, pipeline, etc.).
- **Scan activity** → `unified_scan.log` (`log_unified` used heavily across
  scan_stage_runner, finalise_stage, load_stage, track_stage, pipelines,
  navidrome_import, mp3_import, essentia).
- **File-tag / cover / MB writes** log at DEBUG (tag_file_service) and INFO
  for successes (album_service, musicbrainz_service).

## Gap fixed

`/logs` page (`routes/ui_routes.py` `_USED_LOG_FILES`) now also lists
**`client.log`** and **`queue_processor.log`** — both are written to the log
dir but were previously not selectable in the Logs UI.

## Notes

- The queue page's live event feed is in-memory (lost on restart); the
  persistent history lives in `queue.log` on the /logs page.
- The dashboard's scanning panel filters `unified_scan.log` to scan activity
  only; queue/search lines belong to their dedicated files.

## Files

- `routes/ui_routes.py` — /logs page now exposes client.log + queue_processor.log
