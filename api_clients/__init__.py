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

logger = logging.getLogger(__name__)

__all__ = ["session", "logger"]
