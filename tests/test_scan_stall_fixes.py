"""Regression tests for the 8-hour popularity-scan stall cascade + startup crash.

The "N (of N) futures unfinished" cascade and the letter-scan dict crash had
several root causes, all covered here:

1. **Startup crash**: ``POST /api/scan/from-artist`` passed the progress
   ``extra`` dict POSITIONALLY into ``write_progress_with_current_artist``'s
   ``current_artist`` parameter → psycopg2 "can't adapt type 'dict'" on the
   VARCHAR column → the scan thread never started (letter-initiated scans
   from the artist page).

2. **Rate-limit lock-while-sleep**: ``APIRateLimiter.throttle_*`` slept
   UNDER the provider lock, so 4 concurrent scan workers serialised on the
   same provider — the per-track work could never finish within the album
   budget, and every album after the first stall dropped ALL of its tracks.

3. **No per-track wall-clock timeout**: a stuck worker (rate-limited to
   death) held its semaphore slot for the full 600s album budget, starving
   the other tracks.

4. **Post-singles/cover resource-exhaustion guard**: once an album's track
   workers were badly starved, the follow-up serial enrichment + cover
   passes only deepened the stall — they now skip when >=50% of the album's
   track workers failed/stalled.
"""

from __future__ import annotations

import threading
import time

import pytest


# ---------------------------------------------------------------------------
# 1. scan_state: dict-in-current_artist must not crash the write
# ---------------------------------------------------------------------------

class TestWriteProgressCurrentArtistCoercion:
    def test_extra_current_artist_as_dict_is_coerced(self, db_session):
        """A dict passed via ``extra`` (the letter-scan caller bug) must be
        coerced to a display string, NOT crash with psycopg2
        'can't adapt type dict'."""
        from sqlalchemy import text
        from db.engine import db_session as _db_session
        from services.scanning.scan_state import write_progress_with_current_artist

        with _db_session() as session:
            session.execute(text(
                "CREATE TABLE IF NOT EXISTS scan_states ("
                "scan_type TEXT PRIMARY KEY, is_running BOOLEAN, status TEXT, "
                "stop_requested BOOLEAN, current_artist TEXT, "
                "last_scanned_artist TEXT, extra_data TEXT, updated_at TEXT)"
            ))

        # This is EXACTLY what the old route did — the dict landed in
        # ``current_artist`` (4th positional arg) and crashed.
        write_progress_with_current_artist(
            "popularity_scan_progress.json",
            "popularity_scan",
            True,
            {
                "status": "starting",
                "resume_from": "Abba",
                "current_artist": "Abba",
                "processed_artists": 0,
                "total_artists": 0,
                "percent_complete": 0,
            },
        )

        # The row was written (no crash) and current_artist is a STRING.
        from services.scanning.scan_state import read_progress_file
        state = read_progress_file("popularity_scan_progress.json")
        assert state.get("is_running") is True
        assert isinstance(state.get("current_artist"), str)
        assert "Abba" in state["current_artist"]

    def test_positional_string_current_artist_still_works(self, db_session):
        """A plain string ``current_artist`` (all other callers) is unchanged."""
        from sqlalchemy import text
        from db.engine import db_session as _db_session
        from services.scanning.scan_state import write_progress_with_current_artist

        with _db_session() as session:
            session.execute(text(
                "CREATE TABLE IF NOT EXISTS scan_states ("
                "scan_type TEXT PRIMARY KEY, is_running BOOLEAN, status TEXT, "
                "stop_requested BOOLEAN, current_artist TEXT, "
                "last_scanned_artist TEXT, extra_data TEXT, updated_at TEXT)"
            ))

        write_progress_with_current_artist(
            "popularity_scan_progress.json",
            "popularity_scan",
            True,
            current_artist="Amorphis",
            extra={"status": "running", "percent_complete": 50},
        )
        from services.scanning.scan_state import read_progress_file
        state = read_progress_file("popularity_scan_progress.json")
        assert state["current_artist"] == "Amorphis"
        assert state.get("percent_complete") == 50


class TestScanFromArtistRouteUsesExtraKwarg:
    async def test_route_no_longer_passes_dict_positionally(self, app, client, monkeypatch):
        """The letter/artist scan route must pass the progress dict via
        ``extra=`` so ``current_artist`` stays a plain string."""
        from routes.scan_routes import popularity as pop_route

        source = open(pop_route.__file__, encoding="utf-8").read()
        # The route must use the keyword form (current_artist=artist) and
        # put the dict in extra=.
        assert "current_artist=artist" in source
        assert "extra={" in source


# ---------------------------------------------------------------------------
# 2. Rate limiter: sleep OUTSIDE the lock
# ---------------------------------------------------------------------------

