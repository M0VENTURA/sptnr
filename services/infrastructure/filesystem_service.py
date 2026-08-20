"""Filesystem Service

This module provides filesystem operations and utilities for the Popularr application.

Key Responsibilities:
    - Audio file format validation and support
    - Path resolution with conversion rules (e.g., FLAC→MP3)
    - Directory safety validation (path traversal prevention)
    - Empty directory cleanup after file moves
    - File metadata manipulation (modification times)
    
Supported Audio Formats:
    Default formats: .mp3, .flac, .m4a, .ogg, .wav, .aac, .wma
    
    Note: Configurable via config.yaml using 
          helpers.config_helpers.get_supported_audio_formats()

Path Resolution Policy:
    The service applies conversion rules to determine final file destinations:
    
    Example - FLAC to MP3 Conversion:
        Source: /downloads/album.flac
        Dest:   /music/Artist/Album/album
        Result: /music/Artist/Album/album.mp3  (extension rewritten)

Safety Features:
    - is_path_under_directory(): Prevents path traversal attacks
    - Validates that resolved paths stay within allowed boundaries
    - Handles edge cases (symlinks, relative paths, etc.)

File Operations:
    - cleanup_empty_parents(): Removes empty parent directories after moves
    - apply_release_year_mtime(): Sets file modification time to release year
    - Atomic operations where possible to prevent corruption

Usage:
    >>> from services.infrastructure.filesystem_service import (
    ...     get_import_destination_path,
    ...     is_path_under_directory,
    ...     cleanup_empty_parents
    ... )
    >>> dest = get_import_destination_path("/tmp/file.flac", "/music/dest", {"mode": "flac_to_mp3"})
    >>> print(dest)  # '/music/dest.mp3'

Architecture:
    Low-level infrastructure service used by download and import pipelines.
    No database access - pure filesystem operations only.
    
Called by:
    - services/downloads/download_organize_service.py
    - services/scanning/mp3_import_scanner.py
    - services/infrastructure/fs_manager.py
"""

import os
import shutil
from typing import Any

import logging
import re
from datetime import datetime
from pathlib import Path

import yaml

# Import centralized configuration getter
from helpers.config_helpers import get_supported_audio_formats

# Load supported formats from config (with defaults)
SUPPORTED_AUDIO_FORMATS = get_supported_audio_formats()
logger = logging.getLogger(__name__)

from helpers.config_helpers import get_config


# ==============================================================================
# PATH RESOLUTION (POLICY)
# ==============================================================================
def get_import_destination_path(
    source_path: str,
    dest_path: str,
    settings: dict | None = None,
) -> str:
    """
    Return final import path after applying conversion rules.

    Example:
    - FLAC → MP3 conversion rewrites extension.
    """

    if settings is None:
        cfg = get_config()
        settings = cfg.get("download_conversion", {}) if isinstance(cfg, dict) else {}

    source_ext = os.path.splitext(source_path or "")[1].lower()

    should_convert = (
        settings.get("mode") == "flac_to_mp3"
        and source_ext == ".flac"
    )

    if should_convert:
        dest_root, _ = os.path.splitext(dest_path)
        return f"{dest_root}.mp3"

    return dest_path

# ==============================================================================
# SAFETY (PATH VALIDATION)
# ==============================================================================

def is_path_under_directory(path: str, root: str) -> bool:
    """Check if path is inside root directory."""
    if not path or not root:
        return False

    try:
        abs_path = os.path.realpath(os.path.abspath(path))
        abs_root = os.path.realpath(os.path.abspath(root))

        return os.path.commonpath([abs_path, abs_root]) == abs_root
    except Exception:
        return False


# ==============================================================================
# FILESYSTEM OPERATIONS (MECHANICS)
# ==============================================================================

def cleanup_empty_parents(start_path: str, root: str) -> None:
    """Remove empty parent directories up to root."""
    parent = os.path.dirname(start_path)

    while parent and os.path.isdir(parent):

        if os.path.abspath(parent) == os.path.abspath(root):
            break

        try:
            if os.listdir(parent):
                break

            os.rmdir(parent)
            parent = os.path.dirname(parent)

        except OSError:
            break

def apply_release_year_mtime(
    file_path: str,
    year: Any,
    queue_id: int | None = None,
) -> None:
    """
    Set file modification time to Jan 1 of release year.

    Ensures music files reflect original release year rather than copy time.
    """

    if not file_path or not year:
        return

    try:
        year_int = int(str(year).strip()[:4])

        if year_int < 1900 or year_int > 2100:
            return

        ts = datetime(year_int, 1, 1).timestamp()
        os.utime(file_path, (ts, ts))

        logger.debug(
            f"[MOVE] Queue {queue_id or 'unknown'}: set mtime → {year_int}-01-01 for {file_path}"
        )

    except Exception as e:
        logger.debug(
            f"[MOVE] Queue {queue_id or 'unknown'}: failed to set mtime ({year!r}) → {e}"
        )

# ==============================================================================
# INTERNAL HELPERS
# ==============================================================================

