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
import time
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

from db.engine import get_engine

logger = logging.getLogger(__name__)


class ResilientSQLAlchemyJobStore(SQLAlchemyJobStore):
    """SQLAlchemy job store that survives transient PostgreSQL failures.

    APScheduler's stock ``SQLAlchemyJobStore`` keeps a persistent session on a
    pooled connection.  When PostgreSQL restarts or drops idle connections,
    that session's connection goes stale and every subsequent ``update_job`` /
    ``get_due_jobs`` raises ``OperationalError`` with no recovery — the
    scheduler effectively stops.

    This subclass retries transient DB errors a few times and disposes the
    engine's pool so the next attempt checks out a fresh connection.
    """

    _RETRIES = 3
    _RETRY_DELAY = 1.0

    def _execute(self, fn, *args, **kwargs):
        last_error: Exception | None = None
        for attempt in range(self._RETRIES):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                last_error = exc
                if not self._is_transient(exc):
                    raise
                logger.warning(
                    "[scheduler] Transient DB error in jobstore (attempt %s/%s): %s",
                    attempt + 1,
                    self._RETRIES,
                    exc,
                )
                try:
                    self.engine.dispose()
                except Exception:
                    pass
                if attempt < self._RETRIES - 1:
                    time.sleep(self._RETRY_DELAY * (attempt + 1))
        if last_error is not None:
            raise last_error
        return None

    @staticmethod
    def _is_transient(exc: Exception) -> bool:
        """Return True for connection-level DB errors worth retrying."""
        if exc is None:
            return False
        try:
            from sqlalchemy.exc import OperationalError as SAOperationalError
            from sqlalchemy.exc import InterfaceError as SAInterfaceError
            if isinstance(exc, (SAOperationalError, SAInterfaceError)):
                return True
        except Exception:
            pass
        # Also treat the driver-level error (psycopg2) as transient.
        orig = getattr(exc, "orig", None)
        if orig is not None:
            cls_name = type(orig).__name__.lower()
            if any(k in cls_name for k in ("operationalerror", "interfaceerror", "connectionerror")):
                return True
        return False

    # ── Override the job-store operations used by the scheduler loop ──────
    def add_job(self, job):
        return self._execute(super().add_job, job)

    def update_job(self, job):
        return self._execute(super().update_job, job)

    def remove_job(self, job_id):
        return self._execute(super().remove_job, job_id)

    def remove_all_jobs(self):
        return self._execute(super().remove_all_jobs)

    def lookup_job(self, job_id):
        return self._execute(super().lookup_job, job_id)

    def get_due_jobs(self, now):
        return self._execute(super().get_due_jobs, now)

    def get_next_run_time(self):
        return self._execute(super().get_next_run_time)

    def get_all_jobs(self):
        return self._execute(super().get_all_jobs)

    def get_jobs(self, jobstore_alias=None):
        return self._execute(super().get_jobs, jobstore_alias)


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

    # Use SQLAlchemy job store for persistence across restarts, wrapped so
    # stale PostgreSQL connections (e.g. after a DB restart) don't kill the
    # scheduler loop permanently.
    jobstores: dict[str, Any] = {}
    try:
        engine = get_engine()
        jobstores["default"] = ResilientSQLAlchemyJobStore(engine=engine)
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

    # ── Upcoming releases: daily Wikipedia scrape + weekly MB refresh ─────
    # Both run under the unified ``features.upcoming_releases_scan_enabled``
    # toggle (legacy ``daily_musicbrainz_release_scan_enabled`` honoured as a
    # fallback for installs that only ever set the old key).
    try:
        from helpers.config_helpers import get_feature
        scan_enabled = get_feature("upcoming_releases_scan_enabled", None)
        if scan_enabled is None:
            scan_enabled = get_feature("daily_musicbrainz_release_scan_enabled", True)
        scan_enabled = bool(scan_enabled)
    except Exception:
        scan_enabled = True

    if scan_enabled:
        # Daily 03:00 — scrape the Wikipedia sources + purge stale rows.
        try:
            from services.upcoming_releases.wikipedia_scraper_service import scrape as scrape_wikipedia, purge_stale_upcoming_releases
            if scheduler.get_job("upcoming_wikipedia_scrape") is None:
                def _run_wikipedia_scrape() -> None:
                    try:
                        scrape_wikipedia()
                    finally:
                        purge_stale_upcoming_releases()

                scheduler.add_job(
                    _run_wikipedia_scrape,
                    trigger=CronTrigger(hour=3, minute=0),
                    id="upcoming_wikipedia_scrape",
                    name="Upcoming releases: Wikipedia scrape + stale purge",
                    replace_existing=True,
                )
                logger.info("APScheduler: registered upcoming_wikipedia_scrape (03:00 daily)")
            else:
                logger.info("APScheduler: upcoming_wikipedia_scrape already registered")
        except Exception as exc:
            logger.warning("APScheduler: failed to register upcoming_wikipedia_scrape: %s", exc)

        # Weekly Sunday 04:00 — MusicBrainz release-groups for the collection.
        try:
            from services.upcoming_releases.musicbrainz_fetcher_service import fetch_musicbrainz_upcoming_releases
            if scheduler.get_job("upcoming_musicbrainz_scan") is None:
                scheduler.add_job(
                    fetch_musicbrainz_upcoming_releases,
                    trigger=CronTrigger(day_of_week="sun", hour=4, minute=0),
                    id="upcoming_musicbrainz_scan",
                    name="Upcoming releases: MusicBrainz collection refresh",
                    replace_existing=True,
                )
                logger.info("APScheduler: registered upcoming_musicbrainz_scan (Sunday 04:00)")
            else:
                logger.info("APScheduler: upcoming_musicbrainz_scan already registered")
        except Exception as exc:
            logger.warning("APScheduler: failed to register upcoming_musicbrainz_scan: %s", exc)


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
