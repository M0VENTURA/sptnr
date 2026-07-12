"""Background service initialisation bridge.

Provides ``initialize_app_services()`` for ``app.py`` to start background
worker services. Currently a placeholder — no services auto-start by default.

To add background services, import and start them here so ``app.py`` stays
clean of business logic.
"""


def initialize_app_services(app=None):
    """Start background services after the Flask app is created.

    Called once during app factory setup (app.py). Currently a placeholder
    that logs the event. Extend here to start queue workers, schedulers,
    or periodic sync loops.
    """
    import logging
    logger = logging.getLogger(__name__)
    logger.debug("[TASK_MANAGER] Background service initialisation complete (app=%s)", bool(app))