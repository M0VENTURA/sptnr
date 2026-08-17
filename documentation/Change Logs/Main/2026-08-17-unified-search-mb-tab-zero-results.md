# Fix: unified search MusicBrainz tab always showed zero results

## Symptom

In the global search flyout (All / In Library / MusicBrainz tabs), the
**MusicBrainz tab** always showed "No MusicBrainz releases found" — while the
same query under **All** displayed MusicBrainz releases.

## Root cause

`static/js/unified_search.js` `runSearch()` branches on the active scope:

- `all` → runs the MB search (limit 12) and merges it with library results.
- `library` → runs the MB search only for the badge count.
- `mb` (the MusicBrainz tab) → fell into the `else` and resolved
  `mbPromise` to `Promise.resolve([])` — **the MB search never ran on the
  tab that exists to show MB results.** The tab rendered the empty list.

## Fix

`static/js/unified_search.js` — the `mb` scope now calls
`fetchMb(query, MB_LIMIT_MB_TAB, mbOpts)` (the full 25-result limit), so the
tab renders real MusicBrainz releases with the same quick-queue buttons as
the All tab.

## Related: albums "filtered" after removal from the collection

The MB search also hides release-groups the library already owns
(`_dedupe_owned_releases` in `routes/musicbrainz_routes.py`, plus the
client-side `owned` marking) — both key off the `tracks` table. Albums
removed from Navidrome keep being treated as owned until the stale `tracks`
rows are deleted, which happens on the next change scan after commit
`d834926b` (Navidrome import removal fix). After that scan, removed albums
appear as queueable MusicBrainz results again.

## Files

- `static/js/unified_search.js` — MB scope runs the real search
