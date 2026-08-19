# Fix: Navidrome playlist sync for large playlists (form-encoded POST)

## Symptom

Large generated playlists (genre "Top Tracks", New Music, Essential
Collections) failed every sync:

```
Navidrome updatePlaylist failed after 1 attempts: URL component 'query' too long (InvalidURL)
[NAVIDROME] updatePlaylist songs rejected for A9aHbYmjVLgB2gOhPQnSvL: {}
[PLAYLISTS] updatePlaylist failed for 'Alternative Rock - Top Tracks' — falling back to recreate? (not implemented: in-place is the contract)
```

A 1119-song "Nu Metal - Top Tracks" playlist blew past the URL length limit.
Only small playlists (Ballad 192, Cover 727, Deathcore 533...) succeeded.

## Root cause

`NavidromeClient.update_playlist_songs` sent the ENTIRE new song list as
repeated `songIdToAdd` **query parameters** via `_get_subsonic_response`
(GET).  Each `songIdToAdd` is a ~24-char Navidrome UUID, so a 1000+ song
playlist produces a multi-KB query string that exceeds the server's URL
length limit.  The `createPlaylist` path had the same risk (and additionally
mis-serialised its `songId` list through the query-param helper, sending a
URL-encoded `params` dict instead of the song ids).

## Fix — form-encoded POST body

Subsonic's REST endpoints accept both GET (params in the URL) and POST
(params in the request body); Navidrome implements both.  The POST body has
no URL length limit.

- `api_clients/navidrome.py`:
  - New `_post_subsonic_response(endpoint, ..., **params)` — sends the
    params as a form-encoded POST body, flattening repeated list values into
    repeated `(key, value)` pairs (`songIdToAdd=id1&songIdToAdd=id2&...`).
  - `update_playlist_songs` now uses it (`timeout=120`) — a 1119-song
    playlist fits in the body.
  - New `create_playlist(name, song_ids)` uses the same safe POST path.
- `services/playlists/playlist_navidrome_service.py` — the create branch
  calls `client.create_playlist(...)` instead of the broken query-param
  `_get_subsonic_response("createPlaylist", params=...)` call.

## Files

- `api_clients/navidrome.py` — `_post_subsonic_response`, POST-based
  `update_playlist_songs`, new `create_playlist`.
- `services/playlists/playlist_navidrome_service.py` — create branch uses
  the client's POST method.
- `tests/test_navidrome_playlist_post_body.py` — regression tests (1500-song
  update via POST body, index removal, empty playlist, create via POST,
  service wiring).
