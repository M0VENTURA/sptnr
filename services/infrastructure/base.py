"""Infrastructure service singleton.

Unified access point for infrastructure-level services:
- ``FileSystemManager`` – Filesystem operations.
- ``APIRateLimiter`` – Cross-provider rate limiting.

Initialised once via singleton pattern and accessed via
``get_infra()`` throughout the application.
"""

from __future__ import annotations

import threading
from typing import Optional

import structlog

from helpers.config_helpers import get_config
from services.infrastructure.api_rate_limiter import APIRateLimiter
from services.infrastructure.fs_manager import FileSystemManager

logger = structlog.get_logger(__name__)


class Infrastructure:
    """A unified access point (Singleton) for infrastructure services."""
    _instance: Optional[Infrastructure] = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        cfg = get_config() or {}

        downloads = cfg.get("downloads", {}).get("monitor_folder", "/downloads")
        music = cfg.get("navidrome", {}).get("music_folder", "/music")

        self.fs = FileSystemManager(downloads, music)
        self.api = APIRateLimiter()

        logger.info("Infrastructure services initialized successfully.")

    @classmethod
    def get_instance(cls) -> Infrastructure:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance


def get_infra() -> Infrastructure:
    """Convenience helper to access the infrastructure singleton."""
    return Infrastructure.get_instance()
