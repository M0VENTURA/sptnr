# Manual Soulseek selection now links to the queue item

Date: 2026-08-20

## Symptom

When a manual Soulseek search was opened from a queue item (or a track row
with a queue id) and a result file was selected, the download started on
slskd but the song was **never matched to the queue item** — it stayed
`queued` instead of flipping to `downloading`, and the downloaded file
landed on disk orphaned (the completion service had no `found_filename` to
match back).

## Root cause

`POST /api/slskd/queue-download` only called slskd's enqueue — it never
updated the `download_queue` row.  The automatic pipeline
(`download_pipeline_service`) sets `found_filename` + `status='downloading'`
on the row so `check_completed_downloads` can reconcile the file; the manual
route skipped that step entirely.  Additionally, `slskd_username` /
`slskd_transfer_id` / `is_manual_download` were missing from
`UPDATE_ALLOWED_COLUMNS`, so even a well-intentioned update would have
silently dropped those columns.

## Fixes

1. **`routes/download_search_routes.py::slskd_queue_download`** — after
   enqueueing, it now:
   - Uses `SlskdService.download_file` (the retrying wrapper) with a fallback
     to the raw enqueue.
   - Links the row: `found_filename` (backslash-normalised), `slskd_username`,
     `status='downloading'`, `is_manual_download=True`.
   - Logs the manual link to `queue.log`.
2. **`db/repositories/queue.py`** — added `slskd_username`, `slskd_transfer_id`
   and `is_manual_download` to `UPDATE_ALLOWED_COLUMNS` so the linkage
   persists.

Now the completion service (`check_completed_downloads`) finds the row by
`status='downloading'` + `found_filename`, matches the landed file, moves it
to the library and promotes the row to `imported` — exactly like an
automatic pipeline download.

## Tests

`tests/test_manual_slskd_search_contract.py` — new `TestQueueDownloadLinking`:
- `UPDATE_ALLOWED_COLUMNS` includes the slskd linkage fields;
- backslash filenames are stored forward-slash normalised;
- the written payload matches the completion contract
  (`status='downloading'` + `found_filename` + `slskd_username`).
