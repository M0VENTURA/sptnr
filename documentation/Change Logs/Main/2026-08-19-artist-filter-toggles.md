# Artist page: In Library / Missing toggles behave as on/off switches

## Symptom

The artist page's albums section has "In Library" and "Missing" buttons at
the top, but they didn't behave as toggles — clicking them didn't reliably
switch the visible rows, and empty category cards stayed on screen with
misleading counts.

## Fix

`static/js/artist_detail.js` — `setArtistFilter` now:

1. **Toggles on/off**: clicking the already-active filter (In Library or
   Missing) clears it back to "All"; clicking the other one switches.  The
   buttons behave as on/off switches instead of a sticky 3-way radio.
2. **Hides empty category sections**: when a filter leaves a category with
   zero visible rows (e.g. "Missing" leaves no studio-album rows), the
   whole `.category-section` card is hidden rather than showing an empty
   body with a stale "X / Y in Library" badge.  Selecting "All" re-shows
   every section.

Works for both server-rendered rows (`data-status=library`/`missing`) and
the dynamically-injected missing rows (`data-source=live-missing`).

## Tests

No automated test (frontend-only change) — verified via `get_errors` and
review of the toggle + section-hiding logic.

## Config

No new config keys.
