"""HTTP utility helpers for API clients.

Provides a retry-capable session factory and a custom SSL adapter
that handles TLS issues gracefully.
"""

from __future__ import annotations

import ssl
from typing import Any

from urllib3.util.retry import Retry
from urllib3.util.ssl_ import create_urllib3_context
from requests.adapters import HTTPAdapter
import requests


class SSLAdapter(HTTPAdapter):
    """Custom HTTPAdapter with improved SSL/TLS handling.

    Creates a custom SSL context that is more resilient to
    SSL/TLS protocol errors, particularly the "EOF occurred in violation
    of protocol" error that can occur with some servers.
    """

    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
        ctx = create_urllib3_context()
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2

        if hasattr(ssl, "OP_LEGACY_SERVER_CONNECT"):
            ctx.options |= ssl.OP_LEGACY_SERVER_CONNECT

        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


def create_retry_session(
    user_agent: str | None = None,
    retries: int = 5,
    backoff: float = 1.2,
    status_forcelist: tuple[int, ...] = (429, 500, 502, 503, 504),
    allowed_methods: tuple[str, ...] = ("GET", "POST"),
    verify_ssl: bool = True,
) -> requests.Session:
    """Create a requests.Session preconfigured with retry/backoff.

    Handles HTTP errors, connection errors, and SSL errors with
    exponential backoff. Uses a custom SSL adapter to handle SSL/TLS
    protocol issues more gracefully.

    Args:
        user_agent: Optional User-Agent string.
        retries: Number of retries for failed connections.
        backoff: Exponential backoff factor between retries.
        status_forcelist: HTTP status codes to retry on.
        allowed_methods: HTTP methods to allow retries for.
        verify_ssl: Whether to verify SSL certificates (default True).

    Returns:
        A configured requests.Session.
    """
    s = requests.Session()
    retry = Retry(
        total=retries,
        connect=retries,
        read=retries,
        backoff_factor=backoff,
        status_forcelist=status_forcelist,
        allowed_methods=frozenset(allowed_methods),
        raise_on_status=False,
    )

    s.verify = verify_ssl
    ssl_adapter = SSLAdapter(max_retries=retry)
    s.mount("https://", ssl_adapter)
    s.mount("http://", HTTPAdapter(max_retries=retry))

    if user_agent:
        s.headers.update({"User-Agent": user_agent})

    return s


__all__ = ["SSLAdapter", "create_retry_session"]
