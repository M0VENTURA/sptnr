# Last.fm and MusicBrainz split notes

## Last.fm

New files:

```text
api_clients/lastfm_http.py
services/enrichment/lastfm_service.py
api_clients/lastfm.py
```

`lastfm_http.py` owns raw request behaviour.
`lastfm_service.py` owns multi-artist matching, recommendation cache policy,
track/album interpretation and Last.fm-specific enrichment logic.
`lastfm.py` is a compatibility facade.

## MusicBrainz

New files:

```text
api_clients/musicbrainz_http.py
services/enrichment/musicbrainz_service.py
api_clients/musicbrainz.py
api_clients/musicbrainz_utils.py
```

`musicbrainz_http.py` owns raw MusicBrainz HTTP and throttling.
`musicbrainz_service.py` owns single detection, title/version parsing, release
matching, release suggestions and release-group to release resolution.
`musicbrainz.py` remains the compatibility facade.
`musicbrainz_utils.py` is now a compatibility shim.

## Important note

This split keeps the main public method names intact, but the original
`musicbrainz.py` was very large. If any rarely-used method is still imported
directly, add it to the facade and delegate to `MusicBrainzService`.
