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
import threading
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
        # tenacity drives the retry/backoff loop that used to be hand-rolled
        # here; the engine pool is disposed before each retry so the next
        # attempt checks out a fresh connection after a PostgreSQL restart.
        from tenacity import (
            Retrying,
            retry_if_exception,
            stop_after_attempt,
            wait_exponential,
        )

        def _call():
            return fn(*args, **kwargs)

        def _before_retry(retry_state):
            logger.warning(
                "[scheduler] Transient DB error in jobstore (attempt %s): %s",
                retry_state.attempt_number,
                retry_state.outcome.exception() if retry_state.outcome else None,
            )
            try:
                self.engine.dispose()
            except Exception:
                pass

        return Retrying(
            stop=stop_after_attempt(self._RETRIES),
            wait=wait_exponential(
                multiplier=1.0, exp_base=2, min=self._RETRY_DELAY, max=5.0
            ),
            retry=retry_if_exception(self._is_transient),
            reraise=True,
            before_sleep=_before_retry,
        )(_call)

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

def _same_trigger(existing, trigger) -> bool:
    """Return True when *existing* job already uses the same trigger config.

    Comparing triggers avoids re-creating jobs on every config save: APScheduler
    re-creating a job resets its ``next_run_time`` to now+interval, which would
    silently shift a daily 03:00 cron to the time of day the config was saved.
    """
    if existing is None or existing.trigger is None or trigger is None:
        return False
    try:
        a = existing.trigger
        b = trigger
        if type(a) is not type(b):
            return False
        if isinstance(a, IntervalTrigger):
            return getattr(a, "interval", None) == getattr(b, "interval", None)
        if isinstance(a, CronTrigger):
            return getattr(a, "fields", None) == getattr(b, "fields", None)
        return str(a) == str(b)
    except Exception:
        return False


