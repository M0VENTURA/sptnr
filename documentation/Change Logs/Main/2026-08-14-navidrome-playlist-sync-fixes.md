# Playlist → Navidrome sync fixes (genre orphan sweep + essential lookup)

## Symptom

1. Genre `{Genre} - Top Tracks` playlists were removed from the Playlists
   folder (pool dropped below `genre_playlists_delete_threshold`) but the
   imported playlist still showed in Navidrome.
2. Essential collections (`[Artist] - Essential Collection.m3u`) were not
   being created at the end of per-artist scans.

## Root causes

**1. Navidrome deletion was fragile and one-shot.**
`_delete_genre_playlist_from_navidrome` (finalise_stage.py) read raw config
keys (`user.get("user")` / `user.get("pass")`) so `username`/`password`
shaped configs silently no-opped; failures were debug-only; and it only ran
for the exact `.m3u` file removed in the same pass — a failed/out-of-band
delete was never retried, so Navidrome kept the playlist forever.

**2. Essential lookup was case-sensitive.**
`_create_essential_m3u` queried `album_artist = :artist` exact-match (same
class of bug as the album MB-compare "no library tracks" issue). Genre
playlists are library-wide (no artist filter) so they worked; per-artist
essential collections returned 0 rows on any case/whitespace difference → no
file, silent.

## Fixes (finalise_stage.py)

- `_navidrome_clients()` — resolve clients via
  `get_navidrome_users_normalized()` (handles `user`/`pass`,
  `username`/`password`, legacy `navidrome`, env).
- `_delete_genre_playlist_from_navidrome` — uses the normalised clients;
  logs WARNING when the playlist isn't found / delete fails (diagnosable).
- `_sweep_orphaned_genre_playlists_from_navidrome()` — self-healing sweep:
  fetches Navidrome playlists, matches the genre template suffix (e.g.
  `- Top Tracks`), and deletes any whose `.m3u` is missing from the watch
  dir. Runs at the end of `_create_genre_top_track_playlists` (both normal
  and `prune_only`/startup paths). Re-deletes previously-failed orphans.
- `_create_essential_m3u` — both queries now `LOWER(TRIM(COALESCE(NULLIF(
  album_artist,''), artist))) = LOWER(TRIM(:artist))` (and `<>` for the
  featured-pass). Added a log when an essential collection is skipped with
  the unique-track count vs `_ESSENTIAL_MIN_TRACKS`.

## Tests

`tests/test_genre_playlists.py` → `TestNavidromeOrphanSweep`:

- `test_sweeps_orphaned_keeps_present_and_foreign`
- `test_respects_delete_toggle`
- `test_delete_by_name_uses_normalized_clients`

Existing genre/essential tests unaffected (sweep no-ops without Navidrome
config; essential tests use pre-built Rows so the SQL WHERE is not executed).