def _prefer_music_subfolder(path: str) -> str:
    if not path:
        return path

    normalized = os.path.normpath(path)

    if os.path.basename(normalized).lower() == "downloads":
        music_subdir = os.path.join(normalized, "Music")
        if os.path.isdir(music_subdir):
            return music_subdir

    return normalized


def _safe_str(value: Any) -> str:
    if not value or not isinstance(value, str):
        return ""
    return value.strip()


def create_monitoring_folder(artist, album, year):
    # Same resolution as everything else (DOWNLOADS_DIR env var first),
    # otherwise stored monitoring_folder_path won't match the real
    # downloads dir and folder groups show up empty.
    base_dir = Path(resolve_downloads_dir())

    year_text = str(year).strip() if year not in (None, '') else ''
    year_match = re.search(r"(19|20)\d{2}", year_text)
    folder_year = year_match.group(0) if year_match else 'Unknown'

    folder_name = f"{folder_year} - {artist} - {album}".replace('/', '_').replace('\\', '_')[:200]

    folder_path = base_dir / folder_name
    folder_path.mkdir(parents=True, exist_ok=True)

    logger.info(f"[MONITOR_FOLDER] Created: {folder_path}")
    return folder_path


def ensure_music_directories(downloads_dir: str, music_dir: str):
    """Create all required directories"""
    Path(downloads_dir).mkdir(parents=True, exist_ok=True)
    Path(music_dir).mkdir(parents=True, exist_ok=True)

def transfer_download_to_music(source_path: str, dest_path: str):
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)

    try:
        shutil.move(source_path, dest_path)
        return {"success": True, "target_path": dest_path}
    except Exception as e:
        return {"success": False, "error": str(e)}
    
def _unique_path(root, filename):
    base, ext = os.path.splitext(filename)
    path = os.path.join(root, filename)
    counter = 1

    while os.path.exists(path):
        path = os.path.join(root, f"{base}_{counter}{ext}")
        counter += 1

    return path


def _get_files_in_folder(folder_path: str, max_depth: int = 3, max_files: int = 500) -> list[dict]:
    """List files in *folder_path*, recursing into subfolders.

    Downloads often land nested (``Album/CD1/01 Track.flac``), so a flat
    ``iterdir`` missed everything below the top level — the monitor page
    then showed folder groups with no items. Walks up to ``max_depth``
    levels (bounded, the monitor page polls this every few seconds) and
    returns up to ``max_files`` entries; ``name`` carries the path
    relative to the folder root so the UI can show where each file lives.

    The FLAC conversion archive (``downloads/<original_subfolder>``, default
    ``Original``) is pruned at ANY depth — an archived FLAC inside a tracked
    folder must never surface as a folder item (it was already imported and
    re-surfacing it makes the monitor / prune logic think the folder still
    holds pending audio).
    """
    files = []

    try:
        folder = Path(folder_path)

        if not folder.exists():
            return []

        archive_name = _original_archive_subfolder_name()

        root_depth = len(folder.parts)
        for root, dirs, names in os.walk(folder):
            depth = len(Path(root).parts) - root_depth
            if depth >= max_depth:
                dirs[:] = []  # do not descend further
            # Never descend into the conversion archive at any level.
            dirs[:] = [
                d for d in dirs
                if d != archive_name
                and os.path.normpath(os.path.join(root, d)) != archive_dir_path()
            ]
            for name in names:
                full = Path(root) / name
                try:
                    stat = full.stat()
                except OSError:
                    continue
                files.append({
                    "name": str(full.relative_to(folder)),
                    "size": stat.st_size,
                    "extension": full.suffix.lower(),
                    "modified": datetime.fromtimestamp(
                        stat.st_mtime
                    ).isoformat(),
                    "is_audio": full.suffix.lower() in SUPPORTED_AUDIO_FORMATS,
                })
                if len(files) >= max_files:
                    return files

        files.sort(key=lambda x: x["modified"], reverse=True)
        return files

    except Exception as e:
        logger.error("[FOLDER] Failed to list files: %s", e, exc_info=True)
        return []


def _original_archive_subfolder_name() -> str:
    """The configured conversion archive subfolder name (default ``Original``)."""
    try:
        config = get_config() or {}
        conversion_cfg = (config.get("downloads") or {}).get("conversion") or {}
        name = str(conversion_cfg.get("original_subfolder", "Original") or "Original").strip()
        return name or "Original"
    except Exception:
        return "Original"


def archive_dir_path() -> str:
    """Absolute path of the FLAC conversion archive folder (any resolution).

    Kept in the infrastructure layer so every walker (completion, discovery,
    folder listing) prunes the SAME directory the conversion pipeline writes
    to — a resolver mismatch is exactly how archived originals end up
    re-discovered and re-queued.
    """
    try:
        return resolve_original_archive_dir()
    except Exception:
        root = resolve_downloads_dir(prefer_music_subfolder=False)
        return os.path.normpath(os.path.join(root, "Original"))

