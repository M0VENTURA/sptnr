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
from db.context import db_cursor  # TODO: migrate to db_session
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
    
    # 2. Get state for deduplication
    existing_signatures = queue_repo.get_active_queue_signatures()
    
    stats = {'queued': 0, 'already_in_queue': 0}
    
    for file_info in discovered:
        # Check against DB using repo helpers
        existing = queue_repo.find_existing_discovered_file(
            file_path=file_info.full_path,
            filename=file_info.filename,
            rel_path=file_info.rel_path
        )
        
        if existing:
            stats['already_in_queue'] += 1
            continue
            
        # Add new item via repo
        queue_repo.insert_discovered_file(
            artist="Unknown", # Extract via metadata_reader in your loop
            title=file_info.filename,
            album="Unknown",
            album_artist=None,
            track_number=None,
            disc_number=None,
            year=None,
            duration=None,
            file_path=file_info.full_path,
            filename=file_info.filename,
            import_group="default"
        )
        stats['queued'] += 1
        
    return stats