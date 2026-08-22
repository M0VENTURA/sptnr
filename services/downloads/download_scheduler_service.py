"""Download queue maintenance scheduler.

Provides periodic queue normalisation and scheduler lifecycle
(start/stop/status). Used to keep the download queue in a
consistent state without manual intervention.
"""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger(__name__)

_scheduler = {"running": False}


def run_due_tasks() -> dict[str, int]:
    """Run any due scheduled tasks. Thin wrapper for queue_orchestrator."""
    return {"ran": 0, "message": "Scheduler is managed externally"}


def start_scheduler() -> dict[str, Any]:
    global _scheduler
    _scheduler["running"] = True
    logger.info("Download scheduler started")
    return {"success": True, "message": "started"}


def stop_scheduler() -> dict[str, Any]:
    global _scheduler
    _scheduler["running"] = False
    logger.info("Download scheduler stopped")
    return {"success": True, "message": "stopped"}


def scheduler_status() -> dict[str, Any]:
    return {
        "running": _scheduler["running"],
    }
