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
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Optional

from helpers.normalization_service import normalise_result
from db.repositories import queue as queue_repository
from helpers.response_helpers import _ok, _fail

logger = logging.getLogger(__name__)


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

DEFAULT_FAILURE_STATUS = "failed"

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
    """
    queue_id = _item_id(item)

    if not queue_id:
        return None

    update_fields: dict[str, Any] = {
        "status": DEFAULT_FAILURE_STATUS,
        "failure_reason": reason,
    }

    if clear_file_path:
        update_fields["file_path"] = None

    try:
        return _as_dict(queue_repository.update_queue_item(queue_id, **update_fields))
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


def process_next_batch(limit: int = 50) -> tuple[dict[str, Any], int]:
    """
    Process a batch of ready queue items.

    This is the main entrypoint called by queue_processing_service and queue_worker.
    """
    started_at = time.time()

    try:
        limit = max(1, int(limit or 50))
    except (TypeError, ValueError):
        limit = 50

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

        items = _get_ready_items(limit)

        processed = 0
        succeeded = 0
        skipped = 0
        failed = 0

        results: list[dict[str, Any]] = []

        for item in items:
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

    except Exception as exc:
        logger.exception("process_next_batch failed")
        return _fail(str(exc), 500)


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
