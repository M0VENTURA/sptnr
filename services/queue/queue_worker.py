"""Background queue worker process.

Standalone process entry point for the background queue processing loop.

Features:
    - Signal-based graceful shutdown (SIGTERM/SIGINT).
    - Periodic processing cycles via ``queue_orchestrator.process_cycle``.
    - Error recovery and automatic restart logic.

Architecture:
    Designed to run as a standalone process (not a thread), typically
    managed by systemd or Docker. Uses signal handlers for clean
    shutdown without data loss.

    Call Chain:
        queue_worker \u2192 queue_orchestrator.process_cycle()
            \u2192 queue_processing_service
"""

from __future__ import annotations

import logging
import signal
import sys
import time
from typing import Any

# ✅ Use orchestrator directly (best runtime entrypoint)
from services.queue.queue_orchestrator import process_cycle
from helpers.config_helpers import get_queue_worker_config

logger = logging.getLogger(__name__)

_SHOULD_STOP = False

# Load worker configuration
_worker_cfg = get_queue_worker_config()


# =============================================================================
# SIGNAL HANDLING
# =============================================================================

def _handle_signal(signum: int, frame: Any):
    global _SHOULD_STOP
    _SHOULD_STOP = True
    logger.info("Queue worker received stop signal: %s", signum)


# =============================================================================
# LOGGING SETUP
# =============================================================================

def _configure_logging():
    """
    Ensure logs appear in systemd journal.
    Falls back safely if your logging_config isn't available.
    """
    try:
        from helpers.logging_config import setup_logging
        setup_logging("QueueWorker")
    except Exception:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )


# =============================================================================
# WORKER LOOP
# =============================================================================


def run(interval: int | None = None, batch_size: int | None = None) -> None:
    # Resolve defaults from config
    effective_interval: int = _worker_cfg["interval_seconds"] if interval is None else interval
    effective_batch: int = _worker_cfg["batch_size"] if batch_size is None else batch_size
    logger.info(
        "Popularr Queue Worker started (interval=%ss batch_size=%s)",
        effective_interval,
        effective_batch,
    )

    loop_count = 0

    while not _SHOULD_STOP:
        loop_count += 1

        try:
            start_time = time.monotonic()

            payload, status = process_cycle(
                batch_size=effective_batch,
                run_maintenance_hooks=True,
            )

            duration = round(time.monotonic() - start_time, 3)

            if status >= 500:
                logger.error(
                    "Queue cycle failed (loop=%s duration=%ss): %s",
                    loop_count,
                    duration,
                    payload,
                )
            elif status >= 400:
                logger.warning(
                    "Queue cycle warning (loop=%s duration=%ss): %s",
                    loop_count,
                    duration,
                    payload,
                )
            else:
                logger.info(
                    "Queue cycle success (loop=%s duration=%ss): processed=%s success=%s failed=%s skipped=%s",
                    loop_count,
                    duration,
                    payload.get("processed"),
                    payload.get("succeeded"),
                    payload.get("failed"),
                    payload.get("skipped"),
                )

        except Exception:
            logger.exception("Queue worker cycle crashed")

        # ✅ Safe sleep with interruptibility
        for _ in range(max(1, effective_interval)):
            if _SHOULD_STOP:
                break
            time.sleep(1)

    logger.info("[QUEUE_WORKER] Queue worker stopped cleanly after %s cycles", loop_count)


# =============================================================================
# ENTRYPOINT
# =============================================================================

if __name__ == "__main__":
    _configure_logging()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        interval = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    except (TypeError, ValueError):
        interval = 30

    run(interval=interval)