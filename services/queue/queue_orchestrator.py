"""Queue orchestration service.

Background-processing orchestration layer for the download queue.

Responsibilities:
    - Find queue items ready for processing.
    - Claim items safely (avoids double-processing).
    - Delegate to downloads/queue services.
    - Run lightweight periodic maintenance hooks.
    - Return consistent service-style responses.

Call Chain:
    ``queue_worker`` \u2192 ``queue_processing_service.process_next_batch()``
        \u2192 ``queue_orchestrator.process_next_batch()``

This module does NOT contain:
    - Direct SQL (uses ``db.repositories.queue``).
    - Soulseek candidate scoring.
    - Metadata matching internals.
    - File-moving internals.
    - Flask route logic.
"""

from __future__ import annotations

import importlib
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Optional

from helpers.normalization_service import normalise_result
from db.repositories import queue as queue_repository
from helpers.response_helpers import _ok, _fail
from services.queue.queue_config import _SLSKD_INTER_ITEM_DELAY_SECONDS

logger = logging.getLogger(__name__)

# Maximum number of items allowed in the search/download pipeline at once.
# Dispatch is throttled to this cap so an album enqueue (tens of items at
# once) cannot burst slskd/file-storage with unlimited concurrent work.
# Overridable via ``QUEUE_MAX_IN_FLIGHT`` or ``queue.worker.max_in_flight``.
MAX_IN_FLIGHT = 15


# =============================================================================
# TYPES
# =============================================================================

ProcessorFunc = Callable[[dict[str, Any]], Any]
MaintenanceFunc = Callable[[], Any]


@dataclass(frozen=True)
class ProcessorCandidate:
    module: str
    function: str


@dataclass(frozen=True)
class MaintenanceCandidate:
    module: str
    function: str


# =============================================================================
# CONFIG
# =============================================================================

CLAIM_STATUS = "searching"

# These are terminal-ish statuses that should not be processed by the worker
# even if a repository query accidentally returns them.
DO_NOT_PROCESS_STATUSES = {
    "completed",
    "imported",
    "deleted",
    "failed",
    "unmatched",
    "in_collection",
    "matched",
}

# Preferred real processors, in migration-friendly order.
#
# Add the actual migrated function name here when your final processing service
# lands. This orchestrator will automatically use the first available function.
PROCESSOR_CANDIDATES: tuple[ProcessorCandidate, ...] = (
    ProcessorCandidate(
        "services.downloads.download_processing_service",
        "process_queue_item",
    ),
    ProcessorCandidate(
        "services.downloads.download_processing_service",
        "process_download_queue_item",
    ),
    ProcessorCandidate(
        "services.downloads.download_queue_service",
        "process_queue_item",
    ),
    ProcessorCandidate(
        "services.downloads.download_queue_service",
        "process_download_queue_item",
    ),
    ProcessorCandidate(
        "services.queue.queue_processing_worker_service",
        "process_queue_item",
    ),
)

# Optional periodic hooks. These are deliberately best-effort.
# Missing hooks are ignored.
MAINTENANCE_CANDIDATES: tuple[MaintenanceCandidate, ...] = (
    MaintenanceCandidate(
        "services.downloads.download_retry_service",
        "retry_due_items",
    ),
    MaintenanceCandidate(
        "services.downloads.download_scan_service",
        "check_completed_downloads",
    ),
    MaintenanceCandidate(
        "services.downloads.cleanup_engine_service",
        "cleanup_stale_downloads",
    ),
    MaintenanceCandidate(
        "services.downloads.download_scheduler_service",
        "run_due_tasks",
    ),
    MaintenanceCandidate(
        "services.queue.queue_cleanup_service",
        "cleanup_stuck_items",
    ),
)


# =============================================================================
# RESPONSE HELPERS
# =============================================================================

# =============================================================================
# DYNAMIC SERVICE RESOLUTION
# =============================================================================

def _load_callable(module_name: str, function_name: str) -> Callable[..., Any] | None:
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        logger.debug("Optional module unavailable: %s (%s)", module_name, exc)
        return None

    func = getattr(module, function_name, None)

    if not callable(func):
        logger.debug("Optional function unavailable: %s.%s", module_name, function_name)
        return None

    return func


def _resolve_processor() -> ProcessorFunc | None:
    """
    Return the first available migrated queue item processor.
    """
    for candidate in PROCESSOR_CANDIDATES:
        func = _load_callable(candidate.module, candidate.function)
        if callable(func):
            logger.debug(
                "Using queue processor: %s.%s",
                candidate.module,
                candidate.function,
            )
            return func

    return None


