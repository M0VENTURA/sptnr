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


# ---------------------------------------------------------------------------
# 5. Discogs artist-ID lock must NOT be held across the network call
# ---------------------------------------------------------------------------

class TestDiscogsArtistIdLockNotHeldAcrossIO:
    """Regression test for the 3-hour scan freeze after "Prefetch complete".

    ``_fetch_discogs_artist_id`` used to hold the module-global
    ``_discogs_artist_id_lock`` while calling the Discogs API.  A Discogs
    request can sleep up to 60s per 429 cooldown plus retries, so when the
    bounded album-enrichment thread exceeded its budget it was abandoned
    MID-REQUEST while STILL HOLDING the lock — every later caller (track
    workers, subsequent albums) then blocked on ``with
    _discogs_artist_id_lock:`` forever, and the scan sat silent for hours
    (the Postgres checkpoints kept firing because the DB itself was idle).
    """

    def test_network_call_occurs_outside_lock(self, monkeypatch):
        """While the Discogs lookup is in flight, another thread must be
        able to acquire the lock — proving the call is not made under it."""
        import services.popularity.stages.album_stage as album_stage

        # Fresh cache → guaranteed miss → forces the network path.
        monkeypatch.setattr(album_stage, "_discogs_artist_id_cache", {})
        monkeypatch.setattr(album_stage, "_discogs_artist_id_lock", threading.Lock())

        in_flight = threading.Event()
        release = threading.Event()
        started = {"v": False}
        lock_acquired = {"v": False}

        def _fake_get_artist_id(*args, **kwargs):
            started["v"] = True
            in_flight.set()
            # Simulate a slow/hung Discogs request (cooldown + retries).
            release.wait(timeout=5)
            return "12345"

        class _FakeClient:
            def get_artist_id(self, *args, **kwargs):
                return _fake_get_artist_id(*args, **kwargs)

        # The function imports DiscogsHttpClient and get_config at call
        # time — patch them in their source modules.
        import api_clients.discogs_http as discogs_http_module
        monkeypatch.setattr(discogs_http_module, "DiscogsHttpClient", lambda token: _FakeClient())
        import helpers.config_helpers as config_helpers_module
        monkeypatch.setattr(config_helpers_module, "get_config", lambda: {
            "api_integrations": {"discogs": {"enabled": True, "token": "tok"}}
        })

        def _caller():
            album_stage._fetch_discogs_artist_id(
                "Test Artist", None, {},
            )

        t = threading.Thread(target=_caller)
        t.start()
        assert in_flight.wait(timeout=5), "Discogs lookup never started"

        # The lock MUST be acquirable while the request is in flight —
        # before the fix this blocked forever (abandoned thread holds it).
        lock_acquired["v"] = album_stage._discogs_artist_id_lock.acquire(timeout=2)
        if lock_acquired["v"]:
            album_stage._discogs_artist_id_lock.release()

        release.set()
        t.join(timeout=5)

        assert started["v"] is True
        assert lock_acquired["v"] is True, (
            "Discogs artist-ID lock was held across the network call — an "
            "abandoned thread can deadlock the whole scan."
        )

    def test_source_acquires_lock_before_network(self):
        """The function must do the cache check under the lock, then release
        it before the HTTP call — verify the source shape."""
        import services.popularity.stages.album_stage as album_stage

        source = open(album_stage.__file__, encoding="utf-8").read()
        # Extract the function body between its def and the next def.
        func_start = source.index("def _fetch_discogs_artist_id")
        func_end = source.index("def _fetch_musicbrainz_artist_id")
        body = source[func_start:func_end]

        # The first lock block must contain ONLY the cache read and must be
        # immediately followed (outside the lock) by the network call.
        first_with = body.index("with _discogs_artist_id_lock:")
        # Find the line after the first lock block's indented body: the next
        # line that starts at 8 spaces (the same indent as ``with``).
        after_with = body[first_with:]
        lines = after_with.splitlines()
        # line 0 = "with ...:", line 1 = the indented cache read.
        assert "with _discogs_artist_id_lock:" in lines[0]
        assert "_discogs_artist_id_cache.get(_cache_key" in lines[1]
        # Line 2 must be dedented back to the function-body indent (8 spaces)
        # — proving the lock was released before the network call.
        assert lines[2].startswith("        if not discogs_artist_id:")
        # The network call happens after the lock is released.
        assert "client.get_artist_id" in body
        # And there's a second lock block AFTER the network call (cache write).
        assert body.count("with _discogs_artist_id_lock:") == 2
        assert "_discogs_artist_id_cache[_cache_key]" in body
