"""Regression tests for the Discogs throttle / retry stalls.

Reproduces the "N (of N) futures unfinished" scan symptom: when Discogs
returns a 429 with a long Retry-After, the shared throttle used to sleep
WHILE HOLDING the throttle lock, so every per-track worker blocked behind it
and the whole album's futures timed out past the 300s deadline.

Covers:
- ``throttle_discogs`` releases the lock before sleeping through a cooldown.
- ``_set_rate_limit_window`` caps a single 429 cooldown.
- the retry transport caps an uncapped ``Retry-After`` header.
"""

from __future__ import annotations

import threading
import time


class TestThrottleDiscogsReleasesLock:
    def test_lock_free_during_cooldown_sleep(self):
        import api_clients.discogs_http as dh

        dh._DISCOGS_RATE_LIMIT_UNTIL = time.time() + 2.0
        dh._DISCOGS_LAST_REQUEST_TIME = 0.0

        result = {"done": False}

        def _run():
            dh.throttle_discogs()
            result["done"] = True

        t = threading.Thread(target=_run)
        t.start()
        time.sleep(0.2)

        # While throttle_discogs is sleeping through the cooldown, the lock
        # must be acquirable — otherwise every other worker blocks behind it
        # and all per-track futures time out.
        acquired = dh._DISCOGS_THROTTLE_LOCK.acquire(timeout=0.5)
        if acquired:
            dh._DISCOGS_THROTTLE_LOCK.release()

        t.join(timeout=5)

        assert acquired is True
        assert result["done"] is True


class TestDiscogsCooldownCap:
    def test_set_rate_limit_window_caps_long_cooldown(self):
        import api_clients.discogs_http as dh

        dh._DISCOGS_RATE_LIMIT_UNTIL = 0.0
        dh._set_rate_limit_window(300.0)

        # A single 429 must not arm a cooldown beyond the cap.
        assert dh._DISCOGS_RATE_LIMIT_UNTIL <= time.time() + dh._DISCOGS_MAX_COOLDOWN + 0.01

    def test_set_rate_limit_window_keeps_short_cooldown(self):
        import api_clients.discogs_http as dh

        dh._DISCOGS_RATE_LIMIT_UNTIL = 0.0
        dh._set_rate_limit_window(5.0)

        assert dh._DISCOGS_RATE_LIMIT_UNTIL <= time.time() + 5.0 + 0.01


class TestRetryTransportCapsRetryAfter:
    def test_uncapped_retry_after_does_not_stall(self):
        import httpx
        from api_clients import http_utils

        def _resp(status):
            r = httpx.Response(status, request=httpx.Request("GET", "http://x"))
            r.headers["Retry-After"] = "300"
            return r

        class _FakeTransport:
            def __init__(self, responses):
                self._responses = list(responses)
                self.calls = 0

            def handle_request(self, request):
                self.calls += 1
                return self._responses.pop(0)

        transport = http_utils._RetryTransport(retries=1, backoff=0.1)
        fake = _FakeTransport([_resp(429), _resp(200)])
        transport._transport = fake

        # Shrink the cap so the test is fast; the point is the 300s header is
        # NOT honoured verbatim.
        original_cap = http_utils._MAX_RETRY_AFTER
        http_utils._MAX_RETRY_AFTER = 0.05
        try:
            start = time.time()
            resp = transport.handle_request(httpx.Request("GET", "http://x"))
            elapsed = time.time() - start
        finally:
            http_utils._MAX_RETRY_AFTER = original_cap

        assert fake.calls == 2
        assert resp.status_code == 200
        # Bounded well under the 300s header (and under the 300s album deadline).
        assert elapsed < 5.0