def resolve_music_dir(config: dict | None = None) -> str:
    """
    Resolve music library root directory.

    Uses config if provided, otherwise falls back to config file or /music.
    """

    if config:
        return (
            config.get("music", {}).get("root")
            or config.get("music_root")
            or "/music"
        )

    # fallback to YAML (optional)
    config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")

    try:
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}

                return (
                    (cfg.get("navidrome") or {}).get("music_folder")
                    or "/music"
                )
    except Exception:
        pass

    return "/music"





def get_folder_group_details(folder_path: str):
    """
    Detailed folder inspection.
    """

    try:
        folder = Path(folder_path)

        if not folder.exists():
            return {"success": False, "error": "Folder not found"}

        files = _get_files_in_folder(folder_path)

        return {
            "success": True,
            "folder": folder_path,
            "name": folder.name,
            "file_count": len(files),
            "audio_files": len([f for f in files if f["is_audio"]]),
            "files": files,
        }

    except Exception as e:
        logger.error("[FOLDER_DETAILS] Error: %s", e, exc_info=True)
        return {"success": False, "error": str(e)}
    
    








def is_under_music_root(path_value: str, music_root: str) -> bool:
    if not path_value:
        return False

    norm = os.path.normpath(str(path_value)).replace("\\", "/").rstrip("/").lower()
    music_root_norm = music_root.replace("\\", "/").rstrip("/").lower()

    return norm == music_root_norm or norm.startswith(music_root_norm + "/")


def sanitize_collection_segment(value: str) -> str:
    value = str(value or "").strip()
    invalid = '<>:"|?*\\'
    for ch in invalid:
        value = value.replace(ch, "_")
    return value.strip(". ").lower()


def is_valid_collection_location(path_value, artist, album, album_artist, music_root):
    if not path_value:
        return False

    if not os.path.isfile(path_value):
        return False

    norm = os.path.normpath(str(path_value)).replace("\\", "/").rstrip("/")
    lowered = norm.lower()

    if lowered.startswith("__queued_for_download__"):
        return False

    if not is_under_music_root(norm, music_root):
        return False

    try:
        rel_path = os.path.relpath(norm, music_root).replace("\\", "/")
    except Exception:
        return False

    parts = [p for p in rel_path.split("/") if p and p not in (".", "..")]

    if len(parts) < 3:
        return False

    expected_artist = sanitize_collection_segment(album_artist or artist or "Unknown Artist")
    expected_album = sanitize_collection_segment(album or "Unknown Album")

    artist_dir = sanitize_collection_segment(parts[0])
    album_dir = sanitize_collection_segment(parts[-2])

    return artist_dir == expected_artist and (
        album_dir == expected_album or album_dir.endswith(f" - {expected_album}")
    )


def is_valid_source_music_path(path_value, music_root):
    if not path_value:
        return False

    if not os.path.isfile(path_value):
        return False

    norm = os.path.normpath(str(path_value)).replace("\\", "/").rstrip("/")

    if norm.lower().startswith("__queued_for_download__"):
        return False

    return is_under_music_root(norm, music_root)


def resolve_original_archive_dir() -> str:
    """Absolute path of the FLAC conversion archive folder.

    ``downloads/conversion.original_subfolder`` (default ``"Original"``)
    holds the source files archived by FLAC→MP3 conversion imports.  The
    archive must never be re-discovered by the queue scanners (the archived
    FLACs would otherwise be re-queued as fresh downloads), so every walker
    that scans the downloads root consults this resolver.
    """
    try:
        config = get_config() or {}
        conversion_cfg = (config.get("downloads") or {}).get("conversion") or {}
        subfolder = str(conversion_cfg.get("original_subfolder", "Original") or "Original").strip()
    except Exception:
        subfolder = "Original"
    root = resolve_downloads_dir(prefer_music_subfolder=False)
    return os.path.normpath(os.path.join(root, subfolder or "Original"))


def resolve_downloads_dir(prefer_music_subfolder: bool = True) -> str:
    """
    Single source of truth for download folder resolution.

    Resolution order:
        1. DOWNLOADS_DIR environment variable
        2. downloads.monitor_folder
        3. downloads.folder
        4. /downloads/Music

    ``prefer_music_subfolder`` (default True) preserves the legacy convention
    of scanning the ``Music`` subfolder when the configured root is a folder
    literally named ``downloads``.  Discovery-style scans pass False so files
    landing in the configured root are found too (the recursive walk covers
    the ``Music`` subfolder anyway).
    """

    def _resolve(value: str) -> str:
        normalized = os.path.normpath(value.strip())
        if not prefer_music_subfolder:
            return normalized
        return _prefer_music_subfolder(normalized)

    env_dir = os.environ.get("DOWNLOADS_DIR")

    if env_dir:
        return _resolve(env_dir)

    try:
        config = get_config()

        downloads_cfg = (
            config.get("downloads")
            or {}
        )

        monitor_folder = downloads_cfg.get(
            "monitor_folder"
        )

        if monitor_folder:
            return _resolve(monitor_folder)

        folder = downloads_cfg.get(
            "folder"
        )

        if folder:
            return _resolve(folder)

    except Exception:
        pass

    return "/downloads/Music"
