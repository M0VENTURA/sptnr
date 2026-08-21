# Universal search: render local results first, MB in the background

## Symptom

The universal search still felt slow even after the pg_trgm indexes: the
modal showed a spinner until EVERYTHING loaded, including the MusicBrainz
call.

## Root causes

1. **The render waited for both promises.**  `runSearch` used
   `Promise.all([localPromise, mbPromise])` — the fast in-library results
   were held back until the slow MusicBrainz search completed.  Every
   keystroke therefore felt blocked on MusicBrainz.

2. **The MusicBrainz call itself was heavy.**  The shared
   `/api/musicbrainz/search` endpoint enriched EVERY release-group result
   with its concrete releases (``_attach_concrete_releases``), which does
   one ``browse_releases_for_group`` call PER result — 20 results = 20
   sequential throttled MusicBrainz API calls on top of the search.  The
   universal search doesn't need those (it shows the group rows and opens
   the picker lazily on click), but it was paying the full cost on every
   keystroke.

## Fix

`static/js/unified_search.js`:

- **Split rendering**: the local (in-library) results render IMMEDIATELY
  when the fast DB query returns; MusicBrainz results merge into the same
  buckets when they arrive (a lightweight "Loading MusicBrainz results…"
  notice shows meanwhile in the All scope).  The scope-pill counts update
  as each source lands.
- MB scope still shows the MB-only tab once MB loads; Library scope stays
  local-only (its MB call was always just for the pill count).

`routes/musicbrainz_routes.py`:

- The concrete-release enrichment is now **opt-in** via a
  ``with_releases`` request flag (default OFF).  Free-text / discovery
  searches (the universal search) skip the per-group browse entirely —
  one MusicBrainz API call instead of 1 + N.

`templates/components/_musicbrainz_search_component.html` +
`static/js/downloads.js`:

- Send ``with_releases=true`` when the flow requests it
  (``window._mbSearchWithReleases``).

`static/js/album_detail.js` + `static/js/monitor.js`:

- The album-page lookup and folder-match flows set
  ``window._mbSearchWithReleases = true`` so their release pickers still
  get the concrete releases (the picker needs them to prompt which edition).

## Files

- `static/js/unified_search.js`
- `routes/musicbrainz_routes.py`
- `templates/components/_musicbrainz_search_component.html`
- `static/js/downloads.js`
- `static/js/album_detail.js`
- `static/js/monitor.js`
- `tests/test_mb_search_concrete_releases.py`

## Tests

`test_mb_search_concrete_releases.py`: the concrete-release tests now pass
``with_releases=True`` (the expensive browse is opt-in), and a new test
asserts the default path skips the browse entirely (groups come back without
a ``releases`` key) — the speed fix.
