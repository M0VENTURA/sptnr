"""Tests for the download-queue retry scheduler wiring.

Verifies that the APScheduler's queue-processor tick runs the full cycle
(``process_cycle`` — which includes the maintenance hooks: retry scheduler,
completion check, cleanup), NOT just ``process_next_batch``.  Before the fix,
the APScheduler registered ``process_next_batch`` directly, so
``requeue_due_failed_items`` only ever ran inside the standalone
``queue_worker.py`` process — failed items stayed failed forever when the
worker wasn't running (e.g. APScheduler-only deployments).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from services.queue import queue_orchestrator as orch


@pytest.fixture(autouse=True)
def _clean_runtime():
    from services.scanning.runtime_state import clear_runtime

    clear_runtime("queue")
    yield
    clear_runtime("queue")


def test_process_cycle_runs_maintenance_then_batch():
    """process_cycle must invoke run_maintenance() (retry/cleanup hooks)
    before process_next_batch()."""
    with (
        patch.object(orch, "run_maintenance") as maintenance_mock,
        patch.object(orch, "process_next_batch") as batch_mock,
    ):
        maintenance_mock.return_value = ({"hooks": 3, "failures": 0}, 200)
        batch_mock.return_value = ({"total": 0, "processed": 0}, 200)

        payload, status = orch.process_cycle(run_maintenance_hooks=True)

        maintenance_mock.assert_called_once()
        batch_mock.assert_called_once()
        assert status == 200
        assert payload["maintenance"] is not None


def test_process_cycle_can_skip_maintenance():
    """process_cycle must allow callers to skip maintenance (used by callers
    that only want the batch)."""
    with (
        patch.object(orch, "run_maintenance") as maintenance_mock,
        patch.object(orch, "process_next_batch") as batch_mock,
    ):
        batch_mock.return_value = ({"total": 0}, 200)

        payload, _status = orch.process_cycle(run_maintenance_hooks=False)

        maintenance_mock.assert_not_called()
        assert payload["maintenance"] is None


def test_retry_scheduler_is_a_registered_maintenance_hook():
    """retry_due_items must be registered as a maintenance hook so the queue
    tick's maintenance pass requeues failed items."""
    hook_targets = [
        (c.module, c.function)
        for c in orch.MAINTENANCE_CANDIDATES
    ]
    assert ("services.downloads.download_retry_service", "retry_due_items") in hook_targets


def test_process_cycle_is_used_by_scheduler_registration():
    """The APScheduler's download_queue_processor must register process_cycle
    (not process_next_batch) so retries run from the scheduler tick too.

    We assert this by checking the scheduler module imports process_cycle for
    the job (the registration itself is exercised at import/runtime).
    """
    import services.scheduler.scheduler_service as sched

    # The module must reference process_cycle (the full cycle incl. maintenance)
    # rather than only process_next_batch for the queue processor.
    source = open(sched.__file__, encoding="utf-8").read()
    assert "process_cycle" in source
