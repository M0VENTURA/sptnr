"""
Shared API client utilities and session management for SPTNR.
All API client modules use this shared session.
"""

import logging
from helpers import create_retry_session

# ✅ Shared HTTP session with connection pooling & retry strategy
# Increased backoff from 0.3 to 1.0 to better handle SSL connection resets
session = create_retry_session(
    retries=3,
    backoff=1.0,
    status_forcelist=(429, 500, 502, 503, 504)
)

# ✅ Timeout-sensitive HTTP session with minimal retries
# Used for API calls wrapped in _run_with_timeout to prevent thread pool exhaustion.
# Increased backoff from 0.2 to 0.5s to better handle connection resets.
# With 1 retry max and backoff=0.5s, plus typical API call timeouts of (5,10)s used
# in spotify.py and other clients, max request duration is:
# - First attempt: 15s (5s connect + 10s read)
# - Retry delay: 0.5s
# - Second attempt: 15s
# - Total: ~30.5s maximum
# Note: This slightly exceeds the default 30s API_CALL_TIMEOUT, but the benefit
# of handling transient connection errors outweighs the minimal timeout increase.
# If needed, increase POPULARITY_API_TIMEOUT environment variable to 35s.
timeout_safe_session = create_retry_session(
    retries=1,
    backoff=0.5,
    status_forcelist=(429, 500, 502, 503, 504)
)

logger = logging.getLogger(__name__)

__all__ = ["session", "timeout_safe_session", "logger"]
