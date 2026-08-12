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

import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from db.engine import db_session
from db.repositories.queue import (
    clear_queue as repo_clear_queue,
    update_queue_item,
    get_queue_status_counts,
)
from services.downloads import download_scan_service
from db.repositories import queue as queue_repo
from services.downloads.download_queue_normalizer import normalize_download_queue

logger = logging.getLogger(__name__)


from services.downloads import download_scan_service
from db.repositories import queue as queue_repo

def run_auto_discovery_cycle() -> Dict[str, Any]:
    """
    High-level orchestrator for the discovery cycle.
    """
    # 1. Get files from the scan service (no DB logic here)
    discovered = download_scan_service.discover_audio_files()

    # 2. Dedupe against the queue and insert new items (shared with the
    #    manual "Discover Files" action).
    return download_scan_service.enqueue_discovered_files(discovered)