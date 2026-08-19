# Fix Navidrome playlist POST body (TypeError) + clarify Wikipedia errors

## Symptom

After the form-encoded POST change, every large playlist sync failed again:

```
Navidrome updatePlaylist POST failed after 120: sequence item 1: expected a bytes-like object, tuple found (TypeError)
Navidrome createPlaylist POST failed after 120: sequence item 1: expected a bytes-like object, tuple found (TypeError)
```

Separately, the log showed:

```
ERROR api_clients.navidrome.134   Navidrome updatePlaylist POST failed ...
ERROR api_clients.wikipedia.062   Failed to fetch Wikipedia page 'List_of_2026_albums':
```

— which made it look like Wikipedia was part of the Navidrome client.

## Root cause 1 — httpx form-encoding trap

`_post_subsonic_response` passed the body as a **list of `(key, value)` tuples**.
httpx 0.28's `encode_request` treats a non-Mapping `data` as RAW byte content
(`data=<bytes...>` compat path), so `b"".join(...)` over the tuple list raised
`sequence item 1: expected a bytes-like object, tuple found`.

The fix: pass a **dict**.  httpx's `encode_urlencoded_data` flattens a dict's
list values into the exact repeated form pairs Subsonic needs
(`songIdToAdd=id1&songIdToAdd=id2&...`) — the URL-length-safe POST body is
unchanged, just encoded correctly.

## Root cause 2 — Wikipedia attribution confusion

`api_clients/wikipedia.py` is a **separate module** (the upcoming-releases
scraper).  The Navidrome-looking line numbers were a log coincidence — both
modules live under the shared `api_clients.` package prefix.  The Wikipedia
errors also showed a **blank message** (`: ` — an exception whose `str()` is
empty, e.g. a timeout), which made diagnosis harder.

## Fixes

- `api_clients/navidrome.py` — `_post_subsonic_response` sends the body as a
  dict (httpx flattens list values into repeated form pairs); docstring
  documents the Mapping requirement.
- `api_clients/wikipedia.py` — the failure log now includes the exception
  TYPE (`Failed to fetch Wikipedia page 'X': <detail> (<ExcType>)`), so a
  blank message is no longer unactionable.
- `tests/test_navidrome_playlist_post_body.py` — regression: the POST body
  must be a dict, not a tuple list; fake session records dicts; body helper
  mirrors httpx's flattening.