def _register_default_jobs(scheduler: BackgroundScheduler, cfg: dict[str, Any]) -> None:
    """Register built-in periodic jobs unless they exist or are disabled.

    Job gates and intervals consult the legacy ``watcher.*`` settings first
    (``auto_import_enabled``, ``auto_popularity_scan``,
    ``downloads_watcher_enabled``, ``scan_interval``) so the config page's
    Automation Services card actually controls the running scheduler.  The
    more specific ``jobs.<id>`` block overrides those defaults when present.
    Jobs whose trigger is unchanged are left untouched (their next-run time
    survives); changed/missing jobs are registered with ``replace_existing``
    so a config save (via ``reschedule_jobs_from_config``) re-applies
    interval/enabled changes dynamically — no app restart required.
    """
    jobs = cfg.get("jobs", {})
    watcher = cfg.get("watcher", {}) or {}

    def _enabled(job_id: str, watcher_key: str, default: bool = True) -> bool:
        try:
            return bool(jobs.get(job_id, {}).get("enabled", watcher.get(watcher_key, default)))
        except Exception:
            return default

    def _interval(job_id: str, field: str, default: float) -> float:
        try:
            return float(jobs.get(job_id, {}).get(field, default) or default)
        except Exception:
            return default

    def _put(job_id: str, name: str, trigger, replace: bool = True, **kwargs) -> None:
        """Add or replace a job only when missing or its trigger changed."""
        try:
            existing = scheduler.get_job(job_id)
            if existing is not None and not _same_trigger(existing, trigger):
                scheduler.remove_job(job_id)
                existing = None
            if existing is None:
                scheduler.add_job(
                    kwargs.pop("func"),
                    trigger=trigger,
                    id=job_id,
                    name=name,
                    replace_existing=replace,
                    **kwargs,
                )
                logger.info("APScheduler: registered %s", job_id)
        except Exception as exc:
            logger.warning("APScheduler: failed to register %s: %s", job_id, exc)

    def _remove_job(job_id: str) -> None:
        """Remove a job when its gate was turned off."""
        try:
            if scheduler.get_job(job_id) is not None:
                scheduler.remove_job(job_id)
                logger.info("APScheduler: removed %s (disabled in config)", job_id)
        except Exception as exc:
            logger.warning("APScheduler: failed to remove %s: %s", job_id, exc)

    # ── Library sync ──────────────────────────────────────────────────────
    # The "Auto Import" toggle maps to the library sync job: importing newly
    # detected songs from Navidrome IS the library sync.
    if _enabled("library_sync", "auto_import_enabled", True):
        interval_minutes = _interval("library_sync", "interval_minutes", 360)
        try:
            from services.library.library_sync_service import request_library_sync
            _put(
                "library_sync", "Library sync with Navidrome",
                IntervalTrigger(minutes=interval_minutes),
                func=request_library_sync,
            )
        except Exception as exc:
            logger.warning("APScheduler: failed to register library_sync: %s", exc)
    else:
        _remove_job("library_sync")

    # ── Popularity scan ───────────────────────────────────────────────────
    # The "Auto Popularity Scan" toggle gates the daily recalculation job.
    if _enabled("popularity_scan", "auto_popularity_scan", True):
        interval_minutes = _interval("popularity_scan", "interval_minutes", 1440)  # daily
        try:
            _put(
                "popularity_scan", "Popularity recalculation",
                IntervalTrigger(minutes=interval_minutes),
                func=_run_scheduled_popularity_scan,
            )
        except Exception as exc:
            logger.warning("APScheduler: failed to register popularity_scan: %s", exc)
    else:
        _remove_job("popularity_scan")

    # ── Download queue processor ──────────────────────────────────────────
    # The "Downloads Watcher" toggle maps to the queue processor (the
    # completion service runs inside its maintenance hooks); the "Scan
    # Interval" setting becomes the processor tick.
    if _enabled("download_queue_processor", "downloads_watcher_enabled", True):
        # A single Soulseek batch can outlast the tick interval — keep the
        # interval conservative and never stack overlapping runs (coalesce
        # missed ticks into one) so the orchestrator stops logging
        # "maximum number of running instances reached" / lock contention.
        interval_seconds = _interval(
            "download_queue_processor",
            "interval_seconds",
            float(watcher.get("scan_interval", 60) or 60),
        )
        try:
            from services.queue.queue_orchestrator import process_next_batch
            _put(
                "download_queue_processor", "Process download queue",
                IntervalTrigger(seconds=interval_seconds),
                func=process_next_batch,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=30,
            )
        except Exception as exc:
            logger.warning("APScheduler: failed to register download_queue_processor: %s", exc)
    else:
        _remove_job("download_queue_processor")

    # ── Missing releases scan ─────────────────────────────────────────────
    # Legacy daily behaviour: refresh the missing_releases cache so the
    # artist page / dashboard always reflect MusicBrainz's latest releases.
    try:
        from helpers.config_helpers import get_config as _get_config
        _feats = (_get_config() or {}).get("features", {}) or {}
        enabled = bool(_feats.get("daily_musicbrainz_release_scan_enabled", True))
        if enabled:
            interval_minutes = _interval("missing_releases_scan", "interval_minutes", 1440)
            from services.metadata.artist_scan_service import start_missing_release_scan
            _put(
                "missing_releases_scan", "MusicBrainz missing releases refresh",
                IntervalTrigger(minutes=interval_minutes),
                func=start_missing_release_scan,
            )
        else:
            _remove_job("missing_releases_scan")
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
            _put(
                "upcoming_wikipedia_scrape", "Upcoming releases: Wikipedia scrape + stale purge",
                CronTrigger(hour=3, minute=0),
                func=_run_wikipedia_scrape_task,
            )
        except Exception as exc:
            logger.warning("APScheduler: failed to register upcoming_wikipedia_scrape: %s", exc)

        # Weekly Sunday 04:00 — MusicBrainz release-groups for the collection.
        try:
            from services.upcoming_releases.musicbrainz_fetcher_service import fetch_musicbrainz_upcoming_releases
            _put(
                "upcoming_musicbrainz_scan", "Upcoming releases: MusicBrainz collection refresh",
                CronTrigger(day_of_week="sun", hour=4, minute=0),
                func=fetch_musicbrainz_upcoming_releases,
            )
        except Exception as exc:
            logger.warning("APScheduler: failed to register upcoming_musicbrainz_scan: %s", exc)
    else:
        _remove_job("upcoming_wikipedia_scrape")
        _remove_job("upcoming_musicbrainz_scan")


