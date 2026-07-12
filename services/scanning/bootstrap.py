"""Boot helpers for scanning.

Provides the startup entry point for launching a boot scan during
application initialisation. The boot scan runs Navidrome import
in a non-blocking daemon thread.

Key Functions:
    - start_boot_scan(): Launch a background boot scan thread.

Architecture:
    Runs as a daemon thread so it does not block application startup.
    Errors are logged but do not prevent the application from starting.
"""

from __future__ import annotations

import logging
import threading

from services.scanning.pipeline import start_boot_navidrome_import


def start_boot_scan() -> None:
    """Launch boot scan in a non-blocking daemon thread."""
    def worker() -> None:
        try:
            logging.info("Boot scan starting")
            start_boot_navidrome_import()
        except Exception as exc:
            logging.error("Boot scan failed: %s", exc, exc_info=True)

    threading.Thread(
        target=worker,
        daemon=True,
        name="boot-scan-launcher"
    ).start()