def _iter_maintenance_hooks() -> Iterable[MaintenanceFunc]:
    for candidate in MAINTENANCE_CANDIDATES:
        func = _load_callable(candidate.module, candidate.function)
        if callable(func):
            yield func


# =============================================================================
# ITEM NORMALISATION
# =============================================================================

def _as_dict(item: Mapping[str, Any] | Any) -> dict[str, Any]:
    """
    Convert repository rows into normal dictionaries.
    """
    if isinstance(item, dict):
        return dict(item)

    if hasattr(item, "keys"):
        try:
            return {key: item[key] for key in item.keys()}
        except Exception:
            pass

    try:
        return dict(item)
    except Exception:
        return {}





def _item_id(item: Mapping[str, Any]) -> Optional[int]:
    # ✅ Correct lookup without OR bug
    raw_id = item.get("id")
    if raw_id is None:
        raw_id = item.get("queue_id")

    if raw_id is None:
        return None

    # ✅ Type narrowing (fixes editor error)
    if isinstance(raw_id, int):
        return raw_id

    if isinstance(raw_id, str):
        raw_id = raw_id.strip()
        if not raw_id:
            return None

    try:
        return int(raw_id)
    except (TypeError, ValueError):
        return None


def _item_status(item: Mapping[str, Any]) -> str:
    return str(item.get("status") or "").strip().lower()


def _should_process(item: Mapping[str, Any]) -> bool:
    item_id = _item_id(item)

    # ✅ Correct check
    if item_id is None:
        return False

    status = _item_status(item)

    if status in DO_NOT_PROCESS_STATUSES:
        return False

    return True

# =============================================================================
# REPOSITORY ADAPTERS
# =============================================================================

def _get_ready_items(limit: int) -> list[dict[str, Any]]:
    """
    Fetch ready items from the repository.

    Required repository function:
    - get_ready_for_processing(limit)
    """
    items = queue_repository.get_ready_for_processing(limit) or []
    return [_as_dict(item) for item in items]


def _claim_item(item: Mapping[str, Any]) -> dict[str, Any] | None:
    """
    Claim an item before processing.

    Preferred repository functions:
    - claim_queue_item(queue_id, status=...)
    - claim_for_processing(queue_id, status=...)
    - update_queue_item(queue_id, status=...)

    This function intentionally avoids direct SQL.
    """
    queue_id = _item_id(item)

    if not queue_id:
        return None

    claim_func = getattr(queue_repository, "claim_queue_item", None)
    if callable(claim_func):
        claimed = claim_func(queue_id, status=CLAIM_STATUS)
        return _as_dict(claimed) if claimed else None

    claim_func = getattr(queue_repository, "claim_for_processing", None)
    if callable(claim_func):
        claimed = claim_func(queue_id, status=CLAIM_STATUS)
        return _as_dict(claimed) if claimed else None

    update_func = getattr(queue_repository, "update_queue_item", None)
    if callable(update_func):
        updated = update_func(queue_id, status=CLAIM_STATUS)
        return _as_dict(updated) if updated else None

    logger.error("No repository claim/update function available for queue item %s", queue_id)
    return None


def _mark_failed(
    item: Mapping[str, Any],
    reason: str,
    *,
    clear_file_path: bool = False,
) -> dict[str, Any] | None:
    """
    Mark an item failed through the repository.

    Delegates to ``mark_failed`` (not a raw status update) so the retry
    scheduler's backoff field (``next_retry_at``) is always set — items
    failed without it are requeued on the very next maintenance cycle,
    producing a failed → queued → failed churn loop.
    """
    queue_id = _item_id(item)

    if not queue_id:
        return None

    try:
        failed = queue_repository.mark_failed(
            int(queue_id),
            str(reason or "Queue item processor failed"),
        )
        if failed and clear_file_path:
            queue_repository.update_queue_item(int(queue_id), file_path=None)
        return _as_dict(failed) if failed else None
    except Exception:
        logger.exception("Failed to mark queue item %s as failed", queue_id)
        return None


# =============================================================================
# PROCESSING
# =============================================================================

