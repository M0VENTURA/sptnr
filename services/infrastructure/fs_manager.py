"""Filesystem manager for organised library moves.

Provides atomic file-move operations from download directories
to the target music library structure. Handles:
- Path resolution and directory creation.
- Unique name generation for conflicts.
- Timestamp preservation.

Used by ``services.infrastructure.base.Infrastructure`` singleton.
"""

from pathlib import Path
import os
import shutil
import logging
import time
from typing import Any

class FileSystemManager:
    def __init__(self, downloads: str, music: str):
        self.music_root = Path(music).resolve()
        self.downloads_root = Path(downloads).resolve()

    def move_to_library(self, source_path: str, target: Path, year: Any = None) -> dict:
        """Atomic move and timestamp update."""
        src = Path(source_path)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            # Handle unique name if exists
            if target.exists():
                target = target.with_name(f"{target.stem}_{int(time.time())}{target.suffix}")
            
            shutil.move(str(src), str(target))
            
            if year:
                self._apply_mtime(target, year)
                
            return {"success": True, "target_path": str(target)}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _apply_mtime(self, file_path: Path, year: Any):
        """Sets mtime to Jan 1 of the release year."""
        try:
            year_int = int(str(year).strip()[:4])
            if 1900 <= year_int <= 2100:
                ts = time.mktime(time.strptime(f"{year_int}-01-01", "%Y-%m-%d"))
                os.utime(file_path, (ts, ts))
        except Exception:
            pass

    def cleanup_empty_dirs(self, folder: Path):
        """Standardized cleanup."""
        if folder.is_relative_to(self.downloads_root):
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()
                self.cleanup_empty_dirs(folder.parent)