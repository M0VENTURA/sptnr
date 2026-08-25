"""HTTP utility helpers for API clients.

Provides a retry-capable httpx client factory and a custom transport
that handles TLS issues gracefully.

Replaced the old ``requests``/``urllib3``-based implementation with
``httpx`` for better connection pooling, HTTP/2 support, and async
compatibility.
"""

from __future__ import annotations

import datetime
import logging
import ssl
import time
from typing import Any

import httpx


# ---------------------------------------------------------------------------
# Connection-pool limits (shared session)
# ---------------------------------------------------------------------------

# Pool size / timeout for the shared HTTP session, which serves EVERY provider
# (MusicBrainz, Last.fm, ListenBrainz, Discogs, Navidrome, Wikipedia, search)
# concurrently from the scan's thread pool plus background workers.  The httpx
# defaults (pool_timeout=5s) made bursts fail with ``httpx.PoolTimeout``
# whenever the pool was momentarily saturated — the logs showed "(PoolTimeout)"
# on Navidrome getScanStatus/setRating/search3 and on the Wikipedia scraper
# during scans.
_POOL_MAX_CONNECTIONS = 200
_POOL_MAX_KEEPALIVE = 50
_POOL_TIMEOUT = 30.0

# Cap on a single Retry-After wait (seconds).  Matches the exponential-backoff
# ceiling below so a provider sending a long header (e.g. "Retry-After: 300")
# cannot stall a worker past the per-album track deadline — every per-track
# future would otherwise time out ("N (of N) futures unfinished").  A repeated
# 429/503 re-arms the wait, so capping only bounds the worst single pause.
_MAX_RETRY_AFTER = 60.0

# Total wall-clock budget for ONE request's retry waits (seconds).  Even with
# every wait capped at ``_MAX_RETRY_AFTER``, a provider that keeps answering
# 429 can re-arm the backoff for each retry — 3 retries × 60s = 180s of pure
# sleeping in a single worker, which eats the entire per-album budget when
# the scan runs 17+ tracks concurrently.  Tenacity's ``stop_after_attempt``
# only counts attempts, not elapsed time, so a hard cumulative cap is needed:
# once the sum of waits exceeds this, the request gives up and returns the
# last retryable response instead of sleeping through the scan.
_TOTAL_RETRY_WAIT_BUDGET = 40.0


def _build_pool_limits() -> httpx.Limits:
    """Build ``httpx.Limits`` for the shared session, httpx-version-safe.

    The ``pool_timeout`` keyword only exists on ``httpx.Limits`` since
    httpx 0.25.0 — older httpx raises ``TypeError`` at import time (which
    crashed the whole app on startup).  Fall back to the two always-supported
    keywords there; the larger connection counts are what resolve the pool
    exhaustion, the longer pool wait is a bonus on newer httpx.
    """
    try:
        return httpx.Limits(
            max_connections=_POOL_MAX_CONNECTIONS,
            max_keepalive_connections=_POOL_MAX_KEEPALIVE,
            pool_timeout=_POOL_TIMEOUT,
        )
    except TypeError:  # httpx < 0.25 — no pool_timeout kwarg
        return httpx.Limits(
            max_connections=_POOL_MAX_CONNECTIONS,
            max_keepalive_connections=_POOL_MAX_KEEPALIVE,
        )

logger = logging.getLogger(__name__)


def is_ssl_cert_error(exc: BaseException) -> bool:
    """True when the exception chain contains an SSL certificate failure.

    A certificate-verification failure is a DETERMINISTIC configuration
    error (missing CA bundle, expired cert, wrong host) — retrying it never
    helps, it only burns the retry budget (observed: ~40s per failed
    MusicBrainz call = 4 retries × exponential backoff + 10s timeout each).
    The scan then looks "stalled" with zero log output while every MB call
    silently retries to exhaustion.

    httpx/httpcore wrap the ``ssl.SSLCertVerificationError`` inside
    ``ConnectError`` chains WITHOUT preserving it as ``__cause__`` — the SSL
    detail survives only in the message text ("[SSL: CERTIFICATE_VERIFY_FAILED]
    ... unable to get local issuer certificate").  So both the exception-type
    check AND a message scan are used.
    """
    if isinstance(exc, ssl.SSLCertVerificationError):
        return True
    cause = getattr(exc, "__cause__", None)
    depth = 0
    while cause is not None and depth < 6:
        if isinstance(cause, ssl.SSLCertVerificationError):
            return True
        try:
            if "CERTIFICATE_VERIFY_FAILED" in str(cause):
                return True
        except Exception:
            pass
        cause = getattr(cause, "__cause__", None)
        depth += 1
    try:
        if "CERTIFICATE_VERIFY_FAILED" in str(exc):
            return True
    except Exception:
        pass
    return False


