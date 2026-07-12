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
    for periodic tasks (library sync, popularity scan, queue processor).
    """
    try:
        from services.scheduler.scheduler_service import start_scheduler
        start_scheduler(app=app)
    except Exception as exc:
        logger.warning("Failed to start scheduler: %s", exc)

    logger.debug("[TASK_MANAGER] Background services initialised (app=%s)", bool(app))