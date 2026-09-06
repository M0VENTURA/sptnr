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
from services.infrastructure.api_rate_limiter import get_rate_limiter
from services.infrastructure.fs_manager import FileSystemManager

logger = structlog.get_logger(__name__)


class Infrastructure:
    """A unified access point (Singleton) for infrastructure services."""
    _instance: Optional[Infrastructure] = None
    _lock = threading.Lock()
    _initialized: bool = False

    def __new__(cls) -> Infrastructure:
        """Enforce strict singleton instantiation."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        # Prevent re-initialization if Infrastructure() is called multiple times
        if getattr(self, "_initialized", False):
            return
            
        with self._lock:
            if getattr(self, "_initialized", False):
                return
                
            cfg = get_config() or {}

            downloads = cfg.get("downloads", {}).get("monitor_folder", "/downloads")
            music = cfg.get("navidrome", {}).get("music_folder", "/music")

            self.fs = FileSystemManager(downloads, music)
            
            # FIX: Bind to the exact same shared global rate limiter instance
            # used by the low-level HTTP clients.
            self.api = get_rate_limiter()

            self._initialized = True
            logger.info("Infrastructure services initialized successfully.")

    @classmethod
    def get_instance(cls) -> Infrastructure:
        """Backward compatibility for existing get_instance() calls."""
        return cls()


def get_infra() -> Infrastructure:
    """Convenience helper to access the infrastructure singleton."""
    return Infrastructure()
