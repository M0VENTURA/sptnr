# Release Picker: show track counts on concrete releases

## Symptom

On the album page, Lookup on MusicBrainz → select a release → the Release
Picker listed the concrete releases (name + country like AU/CA) but showed
NO track counts, making it hard to tell a 10-track original from a 13-track
deluxe.

## Root cause

The picker's releases came from two paths, and both could miss track counts:

1. **Fallback fetch** (`/api/album/musicbrainz/release-group/releases`):
   the route called `get_release_group_releases(rg_mbid)` WITHOUT
   `include_track_counts=True`, and `_enrich_releases_with_track_counts`
   tried to derive the release-group MBID from
   `releases[0].get("release-group")` — but the flattened releases from the
   release-group endpoint never carry that nested object, so the function
   always returned early and no counts were ever attached.  The MusicBrainz
   release-group `inc="releases"` response does NOT include each release's
   `media` track-counts, so `track_count` stayed 0.
2. **Renderer**: a `track_count` of 0 (unknown) rendered as a misleading
   "0 tracks".

## Fix

`routes/album_routes.py`:

- `api_release_group_releases` now passes `include_track_counts=True` so the
  browse endpoint supplies real per-release track counts.

`services/enrichment/musicbrainz_service.py`:

- `_enrich_releases_with_track_counts` accepts `rg_mbid` directly (the
  flattened releases don't carry the nested `release-group` object) and only
  falls back to deriving it for raw-MB callers.  `get_release_group_releases`
  passes the group MBID through.

`static/js/album_detail.js`:

- The picker renders the track count only when it is a real positive number
  (unknown/0 shows nothing rather than a misleading "0 tracks").

## Files

- `routes/album_routes.py`
- `services/enrichment/musicbrainz_service.py`
- `static/js/album_detail.js`
- `tests/test_album_release_picker_contract.py` (extended)

## Tests

`test_album_release_picker_contract.py` gains: `include_track_counts=True`
browses the group and attaches real counts to flattened releases (the
regression), and the flag-off path keeps the (zero) media-derived counts
without a double fetch.
