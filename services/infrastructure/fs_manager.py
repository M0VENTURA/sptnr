"""Filesystem manager for organised library moves.

Provides atomic file-move operations from download directories
to the target music library structure. 

Used by ``services.infrastructure.base.Infrastructure`` singleton.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any, Dict

import structlog

# Delegate shared logic to the filesystem service to avoid duplication
from services.infrastructure.filesystem_service import (
    apply_release_year_mtime,
    cleanup_empty_parents
)

logger = structlog.get_logger(__name__)


class FileSystemManager:
    """Manages secure file moves into the music library collection."""

    def __init__(self, downloads: str, music: str) -> None:
        self.music_root = Path(music).resolve()
        self.downloads_root = Path(downloads).resolve()

    def move_to_library(self, source_path: str, target: Path, year: Any = None) -> Dict[str, Any]:
        """Atomic move and timestamp update."""
        src = Path(source_path)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            
            # Avoid naming collisions
            if target.exists():
                target = target.with_name(f"{target.stem}_{int(time.time())}{target.suffix}")

            shutil.move(str(src), str(target))

            if year:
                apply_release_year_mtime(str(target), year)

            logger.info("Successfully moved file to library", source=str(src), target=str(target))
            return {"success": True, "target_path": str(target)}
            
        except Exception as e:
            logger.error("Failed to move file to library", source=str(src), target=str(target), error=str(e))
            return {"success": False, "error": str(e)}

    def cleanup_empty_dirs(self, folder: Path) -> None:
        """Standardized recursive empty folder cleanup under downloads root."""
        if not folder.is_relative_to(self.downloads_root):
            return
            
        # Delegate to the shared string-based path cleanup logic
        cleanup_empty_parents(str(folder / "dummy.txt"), str(self.downloads_root))
