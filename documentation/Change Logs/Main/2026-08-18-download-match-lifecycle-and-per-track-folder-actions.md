# Download match lifecycle fixes + Matched-Folders per-track actions

## What changed

Fixes for files auto-moving into /music without a confident queue match,
plus per-track match/delete in Matched Folders and progressive word-drop
search fallbacks.

## Fix 1 — discovered files no longer enter the active queue

### Symptom

Files that arrived via another system (torrent / manual drop — NOT the
Soulseek queue) were being re-searched on Soulseek, downloaded again and
moved into /music automatically.

### Root cause

`db/repositories/queue.py::insert_queue_item` **hardcoded `status='queued'`**
in the INSERT, ignoring the `status="unmatched"` passed by
`insert_discovered_file`.  The queue processor's `get_ready_for_processing`
picks up every `queued` row with no `file_path` and **no source filter**, so
a discovered file (source='discovered') entered the active queue, got
re-searched/downloaded and auto-moved.

### Fix

- `insert_queue_item` honors the caller's `status` kwarg, and forces
  `'unmatched'` for `local`/`discovered` sources.
- `get_ready_for_processing` excludes `local`/`discovered` sources
  (belt-and-suspenders, matching the `get_active_queue` boundary).
- `check_completed_downloads` skips any `local`/`discovered` row even if it
  somehow reached `downloading` — passive disk states are never auto-moved.

## Fix 2 — auto-move requires a confirmed match

The fuzzy auto-move path previously accepted a file when metadata was
undetermined AND the artist gate was undetermined (bare filename score ≥
0.45).  Now a file with undetermined metadata AND unconfirmed artist is left
in Matched Folders for manual approval — only a confirmed metadata match or
confirmed artist match auto-moves.

## Fix 3 — per-track match/delete in Matched Folders

`services/downloads/download_folder_service.py`:
- `get_folder_tracks(folder_path)` — per-audio-file artist/album/title/size/
  imported state.
- `delete_folder_track(folder_path, file_name)` — delete ONE file
  (safety-railed; refuses files already imported).
- `move_folder_track_to_library(folder_path, file_name)` — move ONE file
  into the library using its embedded metadata.

Routes: `GET /api/downloads/folder/<path>/tracks`,
`POST .../track/delete`, `POST .../track/move`.

Frontend (`static/js/monitor.js`): each file in a Matched-Folders folder now
shows per-track [move] / [delete] buttons (imported files show an
"imported" badge instead).

## Fix 4 — progressive word-drop search fallbacks

`services/downloads/download_pipeline_service.py::_build_fallback_search_queries`
now drops each artist word and each title word one at a time (then paired
drops), covering peers who split multi-word names differently:

```
Avenged Sevenfold - It's Not Easy
→ Avenged - It's Not Easy
→ Sevenfold - It's Not Easy
→ Avenged Sevenfold - Not Easy
→ Avenged Sevenfold - Easy
→ Avenged - Not Easy / Sevenfold - Easy / ...
```

## Fix 5 — move → verify → delete-source → requeue lifecycle (confirmed)

The lifecycle already existed: `check_completed_downloads` matches by
embedded metadata (artist/album-artist, title, duration ±2s), `_move_and_import`
moves via `shutil.move` (source deleted from /downloads), verifies the target
in /music, and `mark_failed` requeues wrong downloads with backoff.  The
fixes above make the "match" gate strict enough to trust it.

## Files

- `db/repositories/queue.py` — insert status honored + ready-items source
  filter.
- `services/downloads/download_completion_service.py` — passive-source skip
  + strict fuzzy-match gate.
- `services/downloads/download_pipeline_service.py` — word-drop fallbacks.
- `services/downloads/download_folder_service.py` — per-track functions +
  per-file imported flag in unmatched-folders.
- `routes/downloads.py` — per-track endpoints.
- `static/js/monitor.js` — per-track buttons.
- Tests: `tests/test_download_match_lifecycle.py`.
