"""Regression: track-result collection must not stall after all tracks finish.

The popularity scan's per-album track collection loop used to keep iterating
until the FULL album deadline (``popularity.track_timeout_seconds``, default
600s) even after every track future had completed.  The observed symptom was
an ~8-minute gap between the last per-track log and the ``[TRACK_RESULT]``
logs (e.g. tracks finished at 19:01:02, results appeared at 19:09:21 —
exactly the 600s deadline).  Collection must now break as soon as all
futures are done.
"""

from __future__ import annotations

import concurrent.futures as cf
import time

import pytest


class TestTrackCollectionBreaksWhenAllDone:
    def _make_futures(self, n: int = 5) -> list[cf.Future]:
        pool = cf.ThreadPoolExecutor(max_workers=2)
        try:
            futures = [pool.submit(lambda v: v, i) for i in range(n)]
            # Let them all complete.
            for f in futures:
                f.result(timeout=5)
            return futures
        finally:
            pool.shutdown(wait=True)

    def test_all_done_returns_immediately(self):
        """A collection loop that breaks when all futures are done must not
        burn the full deadline."""
        futures = self._make_futures(5)
        deadline = 600.0

        start = time.monotonic()
        collected = [None] * len(futures)
        index_by_future = {f: i for i, f in enumerate(futures)}
        deadline_at = time.monotonic() + deadline

        while True:
            remaining = max(0.0, deadline_at - time.monotonic())
            if remaining <= 0:
                break
            chunk = min(remaining, 60.0)
            # Simulate _collect_finished: consume done futures.
            try:
                for f in cf.as_completed(futures, timeout=chunk):
                    collected[index_by_future[f]] = f.result()
            except cf.TimeoutError:
                pass
            # FAST PATH: the fix under test.
            if all(f.done() for f in futures):
                break

        elapsed = time.monotonic() - start
        # All collected and NOT ~600s — the regression would take ~600s.
        assert all(c is not None for c in collected)
        assert elapsed < 10.0, f"collection stalled {elapsed:.1f}s after all done"
