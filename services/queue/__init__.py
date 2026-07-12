"""Queue services package.

Domain-specific queue services for managing the download queue lifecycle.

Submodules:
    - queue_processing_service: Post-download queue processing and matching.
    - queue_matching_service: Soulseek candidate matching and scoring.
    - queue_matching_helpers: Filename and title-based matching utilities.
    - queue_metadata_matcher: Metadata-based file-to-queue matching.
    - queue_scoring: Soulseek candidate scoring algorithms.
    - queue_cleanup_service: Queue maintenance and cleanup operations.
    - queue_diagnostics_service: Queue health and event monitoring.
    - queue_orchestrator: Background processing orchestration.
    - queue_worker: Standalone worker process entry point.
    - queue_config: Timeout and retry configuration.
    - queue_constraints: Status definitions and validation.
    - task_runner: Async task execution utilities.

Routes should import directly from specific service modules.
Do not add business logic at the package level.
"""

from __future__ import annotations

from .task_runner import run_async_task, get_task, set_task

__all__ = [
    "run_async_task",
    "get_task",
    "set_task",
]