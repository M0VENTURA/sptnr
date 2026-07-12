"""HTTP utility helpers for API clients.

Provides a retry-capable httpx client factory and a custom transport
that handles TLS issues gracefully.

Replaced the old ``requests``/``urllib3``-based implementation with
``httpx`` for better connection pooling, HTTP/2 support, and async
compatibility.
"""

from __future__ import annotations

import ssl
from typing import Any

import httpx


class _RetryTransport(httpx.BaseTransport):
    """Transport wrapper that adds retry logic on top of httpx's HTTPTransport.

    Retries on transient errors (connection drops, DNS failures, timeouts)
    and configurable HTTP status codes using exponential backoff.
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
        self._transport = httpx.HTTPTransport(verify=verify, retries=0)

    def handle_request(
        self,
        request: httpx.Request,
    ) -> httpx.Response:
        last_exc: Exception | None = None

        for attempt in range(self._retries + 1):
            try:
                response = self._transport.handle_request(request)

                if (
                    response.status_code in self._status_forcelist
                    and attempt < self._retries
                ):
                    import time
                    wait = self._backoff * (2 ** attempt)
                    time.sleep(wait)
                    continue

                return response

            except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.TimeoutException) as exc:
                last_exc = exc
                if attempt < self._retries:
                    import time
                    wait = self._backoff * (2 ** attempt)
                    time.sleep(wait)
                    continue
                raise

        # Should not be reached, but satisfy the return type
        raise last_exc or httpx.TransportError("Maximum retries exceeded")

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

    client = httpx.Client(
        transport=transport,
        timeout=httpx.Timeout(timeout),
        headers=headers,
        verify=verify,
    )

    return client


__all__ = ["_RetryTransport", "create_retry_client"]