def process_queue_item(
    item: Mapping[str, Any],
    processor: ProcessorFunc | None = None,
) -> tuple[dict[str, Any], int]:
    """
    Process one queue item.

    This method:
    - validates the item
    - claims it
    - delegates actual work to the migrated processor
    - normalises the processor result
    """
    raw_item = _as_dict(item)
    queue_id = _item_id(raw_item)

    if not queue_id:
        return _fail("Queue item has no id", 400, item=raw_item)

    if not _should_process(raw_item):
        return _ok(
            skipped=True,
            queue_id=queue_id,
            reason="Item is not eligible for processing",
            status=_item_status(raw_item),
        )

    resolved_processor = processor or _resolve_processor()

    if resolved_processor is None:
        return _fail(
            "No migrated queue item processor is available",
            501,
            queue_id=queue_id,
            hint=(
                "Add a process_queue_item(item) function to "
                "services.downloads.download_processing_service or update "
                "PROCESSOR_CANDIDATES in services.queue.queue_orchestrator."
            ),
        )

    claimed = _claim_item(raw_item)

    if not claimed:
        return _fail(
            "Failed to claim queue item for processing",
            409,
            queue_id=queue_id,
        )

    try:
        result = resolved_processor(claimed)
        payload, status = normalise_result(result)

        payload.setdefault("queue_id", queue_id)

        if status >= 500:
            _mark_failed(claimed, payload.get("error") or "Queue item processor failed")

        return payload, status

    except Exception as exc:
        logger.exception("Queue item processing failed: %s", queue_id)
        _mark_failed(claimed, str(exc))
        return _fail(str(exc), 500, queue_id=queue_id)


def process_next_batch(
    limit: int = 50,
    *,
    inter_item_delay_seconds: float | None = None,
    max_in_flight: int | None = None,
    use_cycle_lock: bool = True,
) -> tuple[dict[str, Any], int]:
    """
    Process a batch of ready queue items.

    This is the main entrypoint called by queue_processing_service and queue_worker.

    Throttling / safety features:
        - ``inter_item_delay_seconds``: paced dispatch. The configured
          ``slskd.timeouts.inter_item_delay_seconds`` (default 5) is applied
          between items so a large enqueue does not fire every search/download
          back-to-back. Pass ``0`` to disable pacing (used by manual triggers).
        - ``max_in_flight``: only dispatch while fewer than this many items are
          in ``searching``/``downloading``, so storage/API load stays bounded.
        - ``use_cycle_lock``: cross-process mutual exclusion so the standalone
          worker and the APScheduler job never claim items simultaneously.
    """
    started_at = time.time()

    try:
        limit = max(1, int(limit or 50))
    except (TypeError, ValueError):
        limit = 50

    if inter_item_delay_seconds is None:
        inter_item_delay_seconds = _SLSKD_INTER_ITEM_DELAY_SECONDS

    if max_in_flight is None:
        max_in_flight = _resolve_max_in_flight()

    try:
        processor = _resolve_processor()

        if processor is None:
            return _fail(
                "No migrated queue item processor is available",
                501,
                hint=(
                    "Create services.downloads.download_processing_service."
                    "process_queue_item(item), or adjust PROCESSOR_CANDIDATES."
                ),
            )

        # Serialise cycles across processes (worker + APScheduler) so the
        # ready-items fetch, in-flight cap and claiming are all atomic w.r.t.
        # the other process — otherwise a second cycle can fetch the same
        # 'queued' rows a concurrent cycle is about to claim and re-process
        # them once the lock releases.
        def _run_batch() -> tuple[dict[str, Any], int]:
            items = _get_ready_items(limit)

            # Enforce the in-flight cap: never claim more items than free slots.
            available_slots = _available_in_flight_slots(max_in_flight)
            if len(items) > available_slots:
                logger.debug(
                    "[QUEUE] In-flight cap reached (%s in flight, max %s) — processing %s of %s ready item(s)",
                    max_in_flight - available_slots,
                    max_in_flight,
                    available_slots,
                    len(items),
                )
                items = items[:available_slots]

            if not items:
                return _ok(
                    total=0,
                    processed=0,
                    succeeded=0,
                    skipped=0,
                    failed=0,
                    throttled=max_in_flight - available_slots > 0,
                    elapsed_seconds=round(time.time() - started_at, 3),
                    results=[],
                )

            return _process_batch(
                items=items,
                processor=processor,
                inter_item_delay_seconds=inter_item_delay_seconds,
                started_at=started_at,
            )

        if use_cycle_lock:
            from services.queue.queue_lock import queue_cycle_lock

            with queue_cycle_lock() as acquired:
                if not acquired:
                    logger.info(
                        "[QUEUE] Another queue cycle holds the lock — skipping this batch to avoid concurrent dispatch"
                    )
                    return _ok(
                        total=0,
                        processed=0,
                        succeeded=0,
                        skipped=0,
                        failed=0,
                        throttled=True,
                        reason="Another queue cycle is running",
                        elapsed_seconds=round(time.time() - started_at, 3),
                        results=[],
                    )
                return _run_batch()

        return _run_batch()

    except Exception as exc:
        logger.exception("process_next_batch failed")
        return _fail(str(exc), 500)


