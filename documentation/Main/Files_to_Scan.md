

## Newly Added High Priority

- services/library/library_sync_service.py — critical ingestion entrypoint, now mapped but worth deep trace analysis
- services/scanning/navidrome_import.py — core ingestion engine
- services/scanning/payload_builder.py — scoring overwrite source

## Medium Priority (Layer Consolidation)

- services/scanning/navidrome_service.py — orchestration helpers
- services/scanning/navidrome_scan_service.py — config/client lifecycle (overlap risk)

## Enrichment Deep Inspection

- services/enrichment/musicbrainz_service.py — complex matching logic
- services/enrichment/discogs_service.py — single detection + genre extraction
- services/enrichment/single_detection_service.py — classification layer
- services/enrichment/spotify_service.py — track + playlist logic

## Cross-Layer Flow Validation

- ingestion → payload_builder → DB → popularity pipeline
- detect where scoring fields are lost or overwritten
