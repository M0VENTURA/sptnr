# Release Picker: show actual track numbers per release

## Symptom

In the album page's Release Picker (Lookup on MusicBrainz → select a group
with multiple releases), expanding a release's **Tracks** list showed the
songs in a plain auto-numbered `<ol>` (1..N sequential) instead of the real
MusicBrainz track numbers.  Multi-disc releases and editions with gaps
rendered wrong numbers, making it hard to tell which version of the album a
row was — e.g. a 2-CD edition vs a single CD, or a 13-track deluxe vs the
10-track original.

## Root causes

1. **Renderer used the list index, not the track data** — `toggleReleaseTracks`
   rendered `<ol>` items whose numbers were just the array position, ignoring
   `track_number` / `disc_number` entirely.
2. **The route returned a bare list** — `/api/album/musicbrainz/release/
   tracks` returned `get_musicbrainz_release_tracks()` directly (a bare
   `List[Dict]`), while the renderer expects the legacy `{success, tracks}`
   envelope — so the check `!data.success` short-circuited and the list never
   rendered at all ("No track data available").

## Fix

`static/js/album_detail.js` (`toggleReleaseTracks`):

- Render each track with its **actual MusicBrainz track number** from
  `track_number` / `position` (not the list index).
- Multi-disc releases show `disc-track` (e.g. `2-1`, `2-2`); when no track
  number exists but the release is multi-disc, show `Disc 2: …`.
- Duration reads both `duration_ms` and `duration` (ms) and is displayed in
  seconds.

`routes/album_routes.py` (`api_album_musicbrainz_release_tracks`):

- Wrap the bare track list in the legacy envelope `{success, release_mbid,
  tracks}` (a tuple/dict result is passed through unchanged), so the
  renderer's `data.success` / `data.tracks` checks work.

## Files

- `static/js/album_detail.js`
- `routes/album_routes.py`
- `tests/test_album_release_picker_tracks.py` (new)

## Tests

`tests/test_album_release_picker_tracks.py` covers: a bare track list being
wrapped in the envelope with correct `track_number` / `disc_number`, an empty
list still returning `{success: True, tracks: []}`, and a missing
`release_mbid` returning 400.
