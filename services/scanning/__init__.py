"""Scanning service package.

Scan orchestration for library synchronisation and music metadata imports.
Coordinates Navidrome import, scan checkpoints/resume, and pipeline stages.

Submodules:
    - scanner:        Top-level scan entry point and main loop.
    - artist_scanner: Artist-level scan orchestration.
    - album_scanner:  Album-level data retrieval.
    - pipeline:       Multi-stage scan pipeline orchestration.
    - navidrome_import: Artist/album/track import from Navidrome.
    - navidrome_service: Service-level Navidrome API coordination.
    - navidrome_scan_service: Config and client management.
    - metadata_extractor: Track metadata extraction from Navidrome payloads.
    - payload_builder: DB payload construction from extracted data.
    - mp3_import_scanner: Physical file import from music directory.
    - library_sync:   Incremental Navidrome library synchronisation.
    - scan_state:     File-based progress and checkpoint tracking.
    - scan_history_service: DB scan history queries.
    - scan_resume_service: DB-based resume fallback.
    - filters:        Pure skip-decision functions.
    - cleanup:        Post-import data sanitisation.
    - bootstrap:      Boot-time scan launcher.
    - runtime_state:  In-process scan state (per-worker).

Architecture:
    Delegates raw SQL to ``db.repositories`` and pure utilities to
    ``helpers``. The scanning service layer owns orchestration only.
"""
