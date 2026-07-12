"""Download organisation helpers.

Low-level file operations for organising downloaded tracks into
the music library structure. Handles:
- Moving files from downloads to library.
- Applying naming conventions.
- Cleaning up source directories.
"""

import os
import shutil
from pathlib import Path

from helpers.config_helpers import get_config


# =============================================================================
# HELPERS
# =============================================================================

def _first_non_empty(*values):
    for v in values:
        if v is None:
            continue
        if isinstance(v, str):
            if v.strip():
                return v.strip()
            continue
        return v
    return None


def _build_target_path(root, album_artist, year, album, artist, title, track_number, source_file):
    ext = os.path.splitext(source_file)[1]

    if track_number:
        try:
            prefix = f"{int(track_number):02d} - "
        except Exception:
            prefix = f"{track_number} - "
    else:
        prefix = ""

    album_folder = f"({year}) {album}" if year else album

    return os.path.join(
        root,
        album_artist or artist or "Unknown Artist",
        album_folder or "Unknown Album",
        f"{prefix}{title}{ext}"
    )






def _read_track_file_name_format() -> str:
    """
    Read configurable file naming format.

    Uses central config helper instead of direct YAML access.
    """

    try:
        cfg = get_config() or {}
        downloads_cfg = cfg.get("downloads") or {}

        fmt = downloads_cfg.get("file_name_format")

        if isinstance(fmt, str) and fmt.strip():
            return fmt.strip()

    except Exception:
        # Keep silent – fallback handles it
        pass

    # ✅ Default fallback
    return "{album_artist}/{year} - {album}/{track_number}. {artist} - {title}"


def _sanitize_path_component(value: str) -> str:
    if not value:
        return ""
    invalid = '<>:"|?*\\'
    for char in invalid:
        value = value.replace(char, "_")
    return value.strip().strip(".")


def _normalize_album_artist_for_path(value: str) -> str:
    normalized = str(value or "").strip()

    key = (
        normalized.lower()
        .replace("-", " ")
        .replace("_", " ")
        .replace(".", " ")
    )
    key = " ".join(key.split())

    if key in ("various", "various artist", "various artists", "va", "v/a") or key.startswith("various"):
        return "Various Artists"

    return normalized


def is_match(path: str, item: dict) -> bool:
    filename = os.path.basename(path).lower()
    artist = (item.get("artist") or "").lower()
    title = (item.get("title") or "").lower()
    return artist in filename and title in filename


def move_track_to_library(track, release_metadata, music_root):
    """Move a track into the configured library structure."""
    file_path = track.get("file_path")

    if not file_path:
        return {"success": False, "error": "Missing file_path"}

    target_path = _build_target_path(
        music_root,
        release_metadata.get("album_artist"),
        release_metadata.get("year"),
        release_metadata.get("album"),
        track.get("artist"),
        track.get("title"),
        track.get("track_number"),
        file_path,
    )

    if os.path.exists(target_path):
        stem = Path(target_path).stem
        suffix = Path(target_path).suffix
        target_path = f"{target_path[:-len(suffix)]}_{os.getpid()}{suffix}" if target_path.endswith(suffix) else f"{target_path}_{os.getpid()}"

    try:
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        shutil.move(file_path, target_path)
    except Exception as exc:
        return {"success": False, "error": str(exc)}

    return {"success": True, "target_path": target_path}

