"""
DOWNLOAD QUEUE SERVICE

Owns queue state, mutation, and high-level orchestration.
This replaces services.queue wrapper logic.

Responsibilities:
- Queue retrieval
- Queue updates
- Retry / requeue
- Maintenance entry point
"""

from __future__ import annotations

from typing import Any

import structlog

from db.repositories.queue import (
    clear_queue as repo_clear_queue,
    get_queue_status_counts,
    update_queue_item,
)
from services.downloads import download_scan_service
from services.downloads.download_queue_normalizer import normalize_download_queue

logger = structlog.get_logger(__name__)


def run_auto_discovery_cycle() -> dict[str, Any]:
    """High-level orchestrator for the discovery cycle."""
    # 1. Get files from the scan service (no DB logic here)
    discovered = download_scan_service.discover_audio_files()

    # 2. Dedupe against the queue and insert new items (shared with the
    #    manual "Discover Files" action).
    return download_scan_service.enqueue_discovered_files(discovered)
