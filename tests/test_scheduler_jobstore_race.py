"""Regression tests for the multi-worker APScheduler duplicate-key race.

Each hypercorn worker boots its own scheduler over the SAME DB-backed job
store.  Two workers can both pass the existence check and both attempt the
INSERT for a job id — the loser hits the ``apscheduler_jobs_pkey`` unique
constraint (``duplicate key value violates unique constraint
apscheduler_jobs_pkey``) or the store's own ``ConflictingIdError``.
``ResilientSQLAlchemyJobStore.add_job`` must treat that as an upsert
(fall back to ``update_job``) rather than propagating the error, because
``_register_default_jobs`` always registers with ``replace_existing=True``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from apscheduler.jobstores.base import ConflictingIdError
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from sqlalchemy.exc import IntegrityError

from services.scheduler.scheduler_service import ResilientSQLAlchemyJobStore


def _make_store():
    store = ResilientSQLAlchemyJobStore.__new__(ResilientSQLAlchemyJobStore)
    store.engine = MagicMock()
    store._execute = lambda fn, *a, **kw: fn(*a, **kw)
    return store


def _make_job(job_id: str = "popularity_scan"):
    job = MagicMock()
    job.id = job_id
    return job


class TestJobStoreDuplicateKeyRace:
    def test_add_job_falls_back_to_update_on_conflicting_id(self):
        store = _make_store()
        job = _make_job()
        with (
            patch.object(
                SQLAlchemyJobStore, "add_job",
                side_effect=ConflictingIdError(job.id),
            ),
            patch.object(SQLAlchemyJobStore, "update_job") as update_mock,
        ):
            store.add_job(job)
        update_mock.assert_called_once_with(job)

    def test_add_job_falls_back_to_update_on_integrity_error(self):
        store = _make_store()
        job = _make_job()
        with (
            patch.object(
                SQLAlchemyJobStore, "add_job",
                side_effect=IntegrityError("stmt", {}, Exception("duplicate key value")),
            ),
            patch.object(SQLAlchemyJobStore, "update_job") as update_mock,
        ):
            store.add_job(job)
        update_mock.assert_called_once_with(job)

    def test_add_job_happy_path_does_not_update(self):
        store = _make_store()
        job = _make_job()
        with (
            patch.object(SQLAlchemyJobStore, "add_job", return_value="added") as add_mock,
            patch.object(SQLAlchemyJobStore, "update_job") as update_mock,
        ):
            result = store.add_job(job)
        assert result == "added"
        add_mock.assert_called_once_with(job)
        update_mock.assert_not_called()

    def test_add_job_propagates_other_errors(self):
        store = _make_store()
        job = _make_job()
        with (
            patch.object(
                SQLAlchemyJobStore, "add_job", side_effect=RuntimeError("boom"),
            ),
            patch.object(SQLAlchemyJobStore, "update_job") as update_mock,
        ):
            try:
                store.add_job(job)
            except RuntimeError:
                pass
            else:
                raise AssertionError("expected RuntimeError to propagate")
        update_mock.assert_not_called()
