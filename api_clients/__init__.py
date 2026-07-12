"""Shared API client utilities.

API clients should stay deliberately boring:
- own HTTP request details for one external service
- expose small methods that return raw or lightly-normalised API data
- avoid business rules, scoring, orchestration, caching policy, and DB writes

Service-level modules under ``services/`` should compose these clients into
application workflows.

HTTP layer: uses ``httpx`` instead of ``requests`` for built-in connection
pooling, HTTP/2 support, and future async compatibility.
"""

from __future__ import annotations

import logging

from api_clients.http_utils import create_retry_client

logger = logging.getLogger(__name__)

# Standard shared session — used by most API clients
session = create_retry_client(
    retries=3,
    backoff=1.0,
    status_forcelist=(429, 500, 502, 503, 504),
)

# Lightweight session for operations where quick failure is preferred
timeout_safe_session = create_retry_client(
    retries=1,
    backoff=0.5,
    status_forcelist=(429, 500, 502, 503, 504),
)

__all__ = ["session", "timeout_safe_session", "logger"]
