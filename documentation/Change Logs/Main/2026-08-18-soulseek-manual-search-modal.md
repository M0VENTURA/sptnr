# Restore Manual Soulseek Search modal on downloads pages

## Symptom

The search button on downloads queue items no longer opened a Soulseek
search modal like the old system's downloads monitor did.  On the queue
page (`/downloads`) the search button switched to the in-page Soulseek tab
and submitted the form; on the monitor page (`/downloads/monitor` — the
main Downloads destination) queue items had no Soulseek search button at
all, only Retry/Cancel/Delete.

## Root cause

The manual Soulseek search modal (`soulseekManualSearchModal`) and its
queue-aware search flow (`searchOtherSources`, `runSoulseekManualSearch`,
`pollSoulseekManualSearchResults`, `downloadSoulseekManualResult`, …) lived
only in the old system's `downloads_monitor.html`.  The refactored frontend
split the downloads pages across `templates/pages/downloads/*.html` +
`static/js/downloads.js` / `monitor.js` and the modal was never ported:

- `manualQueueSlskdSearch` (queue/manager pages) pre-filled the in-page
  Soulseek tab instead of opening a modal.
- `monitor.js` queue rows had no search action at all.
- `normalizeSoulseekQuery` was referenced by `downloads_page.js` /
  `search_init.js` but never defined anywhere in the current JS.

## Fix

- `static/js/downloads.js` — added the missing `normalizeSoulseekQuery`
  helper and ported the full manual Soulseek search flow: `searchOtherSources`
  / `searchOtherSourcesFromEncoded`, `ensureSoulseekManualSearchModal`,
  `openSoulseekManualSearchModal`, `runSoulseekManualSearch` (with slot-busy
  wait + auto-start), `pollSoulseekManualSearchResults`,
  `renderSoulseekManualSearchResults`, `downloadSoulseekManualResult`
  (queue-aware via `/api/slskd/queue-download` when opened from a queue
  item), and the `?search=` URL auto-open.  `manualQueueSlskdSearch` now
  opens the modal (queue-aware) and falls back to the in-page tab only when
  the modal is unavailable.
- `static/js/monitor.js` — queue item rows now get a search (🔍) button
  that opens the modal pre-filled with the item's artist + title, linking
  the chosen result to the queue row.
- `templates/components/modals/_soulseek_manual_search.html` — new reusable
  modal component.
- `templates/pages/downloads/monitor.html`, `queue.html`, `manager.html` —
  include the modal component.

## Files

- `static/js/downloads.js` — manual search modal flow + normalize helper.
- `static/js/monitor.js` — queue-item search button.
- `templates/components/modals/_soulseek_manual_search.html` — modal markup.
- `templates/pages/downloads/{monitor,queue,manager}.html` — modal includes.
