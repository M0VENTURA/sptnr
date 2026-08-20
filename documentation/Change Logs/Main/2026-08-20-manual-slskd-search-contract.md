# Manual Soulseek search API contract restoration

Date: 2026-08-20

## Symptom

The manual Soulseek search modal threw **"Search did not return a search
ID."**  Every manual search failed before polling could start.

## Root cause

The `/api/slskd/search` route was migrated from `old_system/app.py` and the
response contract drifted:

1. **Start response** — the legacy route returned `{"searchId": ..., "status":
   "searching"}` (camelCase, the exact key `static/js/downloads.js` reads at
   `data.searchId`).  The migrated route returned `{"success": True,
   "search_id": ...}` (snake_case) — so `data.searchId` was always
   `undefined` and the modal threw.
2. **Poll response** — the legacy route returned
   `{results, state, responseCount, fileCount, isComplete}` with flattened
   per-file rows (username/filename/size/size_mb/bitrate/sample_rate/
   length/duration).  The migrated route returned only a flat `results`
   array — no `state`/`isComplete`/`responseCount` — so even if a search
   started, the poll never stopped and always showed "searching".
3. **Slot-busy** — the legacy route returned `{slotBusy, activeSearchId,
   activeSearchQuery, activeSearchState}` with HTTP 202 so the frontend
   showed the "slot busy — auto-retry" banner.  The migrated route had no
   slot-busy detection.

## Fixes (`routes/download_search_routes.py`)

- **POST /api/slskd/search**:
  - Checks the slskd search slot FIRST (`SlskdService.list_searches`) and
    returns the legacy `slotBusy` payload (HTTP 202) when a search is
    already in progress.
  - Returns `{"searchId": <id>, "status": "searching"}` on success.
  - Returns the legacy error text when the search cannot start.
- **GET /api/slskd/search/<id>**:
  - Uses `SlskdService.get_search_results` (rich `(responses, state,
    is_complete)`) instead of the flat client method.
  - Flattens `SearchResponse` objects into the legacy per-file row shape.
  - Returns `{results, state, responseCount, fileCount, isComplete}`.
  - Cleans up `_manual_search_state` when the search completes.

## Tests

`tests/test_manual_slskd_search_contract.py`:
- start response uses `searchId` + `status: "searching"`;
- busy slot returns `slotBusy` + active search details;
- poll flattening produces the legacy row shape with
  `state`/`responseCount`/`fileCount`/`isComplete`;
- in-progress poll with no results returns `isComplete: False`.
