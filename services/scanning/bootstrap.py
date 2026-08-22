"""Boot helpers for scanning.

Provides the startup entry point for launching a boot scan during
application initialisation. The boot scan runs Navidrome import
in a non-blocking daemon thread.
"""

from __future__ import annotations

import threading

import structlog

from services.scanning.pipeline import start_boot_navidrome_import

logger = structlog.get_logger(__name__)


def start_boot_scan() -> None:
    """Launch boot scan in a non-blocking daemon thread."""
    def worker() -> None:
        try:
            logger.info("Boot scan starting")
            start_boot_navidrome_import()
            logger.info("Boot scan completed successfully")
        except Exception as exc:
            logger.error("Boot scan failed", error=str(exc), exc_info=True)

    threading.Thread(
        target=worker,
        daemon=True,
        name="boot-scan-launcher",
    ).start()
