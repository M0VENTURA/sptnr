"""
MP3 metadata import pipeline.

Owns MP3ImportScanner construction and execution. Routes should only call this
pipeline asynchronously.
"""

from __future__ import annotations

from typing import Any

import structlog

from services.scanning.scan_state import (
    get_scan_progress_path,
    write_progress_with_current_artist,
)

logger = structlog.get_logger(__name__)


def run_mp3_import_pipeline(
    *,
    directory: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """
    Run MP3 metadata import.

    Args:
        directory:
            Optional directory path. If provided, scanner runs in directory mode.
            Otherwise it runs against database track paths.
        dry_run:
            If true, scanner should not persist changes where supported.
    """
    progress_file = get_scan_progress_path("mp3_import")
    mode = "directory" if directory else "database"

    # Clear any stale stop flag left by a previous Stop-all so this import
    # isn't immediately halted (the "any scan after Stop is stopped again"
    # issue).
    try:
        from services.scanning.scan_state import clear_stop_request
        clear_stop_request(progress_file)
    except Exception as exc:
        logger.debug("MP3 import stop-flag clear failed", error=str(exc))

    try:
        write_progress_with_current_artist(
            progress_file,
            "mp3_import",
            True,
            extra={
                "status": "running",
                "mode": mode,
                "dry_run": dry_run,
            },
        )

        from services.scanning.mp3_import_scanner import MP3ImportScanner

        scanner = MP3ImportScanner(
            directory=directory,
            dry_run=dry_run,
            verbose=True,
            mode=mode,
        )

        results = scanner.scan()

        write_progress_with_current_artist(
            progress_file,
            "mp3_import",
            False,
            extra={
                "status": "complete",
                "mode": mode,
                "dry_run": dry_run,
                "exit_code": 0,
            },
        )

        return {
            "success": True,
            "mode": mode,
            "dry_run": dry_run,
        }

    except Exception as exc:
        logger.exception("MP3 import pipeline failed", error=str(exc))

        write_progress_with_current_artist(
            progress_file,
            "mp3_import",
            False,
            extra={
                "status": "error",
                "error": str(exc),
                "exit_code": 1,
            },
        )

        return {
            "success": False,
            "error": str(exc),
            "mode": mode,
            "dry_run": dry_run,
        }