def _resolve_max_in_flight() -> int:
    """Resolve the in-flight dispatch cap from env/config (with defaults)."""
    try:
        env_value = int(os.getenv("QUEUE_MAX_IN_FLIGHT") or 0)
        if env_value > 0:
            return env_value
    except (TypeError, ValueError):
        pass
    try:
        from helpers.config_helpers import get_queue_worker_config
        return int(get_queue_worker_config().get("max_in_flight") or MAX_IN_FLIGHT)
    except Exception:
        return MAX_IN_FLIGHT


def _available_in_flight_slots(max_in_flight: int) -> int:
    """Return how many more items may enter the search/download pipeline."""
    try:
        counts = queue_repository.get_queue_status_counts()
        in_flight = int(counts.get("searching", 0) or 0) + int(
            counts.get("downloading", 0) or 0
        )
        return max(0, int(max_in_flight) - in_flight)
    except Exception as exc:
        logger.debug("[QUEUE] In-flight slot check failed: %s", exc)
        return max_in_flight


def _process_batch(
    *,
    items: list[dict[str, Any]],
    processor: ProcessorFunc,
    inter_item_delay_seconds: float | None,
    started_at: float,
) -> tuple[dict[str, Any], int]:
    """Dispatch a pre-claimed batch, optionally pacing between items."""
    processed = 0
    succeeded = 0
    skipped = 0
    failed = 0

    results: list[dict[str, Any]] = []

    for index, item in enumerate(items):
        queue_id = _item_id(item)

        try:
            payload, status = process_queue_item(item, processor=processor)

            processed += 1

            if payload.get("skipped"):
                skipped += 1
            elif status < 400 and payload.get("success", True):
                succeeded += 1
            else:
                failed += 1

            results.append(
                {
                    "queue_id": queue_id,
                    "status_code": status,
                    "success": payload.get("success", status < 400),
                    "skipped": bool(payload.get("skipped")),
                    "error": payload.get("error"),
                }
            )

        except Exception as exc:
            logger.exception("Unhandled batch item failure")
            failed += 1

            if queue_id:
                _mark_failed(item, str(exc))

            results.append(
                {
                    "queue_id": queue_id,
                    "status_code": 500,
                    "success": False,
                    "error": str(exc),
                }
            )

        # Pace dispatch so an album enqueue cannot burst every search/download
        # back-to-back — the configured inter-item delay gives slskd breathing
        # room (and keeps file storage from being hammered in one go).
        if (
            inter_item_delay_seconds
            and inter_item_delay_seconds > 0
            and index < len(items) - 1
        ):
            time.sleep(float(inter_item_delay_seconds))

    elapsed_seconds = round(time.time() - started_at, 3)

    return _ok(
        total=len(items),
        processed=processed,
        succeeded=succeeded,
        skipped=skipped,
        failed=failed,
        elapsed_seconds=elapsed_seconds,
        results=results,
    )


# =============================================================================
# MAINTENANCE
# =============================================================================

def run_maintenance() -> tuple[dict[str, Any], int]:
    """
    Run optional queue/download maintenance hooks.

    Missing hooks are ignored. Failing hooks are reported but do not stop
    the worker.
    """
    results: list[dict[str, Any]] = []

    for hook in _iter_maintenance_hooks():
        hook_name = f"{hook.__module__}.{hook.__name__}"

        try:
            result = hook()
            payload, status = normalise_result(result)

            results.append(
                {
                    "hook": hook_name,
                    "status_code": status,
                    "success": payload.get("success", status < 400),
                    "error": payload.get("error"),
                }
            )

        except Exception as exc:
            logger.exception("Maintenance hook failed: %s", hook_name)
            results.append(
                {
                    "hook": hook_name,
                    "status_code": 500,
                    "success": False,
                    "error": str(exc),
                }
            )

    failures = [row for row in results if not row.get("success")]

    return _ok(
        hooks=len(results),
        failures=len(failures),
        results=results,
    )


def process_cycle(
    *,
    batch_size: int = 50,
    run_maintenance_hooks: bool = True,
) -> tuple[dict[str, Any], int]:
    """
    Full worker cycle:
    - optional maintenance
    - process next queue batch

    queue_worker can call this instead of process_next_batch if you want one
    cycle to include maintenance.
    """
    maintenance_payload: dict[str, Any] | None = None

    if run_maintenance_hooks:
        maintenance_payload, _ = run_maintenance()

    batch_payload, batch_status = process_next_batch(batch_size)

    batch_payload["maintenance"] = maintenance_payload

    return batch_payload, batch_status
