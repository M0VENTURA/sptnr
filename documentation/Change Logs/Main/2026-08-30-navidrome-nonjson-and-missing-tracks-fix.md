# Navidrome non-JSON responses + missing-tracks RowMapping fix (2026-08-30)

## Symptom

```
ERROR api_clients.navidrome  Navidrome updatePlaylist POST failed ... JSONDecodeError 'Expecting value: line 1 column 1 (char 0)'
ERROR api_clients.navidrome  Navidrome getScanStatus failed after 1 attempts ... ReadTimeout 'timed out'
ERROR api_clients.navidrome  Navidrome setRating failed after 1 attempts ... ReadTimeout 'timed out'
ERROR routes.album_routes    Failed to get missing tracks ... "Could not locate column in row for column '0'"
```

## Root causes + fixes

### 1. `updatePlaylist` POST → JSONDecodeError (`api_clients/navidrome.py`)

Navidrome returns a 2xx with a NON-JSON body (an HTML error page, a bare
"ok" string, or a whitespace-padded response) for mutation endpoints like
`updatePlaylist`.  `_post_subsonic_response` only guarded against EMPTY
bodies; a non-empty non-JSON body fell through to `response.json()` and
raised `JSONDecodeError`, logged as a loud ERROR every few minutes even
though the mutation succeeded.

**Fix:** `_post_subsonic_response`, `_get_subsonic_response`, and
`upload_playlist_cover` now catch `(json.JSONDecodeError, ValueError)` on a
2xx response and treat it as a successful/empty response (mutation endpoints
are HTTP-2xx = success).  The auth-fallback retry path is guarded the same
way.  `json` imported.

### 2. `getScanStatus` / `setRating` ReadTimeout spam

These time out while Navidrome is busy (mid library scan, DB locked).
`_log_throttled_error` throttled to once per 60s but logged at ERROR, so
error.log accumulated "timed out" noise.

**Fix:** transient network timeouts (`ReadTimeout`, `ConnectTimeout`,
`WriteTimeout`, `Timeout`, `ConnectionError`, `ConnectError`,
`RemoteProtocolError`) are now logged at **WARNING** (throttled); hard
failures (auth, 4xx/5xx) stay at ERROR.  Transient Navidrome unavailability
is expected and non-fatal — it no longer pollutes error.log.

### 3. `get_missing_tracks` → "Could not locate column in row for column '0'"

`services/metadata/album_missing_service.py` queried with
`.mappings().all()` (RowMapping rows) then read `mb_row[0]` — indexing a
RowMapping by INTEGER position raises "Could not locate column in row for
column '0'".  The album page's missing-tracks check 500'd.

**Fix:** read the MBID via the column name
(`mb_row.get("musicbrainz_album_mbid")`).

## Files

- `api_clients/navidrome.py`
- `services/metadata/album_missing_service.py`
- `tests/test_navidrome_playlist_post_body.py` (non-JSON 2xx = success)
- `tests/test_album_missing_and_disc_cleanup.py` (stored-MBID no-index bug)