class _RetryTransport(httpx.BaseTransport):
    """Transport wrapper that adds retry logic on top of httpx's HTTPTransport.

    Retries on transient errors (connection drops, DNS failures, timeouts)
    and configurable HTTP status codes using exponential backoff.  The retry
    loop is driven by ``tenacity`` (``stop_after_attempt`` +
    ``wait_exponential``) instead of a hand-rolled counter.
    """

    def __init__(
        self,
        retries: int = 3,
        backoff: float = 1.0,
        status_forcelist: tuple[int, ...] = (429, 500, 502, 503, 504),
        verify: bool = True,
    ):
        self._retries = retries
        self._backoff = backoff
        self._status_forcelist = status_forcelist
        # Explicit pool limits — see ``_build_pool_limits`` (httpx-version-safe).
        self._transport = httpx.HTTPTransport(
            verify=verify,
            retries=0,
            limits=_build_pool_limits(),
        )

    def handle_request(
        self,
        request: httpx.Request,
    ) -> httpx.Response:
        from tenacity import (
            Retrying,
            retry_if_exception,
            stop_after_attempt,
        )

        class _RetryableStatus(Exception):
            """Marker raised when the peer returned a retryable status code."""

            def __init__(self, response: httpx.Response):
                super().__init__(f"Retryable HTTP status {response.status_code}")
                self.response = response

        class _RetryBudgetExceeded(Exception):
            """Marker raised when the cumulative retry-wait budget is exhausted.

            Raising a NON-retryable exception inside ``_wait`` makes tenacity
            stop immediately (its ``retry`` predicate returns False for this
            type), instead of falling back to a zero-delay retry.  The outer
            ``except _RetryableStatus`` then surfaces the last retryable
            response (or the raw exception if there was none).
            """

        def _is_retryable(exc: BaseException) -> bool:
            if isinstance(exc, _RetryableStatus):
                return True
            if isinstance(exc, _RetryBudgetExceeded):
                return False
            if is_ssl_cert_error(exc):
                # Certificate problems are config errors — retrying only
                # wastes the wait budget (each attempt sleeps then times out).
                return False
            return isinstance(
                exc,
                (httpx.ConnectError, httpx.RemoteProtocolError, httpx.TimeoutException),
            )

        # The last retryable status is returned as-is (legacy behaviour) — a
        # peer that keeps answering 503/429 should surface the final response
        # instead of a synthetic exception.
        last_status_response: httpx.Response | None = None

        def _attempt() -> httpx.Response:
            nonlocal last_status_response
            response = self._transport.handle_request(request)
            if response.status_code in self._status_forcelist:
                last_status_response = response
                raise _RetryableStatus(response)
            return response

        # Cumulative retry-wait budget: once the sum of all waits exceeds
        # ``_TOTAL_RETRY_WAIT_BUDGET`` the request stops retrying and returns
        # the last retryable response.  This caps the 429/503 storm — a peer
        # that keeps answering 429 could otherwise re-arm the backoff on every
        # retry (3 × capped 60s = 180s of pure sleeping in ONE worker), which
        # eats the whole per-album budget when a scan runs 17+ tracks
        # concurrently.  ``stop_after_attempt`` only counts attempts, not
        # elapsed wait time, so the budget is enforced here.
        _retry_waits: dict[str, float] = {"total": 0.0}

        def _wait(retry_state):
            """Wait before the next retry.

            Honors a ``Retry-After`` header on the last retryable response
            (429/503) when present — some APIs (MusicBrainz, Last.fm,
            Discogs) send an explicit seconds value or an HTTP-date.  Falls
            back to exponential backoff otherwise.  Each wait is capped at
            ``_MAX_RETRY_AFTER`` AND the cumulative sum is capped at
            ``_TOTAL_RETRY_WAIT_BUDGET`` so a sustained 429 storm can never
            stall a worker past the per-album track deadline.
            """
            exc = retry_state.outcome.exception()
            retry_after = None
            if isinstance(exc, _RetryableStatus):
                response = getattr(exc, "response", None)
                if response is not None:
                    retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    wait = min(float(retry_after), _MAX_RETRY_AFTER)
                except ValueError:
                    # HTTP-date form (e.g. "Wed, 21 Oct 2015 07:28:00 GMT").
                    from email.utils import parsedate_to_datetime
                    try:
                        when = parsedate_to_datetime(retry_after)
                        if when.tzinfo is None:
                            when = when.replace(tzinfo=datetime.timezone.utc)
                        wait = min(
                            max(0.0, (when - datetime.now(datetime.timezone.utc)).total_seconds()),
                            _MAX_RETRY_AFTER,
                        )
                    except Exception:
                        wait = min(self._backoff * (2 ** (retry_state.attempt_number - 1)), 60.0)
            else:
                # Exponential backoff (legacy default).
                n = retry_state.attempt_number  # 1-based (1 = first failed attempt)
                wait = min(self._backoff * (2 ** (n - 1)), 60.0)
            # Enforce the cumulative budget: once the sum of all waits exceeds
            # the cap, STOP retrying entirely — raising a non-retryable
            # sentinel makes tenacity give up immediately.  Returning 0.0
            # here (the previous behaviour) retried with ZERO delay, which
            # against an overloaded peer (429/503 storm) turned "back off
            # politely" into "hammer it as fast as possible" for the
            # remaining attempts — the opposite of the intent.
            if _retry_waits["total"] + wait > _TOTAL_RETRY_WAIT_BUDGET:
                logger.debug(
                    "[HTTP] retry wait budget exceeded for %s — giving up (cumulative %.1fs)",
                    request.url,
                    _retry_waits["total"],
                )
                raise _RetryBudgetExceeded()
            _retry_waits["total"] += wait
            logger.debug(
                "[HTTP] retrying %s — attempt %d, wait %.1fs (cumulative %.1fs)",
                request.url,
                retry_state.attempt_number,
                wait,
                _retry_waits["total"],
            )
            return wait

        retrying = Retrying(
            stop=stop_after_attempt(self._retries + 1),
            wait=_wait,
            retry=retry_if_exception(_is_retryable),
            reraise=True,
        )

        try:
            for attempt in retrying:
                with attempt:
                    return _attempt()
        except _RetryBudgetExceeded:
            # Budget exhausted: surface the last retryable response (or re-raise
            # the network error if the budget ran out before any status retry).
            if last_status_response is not None:
                return last_status_response
            raise
        except _RetryableStatus:
            if last_status_response is not None:
                return last_status_response
            raise
        raise httpx.TransportError("Maximum retries exceeded")

    def close(self) -> None:
        self._transport.close()

    @property
    def is_closed(self) -> bool:
        return self._transport.is_closed


