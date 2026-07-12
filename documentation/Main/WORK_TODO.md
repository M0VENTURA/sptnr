
# POPULARR WORK TODO

## CRITICAL
- Replace popularity_scan() with run_popularity_scan()
- Remove dependency on root popularity.py

## PIPELINE
- Wire services.popularity.pipeline into scanning pipeline
- Remove duplicate pipeline (2).py

## STAGED SYSTEM
- Implement load_stage logic
- Implement album_stage logic
- Implement track_stage scoring
- Implement finalise_stage logic

## PROGRESS SYSTEM
- Replace progress.py usage with scan_state.py
- Add stop flags + read functions

## CLEANUP
- Remove duplicate files (* (2).py)
- Remove legacy scanner files

## ARCHITECTURE
- Move helper logic into services layer
- Ensure services do not write DB directly

## OPTIONAL
- Add status flags per file (STABLE/MIGRATING)


## 11. Library Sync + Ingestion Alignment

- Confirm `library_sync_service` is the canonical sync entrypoint
- Trace ingestion → payload_builder → popularity pipeline overwrite chain
- Ensure ingestion does not permanently overwrite scoring fields
- Decide if NAVIDROME_SCORE_DEFAULTS should be conditional

## 12. Navidrome Layer Consolidation

- Review overlap between:
  - navidrome_import.py
  - navidrome_service.py
  - navidrome_scan_service.py
- Define clear ownership:
  - ingestion vs orchestration vs client lifecycle

## 13. Payload / Scoring Conflict

- Investigate scoring resets caused by payload_builder
- Ensure scoring pipeline re-runs after ingestion
- Validate no race condition between:
  - library sync
  - popularity scan

## 14. Enrichment Architecture Cleanup

- Keep enrichment services read-only where possible
- Ensure persistence only occurs in explicit persistence services
- Validate no silent DB writes in enrichment layer
