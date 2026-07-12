# MusicBrainz and helper adjustment notes

## Further MusicBrainz recommendations implemented

The previous MusicBrainz split was extended so specialised methods no longer
need to remain inside `api_clients/musicbrainz.py`:

- artist country lookup
- artist members/member names
- video relationship detection
- various-artists appearance detection
- composer lookup
- recording search by artist
- clean-name lookup
- album type fallback
- release suggestion extraction
- release-group to release MBID resolution

Raw HTTP remains in:

```text
api_clients/musicbrainz_http.py
```

Interpretation/business logic now lives in:

```text
services/enrichment/musicbrainz_service.py
```

DB-mutating MusicBrainz logic now lives in:

```text
services/enrichment/musicbrainz_persistence_service.py
```

## Helpers moved/recommended

### tag_manager.py

Split into:

```text
services/metadata/tag_constants.py
db/repositories/tag_repository.py
services/metadata/tag_file_service.py
helpers/tag_manager.py  # compatibility shim
```

Rationale:
- Constants are shared metadata definitions.
- DB tag reads/writes belong in repositories.
- MP3/FLAC file writes belong in metadata/file services.

### album_art_manager.py

Moved to:

```text
services/enrichment/album_art_service.py
helpers/album_art_manager.py  # compatibility shim
```

Rationale:
- Album-art selection/fetching/application is enrichment + file write orchestration.
- DB caching is explicit in service functions.

### api_rate_limiter.py

Moved to:

```text
services/infrastructure/api_rate_limiter.py
helpers/api_rate_limiter.py  # compatibility shim
```

Rationale:
- Cross-provider rate limiting is infrastructure, not a generic helper.

### api_response.py

Moved to:

```text
services/web/api_response.py
helpers/api_response.py  # compatibility shim
```

Rationale:
- Flask response helpers are web/route infrastructure.

## Important note about tag_file_service

The provided `tag_file_service.py` preserves the core MP3/FLAC behaviour and
the important MPEG-frame fallback path. If you rely on every niche frame from
the old helper, keep expanding the mapping inside `services/metadata/tag_file_service.py`.
Do not move that logic back into helpers or api_clients.