class TestRateLimiterSleepsOutsideLock:
    def test_throttle_musicbrainz_sleeps_outside_lock(self, monkeypatch):
        import services.infrastructure.api_rate_limiter as arl

        limiter = arl.APIRateLimiter(state_file="/dev/null")
        limiter.state["musicbrainz_last_request"] = 0.0

        # Force a 0.3s wait (the real MIN interval is 1.0; shrink it so the
        # test is fast) — the sleep must NOT hold ``_mb_lock``.
        monkeypatch.setattr(arl, "MUSICBRAINZ_MIN_INTERVAL", 0.3)

        result = {"done": False}

        def _run():
            limiter.throttle_musicbrainz()
            result["done"] = True

        t = threading.Thread(target=_run)
        t.start()
        time.sleep(0.1)

        # While the worker is sleeping, the lock must be acquirable —
        # otherwise 4 scan workers serialise on it (the "N of N futures
        # unfinished" cascade).
        acquired = limiter._mb_lock.acquire(timeout=0.5)
        if acquired:
            limiter._mb_lock.release()
        t.join(timeout=5)

        assert acquired is True
        assert result["done"] is True

    def test_throttle_lastfm_sleeps_outside_lock(self, monkeypatch):
        import services.infrastructure.api_rate_limiter as arl

        limiter = arl.APIRateLimiter(state_file="/dev/null")
        limiter.state["lastfm_last_request"] = 0.0
        monkeypatch.setattr(arl, "LASTFM_RATE_LIMIT_PER_SECOND", 0.3)

        result = {"done": False}

        def _run():
            limiter.throttle_lastfm()
            result["done"] = True

        t = threading.Thread(target=_run)
        t.start()
        time.sleep(0.1)
        acquired = limiter._lastfm_lock.acquire(timeout=0.5)
        if acquired:
            limiter._lastfm_lock.release()
        t.join(timeout=5)

        assert acquired is True
        assert result["done"] is True

    def test_throttle_listenbrainz_sleeps_outside_lock(self, monkeypatch):
        import services.infrastructure.api_rate_limiter as arl

        limiter = arl.APIRateLimiter(state_file="/dev/null")
        limiter.state["listenbrainz_last_request"] = 0.0
        monkeypatch.setattr(arl, "LISTENBRAINZ_MIN_INTERVAL", 0.3)

        result = {"done": False}

        def _run():
            limiter.throttle_listenbrainz()
            result["done"] = True

        t = threading.Thread(target=_run)
        t.start()
        time.sleep(0.1)
        acquired = limiter._listenbrainz_lock.acquire(timeout=0.5)
        if acquired:
            limiter._listenbrainz_lock.release()
        t.join(timeout=5)

        assert acquired is True
        assert result["done"] is True


# ---------------------------------------------------------------------------
# 3. Scan runner: per-track wall-clock timeout + resource-exhaustion guard
# ---------------------------------------------------------------------------

class TestScanRunnerStallGuards:
    def test_runner_defines_bounded_track_worker(self):
        """The scan runner must have the bounded per-track worker (wall-clock
        cap) wired into the chunked submit path and the single-threaded
        fallback — the guard that stops one stuck track from burning the
        whole album budget."""
        import services.popularity.scan_stage_runner as srr

        source = open(srr.__file__, encoding="utf-8").read()
        assert "_run_track_job_bounded" in source
        assert "_run_skip_job_bounded" in source
        # The chunked submit path must use the bounded runner.
        assert "_pool.submit(_submit_chunked, job)" in source
        # Both the pool path and the serial fallback route through it.
        assert "_run_track_job_bounded(job) for job in _track_jobs" in source
        assert "_run_skip_job_bounded(job) for job in _skip_jobs" in source

    def test_post_singles_enrichment_guard_present(self):
        """When >=50% of an album's track workers failed/stalled, the
        post-singles enrichment and cover detection must be skipped so the
        next album's workers get the rate-limit budget instead of yet
        another serial consumer."""
        import services.popularity.scan_stage_runner as srr

        source = open(srr.__file__, encoding="utf-8").read()
        assert "_track_failure_ratio" in source
        assert "_track_failure_ratio >= 0.5" in source
        assert "Skipping post-singles enrichment" in source
        assert "Skipping cover detection" in source

    def test_album_stall_heartbeat_present(self):
        """The album collector must emit a per-minute heartbeat listing the
        in-flight tracks so a stalled album is diagnosable in real time
        instead of 8 hours of silence before the deadline."""
        import services.popularity.scan_stage_runner as srr

        source = open(srr.__file__, encoding="utf-8").read()
        assert "still waiting on" in source
        assert "_heartbeat_interval" in source


# ---------------------------------------------------------------------------
# 4. Scheduler: queue processor must not block the event loop
# ---------------------------------------------------------------------------

class TestQueueProcessorNonBlocking:
    def test_scheduler_spawns_daemon_thread(self):
        """The APScheduler download-queue tick must run ``process_cycle`` on
        a daemon thread so a long maintenance pass never blocks the web
        worker's event loop and starves the popularity scan."""
        import services.scheduler.scheduler_service as sched

        source = open(sched.__file__, encoding="utf-8").read()
        assert "_download_queue_processor_tick" in source
        assert 'daemon=True' in source
        # The cycle is spawned, not invoked inline.
        assert ".start()" in source
