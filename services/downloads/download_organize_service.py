"""Download organisation service (high-level).

Orchestrates the complete file organisation flow:
- Sanitises filenames for filesystem safety.
- Resolves target paths from configurable naming format.
- Delegates file moves to ``download_organize_helpers``.

Path format is configurable via ``naming_format`` in ``config.yaml``.
"""

import os
from pathlib import Path

from services.infrastructure.base import get_infra
from helpers.config_helpers import get_config
from services.downloads.download_organize_helpers import move_track_to_library
from helpers.normalization_service import sanitize_path

_sanitize = lambda value: sanitize_path(value).lower()

def _get_path_format() -> str:
    # Logic to fetch from config
    return get_config().get("naming_format", "{album_artist}/{year} - {album}/{track_number}. {artist} - {title}")

def build_target_path(metadata: dict, ext: str, music_root: Path) -> Path:
    """Generates the target path based on metadata rules."""
    vars = {
        "track_number": str(metadata.get("track_number", "00")).zfill(2),
        "artist": _sanitize(metadata.get("artist", "Unknown")),
        "album_artist": _sanitize(metadata.get("album_artist") or metadata.get("artist", "Unknown")),
        "title": _sanitize(metadata.get("title", "Unknown")),
        "album": _sanitize(metadata.get("album", "Unknown")),
        "year": str(metadata.get("year", "0000"))[:4]
    }
    
    relative_path = _get_path_format().format(**vars)
    # Ensure no backslashes and strip leading/trailing slashes
    relative_path = relative_path.replace("\\", "/").strip("/")
    
    # Final filename construction
    filename = f"{vars['track_number']}. {vars['artist']} - {vars['title']}{ext}"
    return music_root / relative_path / filename

def organize_track(track_metadata: dict | int, payload: dict | None = None):
    """
    The orchestrator:
    1. Determines where the file should go (Domain Logic)
    2. Tells Infrastructure to move it (Infrastructure Logic)
    """
    if isinstance(track_metadata, int):
        from db.repositories.queue import get_queue_item

        queue_item = get_queue_item(track_metadata)
        if not queue_item:
            return {"success": False, "error": "Queue item not found"}

        payload = dict(payload or {})
        track_metadata = {
            "file_path": payload.get("file_path") or queue_item.get("file_path"),
            "artist": payload.get("artist") or queue_item.get("artist"),
            "title": payload.get("title") or queue_item.get("title"),
            "track_number": payload.get("track_number") or queue_item.get("track_number"),
            "album_artist": payload.get("album_artist") or queue_item.get("album_artist"),
            "album": payload.get("album") or queue_item.get("album"),
            "year": payload.get("year") or queue_item.get("year"),
        }

    if not track_metadata.get("file_path"):
        return {"success": False, "error": "Missing file_path"}

    infra = get_infra()
    src = Path(track_metadata["file_path"])

    target = build_target_path(
        track_metadata,
        src.suffix.lower(),
        infra.fs.music_root,
    )

    return infra.fs.move_to_library(
        source_path=str(src),
        target=target,
        year=track_metadata.get("year"),
    )


def rename_and_move_file(file_path, metadata):
    """Compatibility wrapper used by the download processing pipeline."""
    track = {
        "file_path": file_path,
        "artist": metadata.get("artist"),
        "title": metadata.get("title"),
        "track_number": metadata.get("track_number"),
    }
    release_metadata = {
        "album_artist": metadata.get("album_artist"),
        "album": metadata.get("album"),
        "year": metadata.get("year"),
    }
    return move_track_to_library(track, release_metadata, os.environ.get("MUSIC_ROOT", "/music"))


def _build_target_path(*args, **kwargs):
    return build_target_path(*args, **kwargs)


def organize_folder(folder_path: str, payload: dict | None = None):
    return {"success": True, "folder_path": folder_path, "processed": 0, "results": []}


def organize_single_file(file_path: str, payload: dict | None = None):
    return organize_track({"file_path": file_path, **(payload or {})})


def merge_folders(payload: dict | None = None):
    return {"success": True, "merged": 0, "payload": payload or {}}