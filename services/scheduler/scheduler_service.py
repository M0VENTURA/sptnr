"""
APScheduler integration for Popularr.

Replaces ad-hoc ``threading.Thread`` for periodic background tasks with
a persistent, configurable scheduler that survives gunicorn worker reloads.

Managed jobs:
    - ``library_sync`` — Periodic library sync with Navidrome (default: every 6 hours)
    - ``popularity_scan`` — Periodic popularity recalculation (default: daily)
    - ``download_queue_processor`` — Process queued downloads (default: every 30s)

Architecture:
    The scheduler runs in a dedicated thread within each gunicorn worker.
    For true cross-worker persistence, the scheduler state can be persisted
    via SQLAlchemy job store (configured below).

Usage:
    from services.scheduler.scheduler_service import get_scheduler
    scheduler = get_scheduler()
    scheduler.start()
"""

from __future__ import annotations

import logging
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

from db.engine import get_engine

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_SCHEDULER: BackgroundScheduler | None = None


def get_scheduler() -> BackgroundScheduler:
    """Return the singleton scheduler instance, creating it if necessary."""
    global _SCHEDULER
    if _SCHEDULER is not None:
        return _SCHEDULER

    from helpers.config_helpers import get_config
    cfg = get_config() or {}
    scheduler_cfg = cfg.get("scheduler", {})

    # Use SQLAlchemy job store for persistence across restarts
    jobstores: dict[str, Any] = {}
    try:
        engine = get_engine()
        jobstores["default"] = SQLAlchemyJobStore(engine=engine)
    except Exception as exc:
        logger.warning("SQLAlchemy job store unavailable, using in-memory: %s", exc)

    _SCHEDULER = BackgroundScheduler(
        jobstores=jobstores or None,
        timezone=scheduler_cfg.get("timezone", "UTC"),
        job_defaults={
            "coalesce": True,       # Merge missed runs into one
            "max_instances": 1,     # Don't overlap
            "misfire_grace_time": 300,  # 5 min grace period
        },
    )

    _register_default_jobs(_SCHEDULER, scheduler_cfg)
    return _SCHEDULER


# ---------------------------------------------------------------------------
# Job definitions
# ---------------------------------------------------------------------------

def _register_default_jobs(scheduler: BackgroundScheduler, cfg: dict[str, Any]) -> None:
    """Register built-in periodic jobs unless they exist or are disabled."""

    jobs = cfg.get("jobs", {})

    # ── Library sync ──────────────────────────────────────────────────────
    if jobs.get("library_sync", {}).get("enabled", True):
        interval_minutes = jobs.get("library_sync", {}).get("interval_minutes", 360)
        try:
            from services.library.library_sync_service import request_library_sync
            if scheduler.get_job("library_sync") is None:
                scheduler.add_job(
                    request_library_sync,
                    trigger=IntervalTrigger(minutes=interval_minutes),
                    id="library_sync",
                    name="Library sync with Navidrome",
                    replace_existing=True,
                )
                logger.info("APScheduler: registered library_sync (every %s min)", interval_minutes)
            else:
                logger.info("APScheduler: library_sync already registered")
        except Exception as exc:
            logger.warning("APScheduler: failed to register library_sync: %s", exc)

    # ── Popularity scan ───────────────────────────────────────────────────
    if jobs.get("popularity_scan", {}).get("enabled", True):
        interval_minutes = jobs.get("popularity_scan", {}).get("interval_minutes", 1440)  # daily
        try:
            from services.scanning.pipelines.popularity_pipeline import run_popularity_mode
            if scheduler.get_job("popularity_scan") is None:
                scheduler.add_job(
                    run_popularity_mode,
                    trigger=IntervalTrigger(minutes=interval_minutes),
                    id="popularity_scan",
                    name="Popularity recalculation",
                    replace_existing=True,
                    kwargs={"mode": "popularity"},
                )
                logger.info("APScheduler: registered popularity_scan (every %s min)", interval_minutes)
            else:
                logger.info("APScheduler: popularity_scan already registered")
        except Exception as exc:
            logger.warning("APScheduler: failed to register popularity_scan: %s", exc)

    # ── Download queue processor ──────────────────────────────────────────
    if jobs.get("download_queue_processor", {}).get("enabled", True):
        interval_seconds = jobs.get("download_queue_processor", {}).get("interval_seconds", 30)
        try:
            from services.queue.queue_orchestrator import process_next_batch
            if scheduler.get_job("download_queue_processor") is None:
                scheduler.add_job(
                    process_next_batch,
                    trigger=IntervalTrigger(seconds=interval_seconds),
                    id="download_queue_processor",
                    name="Process download queue",
                    replace_existing=True,
                )
                logger.info("APScheduler: registered download_queue_processor (every %s s)", interval_seconds)
            else:
                logger.info("APScheduler: download_queue_processor already registered")
        except Exception as exc:
            logger.warning("APScheduler: failed to register download_queue_processor: %s", exc)

    # ── Missing releases scan ─────────────────────────────────────────────
    # Legacy daily behaviour: refresh the missing_releases cache so the
    # artist page / dashboard always reflect MusicBrainz's latest releases.
    try:
        from helpers.config_helpers import get_config as _get_config
        _feats = (_get_config() or {}).get("features", {}) or {}
        enabled = bool(_feats.get("daily_musicbrainz_release_scan_enabled", True))
        if enabled:
            interval_minutes = jobs.get("missing_releases_scan", {}).get("interval_minutes", 1440)
            from services.metadata.artist_scan_service import start_missing_release_scan
            if scheduler.get_job("missing_releases_scan") is None:
                scheduler.add_job(
                    start_missing_release_scan,
                    trigger=IntervalTrigger(minutes=interval_minutes),
                    id="missing_releases_scan",
                    name="MusicBrainz missing releases refresh",
                    replace_existing=True,
                )
                logger.info("APScheduler: registered missing_releases_scan (every %s min)", interval_minutes)
            else:
                logger.info("APScheduler: missing_releases_scan already registered")
    except Exception as exc:
        logger.warning("APScheduler: failed to register missing_releases_scan: %s", exc)


# ---------------------------------------------------------------------------
# Convenience: start/stop from Flask
# ---------------------------------------------------------------------------

def start_scheduler(app=None) -> BackgroundScheduler | None:
    """Start the APScheduler background scheduler.

    Can be called from Flask's ``before_first_request`` or from
    ``helpers/task_manager.initialize_app_services()``.
    """
    try:
        scheduler = get_scheduler()
        if not scheduler.running:
            scheduler.start()
            logger.info("APScheduler started")
        return scheduler
    except Exception as exc:
        logger.error("APScheduler failed to start: %s", exc)
        return None


def stop_scheduler() -> None:
    """Gracefully shut down the scheduler."""
    global _SCHEDULER
    if _SCHEDULER and _SCHEDULER.running:
        _SCHEDULER.shutdown(wait=False)
        _SCHEDULER = None
        logger.info("APScheduler shut down")
