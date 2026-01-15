"""
Shared API client utilities and session management for SPTNR.
All API client modules use this shared session.
"""

import logging
from helpers import create_retry_session

# ✅ Shared HTTP session with connection pooling & retry strategy
session = create_retry_session(
    retries=3,
    backoff=0.3,
    status_forcelist=(429, 500, 502, 503, 504)
)

# ✅ Timeout-sensitive HTTP session with minimal retries
# Used for API calls wrapped in _run_with_timeout to prevent thread pool exhaustion.
# With 1 retry max and (5,10) timeout, max duration is ~17s, well under 30s timeout.
timeout_safe_session = create_retry_session(
    retries=1,
    backoff=0.2,
    status_forcelist=(429, 500, 502, 503, 504)
)

logger = logging.getLogger(__name__)

__all__ = ["session", "timeout_safe_session", "logger"]
