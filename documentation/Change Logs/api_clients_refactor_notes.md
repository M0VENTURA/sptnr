# API clients cleanup notes

## Goal

Keep `api_clients/` as pure HTTP client modules. Move application policy,
fallbacks, scoring, interpretation and orchestration into `services/`.

## Rebuilt files

```text
api_clients/__init__.py
api_clients/acousticbrainz.py
api_clients/audiodb.py
api_clients/listenbrainz.py
api_clients/audiodb_and_listenbrainz.py   # compatibility shim
api_clients/coverartarchive.py

services/music/__init__.py
services/music/artwork_service.py
services/music/listenbrainz_service.py
services/music/discogs_service.py
```

## Merge decisions

### AudioDB

The old `api_clients/audiodb.py` and the `AudioDbClient` from
`api_clients/audiodb_and_listenbrainz.py` are merged into one class-based
`api_clients/audiodb.py`.

### ListenBrainz

ListenBrainz was split out into `api_clients/listenbrainz.py`.

### audiodb_and_listenbrainz.py

This file is now only a compatibility shim so older imports keep working.

### Discogs

`api_clients/discogs.py` should remain for now because it contains detailed
HTTP/rate-limit/circuit-breaker behaviour. New business-level calls should use
`services/music/discogs_service.py`.

## Recommended follow-up

Gradually move more Discogs business logic out of `api_clients/discogs.py`,
especially single-detection policy and cache orchestration.