MINIMUM_TLS_VERSION = ssl.TLSVersion.TLSv1_2


def create_retry_client(
    user_agent: str | None = None,
    retries: int = 5,
    backoff: float = 1.2,
    status_forcelist: tuple[int, ...] = (429, 500, 502, 503, 504),
    verify: bool = True,
    timeout: float = 30.0,
) -> httpx.Client:
    """Create an ``httpx.Client`` preconfigured with retry/backoff.

    Replaces the old ``create_retry_session()`` which relied on
    ``urllib3.Retry`` + custom SSL adapter.  This version uses httpx's
    transport layer with a custom retry wrapper for fine-grained control
    over which status codes to retry.

    Args:
        user_agent: Optional User-Agent string.
        retries: Number of retries for failed connections / retryable statuses.
        backoff: Exponential backoff factor between retries.
        status_forcelist: HTTP status codes to retry on.
        verify: Whether to verify SSL certificates (default True).
        timeout: Default request timeout in seconds.

    Returns:
        A configured ``httpx.Client``.
    """
    transport = _RetryTransport(
        retries=retries,
        backoff=backoff,
        status_forcelist=status_forcelist,
        verify=verify,
    )

    headers = {}
    if user_agent:
        headers["User-Agent"] = user_agent

    # Request identity -> monotonic start time, used by the response hook.
    _request_start_times: dict[int, float] = {}

    def _log_request_start(request: httpx.Request) -> None:
        logger.debug("[HTTP] >>> %s %s", request.method, request.url)
        _request_start_times[id(request)] = time.monotonic()

    def _log_response(response: httpx.Response) -> None:
        # ``response.elapsed`` raises RuntimeError until the body has been
        # read/closed — the "response" event hook fires on headers, so
        # reading it there made every request fail with "'.elapsed' may only
        # be accessed after the response has been read or closed" and get
        # retried 3 times. Track the duration from the request hook instead.
        start = _request_start_times.pop(id(response.request), None)
        duration = (time.monotonic() - start) if start is not None else None
        duration_suffix = f" ({duration:.1f}s)" if duration is not None else ""
        logger.debug(
            "[HTTP] <<< %s %s → %s%s",
            response.request.method,
            response.request.url,
            response.status_code,
            duration_suffix,
        )

    client = httpx.Client(
        transport=transport,
        timeout=httpx.Timeout(timeout),
        headers=headers,
        verify=verify,
        limits=_build_pool_limits(),
        event_hooks={
            "request": [_log_request_start],
            "response": [_log_response],
        },
    )

    return client


__all__ = ["_RetryTransport", "create_retry_client"]
