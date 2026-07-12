"""services.downloads package.

End-to-end download management services:
    - Scanning: File discovery (download_scan_service)
    - Queue: Download queue management & normalization (download_queue_*)
    - Processing: Pipeline orchestration (download_pipeline_service)
    - Organization: File moving & renaming (download_organize_*)
    - Matching: Folder/release matching (match_engine, match_orchestrator)
    - Cleanup: Post-download file cleanup (cleanup_engine_service)
    - Retry: Failed download retry logic (download_retry_service)
    - Scheduler: Periodic maintenance (download_scheduler_service)
    - slskd: Soulseek download client wrapper (slskd_service)

Import pattern:
    All public functions are re-exported at the package level for
    convenient access::

        from services.downloads import scan_downloads, get_folder_groups
"""

from importlib import import_module
from typing import Any

__all__ = [
    "scan_downloads",
    "get_scan_progress",
    "verify_moved_files",
    "discover_files",
    "get_folder_groups",
    "get_folder_details",
    "cancel_folder",
    "organize_folder",
    "organize_single_file",
    "merge_folders",
    "match_folder",
    "auto_match_folder",
    "search_and_update_musicbrainz",
    "select_best_musicbrainz_candidate",
    "start_scheduler",
    "stop_scheduler",
    "scheduler_status",
]


_EXPORTS: dict[str, str] = {
    "scan_downloads": ".download_scan_service",
    "get_scan_progress": ".download_scan_service",
    "verify_moved_files": ".download_scan_service",
    "discover_files": ".download_scan_service",
    "get_folder_groups": ".download_folder_service",
    "get_folder_details": ".download_folder_service",
    "cancel_folder": ".download_folder_service",
    "organize_folder": ".download_organize_service",
    "organize_single_file": ".download_organize_service",
    "merge_folders": ".download_organize_service",
    "match_folder": ".download_matching_service",
    "auto_match_folder": ".download_matching_service",
    "search_and_update_musicbrainz": ".download_matching_service",
    "select_best_musicbrainz_candidate": ".download_matching_service",
    "start_scheduler": ".download_scheduler_service",
    "stop_scheduler": ".download_scheduler_service",
    "scheduler_status": ".download_scheduler_service",
}


def __getattr__(name: str) -> Any:
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module = import_module(_EXPORTS[name], __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value
