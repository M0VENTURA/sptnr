# Popularr new structure without shims

No compatibility shims are included. New code imports directly from `services/*` or low-level `api_clients/*_http.py`.

## Key files

```text
app.py
helpers/task_manager.py
cli/popularity_cli.py
routes/popularity_routes.py
api_clients/musicbrainz_http.py
api_clients/spotify_http.py
api_clients/slskd_http.py
api_clients/wikidata_http.py
services/enrichment/musicbrainz_service.py
services/enrichment/single_detection_service.py
services/enrichment/spotify_service.py
services/enrichment/wikidata_bio_service.py
services/downloads/slskd_service.py
services/popularity/pipeline.py
services/popularity/legacy_scanner.py
services/popularity/popularity_math.py
services/popularity/popularity_matching.py
services/popularity/popularity_sources.py
services/popularity/popularity_adjustments.py
services/popularity/standout_service.py
```

Move your old `popularity_scan(...)` implementation into `services/popularity/legacy_scanner.py`.

