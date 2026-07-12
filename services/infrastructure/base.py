"""Infrastructure service singleton.

Unified access point for infrastructure-level services:
- ``FileSystemManager`` – Filesystem operations.
- ``APIRateLimiter`` – Cross-provider rate limiting.

Initialised once via ``init_infrastructure()`` and accessed via
``get_infra()`` throughout the application.
"""

import logging
import threading
from typing import Optional

from helpers.config_helpers import get_config
from services.infrastructure.fs_manager import FileSystemManager
from services.infrastructure.api_rate_limiter import APIRateLimiter

# Setup standard logger for the infrastructure layer
logger = logging.getLogger(__name__)

class Infrastructure:
    """
    A unified access point (Singleton) for infrastructure services.
    Ensures that managers are initialized exactly once.
    """
    _instance: Optional['Infrastructure'] = None
    _lock = threading.Lock()

    def __init__(self):
        cfg = get_config()
        
        # Extract paths from config
        downloads = cfg.get("downloads", {}).get("monitor_folder", "/downloads")
        music = cfg.get("navidrome", {}).get("music_folder", "/music")
        
        # Initialize sub-services
        self.fs = FileSystemManager(downloads, music)
        self.api = APIRateLimiter()
        
        logger.info("Infrastructure services initialized.")

    @classmethod
    def get_instance(cls) -> 'Infrastructure':
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

# Helper for easy access throughout the app
def get_infra() -> Infrastructure:
    return Infrastructure.get_instance()