def reschedule_jobs_from_config() -> dict[str, Any]:
    """Re-read config and re-apply job gates/intervals to the running scheduler.

    Called after a config save so the Automation Services (watcher) settings
    and scheduler jobs take effect immediately instead of on the next
    restart.  Idempotent — jobs keep their current triggers when nothing
    changed (``replace_existing`` only rewrites what differs in APScheduler's
    view).  Returns a small stats dict for logging.
    """
    stats: dict[str, Any] = {"jobs": 0, "error": None}
    try:
        from helpers.config_helpers import get_config
        cfg = get_config() or {}
        scheduler = get_scheduler()
        if not scheduler.running:
            scheduler.start()
            logger.info("APScheduler started (reschedule_jobs_from_config)")
        _register_default_jobs(scheduler, cfg)
        stats["jobs"] = len(list(scheduler.get_jobs() or []))
        logger.info("APScheduler: config re-applied — %s job(s) registered", stats["jobs"])
        return stats
    except Exception as exc:
        logger.error("APScheduler: reschedule_jobs_from_config failed: %s", exc, exc_info=True)
        return {**stats, "error": str(exc)}


def _run_wikipedia_scrape_task() -> None:
    """Scrape Wikipedia upcoming-releases sources, then purge stale rows.

    Module-level (not a closure) so APScheduler's persistent SQLAlchemy job
    store can serialize the callable by ``module:function`` reference — a
    local closure inside ``_register_default_jobs`` fails with "This Job
    cannot be serialized".
    """
    from services.upcoming_releases.wikipedia_scraper_service import (
        scrape as scrape_wikipedia,
        purge_stale_upcoming_releases,
    )
    try:
        scrape_wikipedia()
    finally:
        purge_stale_upcoming_releases()


def _run_scheduled_popularity_scan() -> None:
    """Run the scheduled popularity recalculation, guarded against overlap.

    Module-level (not a closure) so the persistent job store can serialize
    the callable.  A scheduled run must never overlap a manually started
    popularity/full scan — both write to the same ``popularity_scan``
    progress state, so whichever finishes first flips the shared row to
    "complete" while the other is still running, making the dashboard show
    the manual full scan as stopped mid-letter.  The guard consults both the
    shared DB scan state (multi-worker safe) and the in-process runtime
    registry, then records the runtime so the manual routes see it busy too.
    """
    from services.scanning.pipelines.popularity_pipeline import (
        is_popularity_scan_active,
        run_popularity_mode,
    )

    # Check the shared DB state BEFORE claiming the in-process runtime, so
    # the guard's own claim does not look like an already-running scan.
    if is_popularity_scan_active():
        logger.info("Skipping scheduled popularity scan — a popularity scan is already active")
        return

    from services.scanning.runtime_state import (
        clear_runtime,
        is_runtime_running,
        scan_lock,
        set_runtime,
    )

    with scan_lock:
        if is_runtime_running("popularity"):
            logger.info("Skipping scheduled popularity scan — a popularity scan is already running")
            return
        set_runtime("popularity", {"thread": threading.current_thread(), "type": "scheduled-popularity"})

    try:
        run_popularity_mode(mode="popularity")
    finally:
        clear_runtime("popularity")


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
