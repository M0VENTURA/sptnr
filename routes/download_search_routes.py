"""slskd download search/proxy routes — migrated from old app.py."""

from __future__ import annotations

import asyncio
import re
import threading
import time
from typing import Any

import structlog
from quart import Blueprint, jsonify, request

from api_clients.slskd_http import SlskdHttpClient
from db.repositories.queue import update_queue_item
from db.repositories.search_logs import log_slskd_search
from helpers.config_helpers import get_config
from helpers.logging_config import log_queue, log_search
from services.downloads.slskd_service import SlskdService

logger = structlog.get_logger(__name__)

slskd_bp = Blueprint("slskd_api", __name__, url_prefix="/api/slskd")
slsk_bp = Blueprint("slsk_api", __name__, url_prefix="/api/slsk")


# =============================================================================
# MANUAL SEARCH LOGGING
# =============================================================================

_manual_search_state: dict[str, dict[str, Any]] = {}
_manual_search_lock = threading.Lock()


def _log_manual_search_event(
    *,
    search_type: str,
    query: str,
    result_count: int = 0,
    duration_seconds: float | None = None,
    notes: str | None = None,
    selected_result: dict[str, Any] | None = None,
) -> None: