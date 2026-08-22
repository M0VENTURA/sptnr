"""Filesystem manager for organised library moves.

Provides atomic file-move operations from download directories
to the target music library structure. Handles:
- Path resolution and directory creation.
- Unique name generation for conflicts.
- Timestamp preservation.

Used by ``services.infrastructure.base.Infrastructure`` singleton.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict

import structlog

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
            if target.exists():
                target = target.with_name(f"{target.stem}_{int(time.time())}{target.suffix}")

            shutil.move(str(src), str(target))

            if year:
                self._apply_mtime(target, year)

            logger.info("Successfully moved file to library", source=str(src), target=str(target))
            return {"success": True, "target_path": str(target)}
        except Exception as e:
            logger.error("Failed to move file to library", source=str(src), target=str(target), error=str(e))
            return {"success": False, "error": str(e)}

    def _apply_mtime(self, file_path: Path, year: Any) -> None:
        """Sets mtime to Jan 1 of the release year."""
        try:
            year_int = int(str(year).strip()[:4])
            if 1900 <= year_int <= 2100:
                ts = time.mktime(time.strptime(f"{year_int}-01-01", "%Y-%m-%d"))
                os.utime(file_path, (ts, ts))
        except Exception as exc:
            logger.debug("Failed to apply release year mtime", path=str(file_path), error=str(exc))

    def cleanup_empty_dirs(self, folder: Path) -> None:
        """Standardized recursive empty folder cleanup under downloads root."""
        try:
            if folder.is_relative_to(self.downloads_root):
                if folder.is_dir() and not any(folder.iterdir()):
                    folder.rmdir()
                    logger.debug("Cleaned up empty directory", path=str(folder))
                    self.cleanup_empty_dirs(folder.parent)
        except Exception as exc:
            logger.debug("Empty directory cleanup skipped", path=str(folder), error=str(exc))
