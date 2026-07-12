# Discogs split notes

## New structure

```text
api_clients/discogs_http.py
  Low-level Discogs HTTP, throttling, 429 handling, 5xx retry/circuit breaker.

services/enrichment/discogs_service.py
  App-level Discogs enrichment: single detection, matching, cache access,
  artist profile cleanup, genre/profile helper logic.

api_clients/discogs.py
  Compatibility facade preserving old imports and method names.
```

## Why this split

`api_clients` should not own business rules. It should only know how to talk
to external APIs.

Discogs single detection is business/enrichment logic because it decides how
to interpret Discogs data for Popularr scoring. That now lives in
`services/enrichment/discogs_service.py`.

## Migration guidance

Existing code can keep using:

```python
from api_clients.discogs import DiscogsClient
```

New code should prefer:

```python
from services.enrichment.discogs_service import DiscogsService
```

or for raw HTTP:

```python
from api_clients.discogs_http import DiscogsHttpClient
```

## Compatibility

The following wrapper functions are preserved in `api_clients/discogs.py`:

- `is_discogs_single`
- `get_discogs_genres`
- `has_discogs_video`
- `get_discogs_artist_biography`

The previous `services/music/*` files are now compatibility shims to
`services/enrichment/*`.
