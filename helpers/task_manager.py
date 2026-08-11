"""Background service initialisation bridge.

Provides ``initialize_app_services()`` for ``app.py`` to start background
worker services. Currently starts the APScheduler for periodic tasks.

To add background services, import and start them here so ``app.py`` stays
clean of business logic.
"""

import logging

logger = logging.getLogger(__name__)


def initialize_app_services(app=None):
    """Start background services after the Flask app is created.

    Called once during app factory setup (``app.py``). Starts APScheduler
    for periodic tasks (library sync, popularity scan, queue processor) and
    mirrors the Download Retry Scheduler's ``auto_start`` config into its
    runtime state so the config page shows Running after boot.
    """
    try:
        from services.scheduler.scheduler_service import start_scheduler
        start_scheduler(app=app)
    except Exception as exc:
        logger.warning("Failed to start scheduler: %s", exc)

    # Retry scheduler: keep the UI state in sync with the config.  The actual
    # retry work runs inside the queue worker's maintenance hooks (gated by
    # the same config), so this only mirrors the flag the config page polls.
    try:
        from services.downloads.download_scheduler_service import start_scheduler as _start_retry_scheduler
        from services.downloads.download_retry_service import _retry_scheduler_enabled
        if _retry_scheduler_enabled():
            _start_retry_scheduler()
            logger.info("[RETRY_SCHEDULER] Auto-start on boot: ENABLED")
    except Exception as exc:
        logger.warning("Failed to sync retry scheduler state: %s", exc)

    logger.debug("[TASK_MANAGER] Background services initialised (app=%s)", bool(app))