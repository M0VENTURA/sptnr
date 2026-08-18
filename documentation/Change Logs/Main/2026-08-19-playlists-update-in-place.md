# Playlists: update in place instead of recreate (fixes Navidrome duplicates)

## Symptom

The generated playlists — **Essential Collection** (`{Artist} - Essential
Collection.m3u`), **Genre Top Tracks** (`{Genre} - Top Tracks.m3u`) and
**New Music.m3u** — are written as `.m3u` files into the Navidrome
Playlists watch folder.  Every scan **rewrote** the file with new content
(tracks added/removed, order changed), and Navidrome imported each rewrite
as a **NEW playlist** (fresh id), leaving the old one behind → duplicate
entries in Navidrome's UI.

## Root cause

The generated playlists only ever touched the `.m3u` file — nothing synced
the Navidrome playlist itself.  Navidrome's `.m3u` importer detects the
file change and re-imports it rather than updating the existing playlist.

## Fix

Three changes:

1. **`api_clients/navidrome.py`** — new `update_playlist_songs(playlist_id,
   song_ids)` method: fetches the current entry count, then calls Subsonic
   `updatePlaylist` with `songIndexToRemove` (0..N-1) + `songIdToAdd`
   (repeatable) to **replace the song list in place** — the playlist's id,
   name, cover and created date are preserved.

2. **`services/playlists/playlist_navidrome_service.py`** — new
   `sync_playlist_by_name(client, name, song_ids)`:
   - finds every regular playlist with the same name (smart `.nsp`
     playlists are left alone),
   - deletes duplicate same-name playlists,
   - updates the primary in place (old songs below the star threshold
     removed, new songs added, order per `song_ids`),
   - creates the playlist only when none exists.

3. **`services/popularity/stages/finalise_stage.py`** — new
   `_sync_playlist_to_navidrome()` helper (wraps the above for every
   configured Navidrome user) wired into all three generators:
   - `_create_essential_m3u` — Essential Collection
   - `_create_genre_top_track_playlists` — Genre Top Tracks
   - `_create_new_music_playlist` — New Music

   The song ids are the local track ids (== Navidrome song ids — the
   library is imported from Navidrome), so the Navidrome playlist's order
   mirrors the same popularity sort used for the `.m3u`.

Now a scan **edits** the existing Navidrome playlist: tracks that no longer
hit the 4★/5★ threshold are removed, new qualifying tracks are added, and
the order is refreshed by popularity.  No more duplicate entries.

## Tests

No automated test (network-dependent API flow) — verified via `get_errors`
and manual review of the update-by-name contract.

## Config

No new config keys.  The sync is best-effort: it runs after each `.m3u`
write and never raises on Navidrome API failure (the `.m3u` file remains
the source of truth).
