"""Progress and checkpoint helpers for scanning.

Single source of truth for scan progress tracking across processes.
Provides file-based checkpointing for resume capability, progress
files for WebUI display, and graceful stop/cancel state.

Key Functions:
    - save_progress(): Save current scan position for resume.
    - get_resume_artist(): Read last checkpoint to determine resume point.
    - write_progress_with_current_artist(): Update progress file for UI.
    - read_progress_file(): Read current progress for WebUI endpoint.
    - is_stop_requested(): Check if user requested scan cancellation.

Architecture:
    Uses JSON files on disk for cross-process state sharing. This is
    the primary resume mechanism, suitable for multi-worker deployments.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from helpers.config_helpers import get_state_directory

DEFAULT_STATE_DIR = Path(get_state_directory())


# -------------------------------------------------------------------------
# Internal helpers
# -------------------------------------------------------------------------

def _now() -> str:
    return datetime.now().isoformat()


def _ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


# -------------------------------------------------------------------------
# Generic path helpers
# -------------------------------------------------------------------------

def get_database_dir() -> str:
    explicit = os.environ.get("SCAN_STATE_DIR")
    if explicit:
        return explicit

    db_path = (
        os.environ.get("DB_PATH")
        or os.environ.get("DATABASE_PATH")
        or "/database/popularr.db"
    )

    return os.path.dirname(db_path) or "/database"


def progress_path(filename: str) -> str:
    return str(Path(get_database_dir()) / filename)


# -------------------------------------------------------------------------
# Progress paths
# -------------------------------------------------------------------------

def get_navidrome_progress_path() -> str:
    return os.environ.get(
        "NAVIDROME_PROGRESS_FILE",
        progress_path("navidrome_scan_progress.json"),
    )


def get_library_progress_path() -> str:
    return os.environ.get(
        "LIBRARY_PROGRESS_FILE",
        progress_path("library_scan_progress.json"),
    )


def get_scan_progress_path(scan_type: str = "library_scan") -> str:
    if scan_type in {"navidrome", "navidrome_scan"}:
        return get_navidrome_progress_path()

    if scan_type in {"library", "library_scan", "full", "full_scan"}:
        return get_library_progress_path()

    return progress_path(f"{scan_type}_progress.json")


# -------------------------------------------------------------------------
# Checkpoint paths
# -------------------------------------------------------------------------

def get_navidrome_checkpoint_path() -> str:
    return os.environ.get(
        "NAVIDROME_CHECKPOINT_FILE",
        progress_path("navidrome_scan_checkpoint.json"),
    )


def get_library_checkpoint_path() -> str:
    return os.environ.get(
        "LIBRARY_CHECKPOINT_FILE",
        progress_path("library_scan_checkpoint.json"),
    )


def get_scan_checkpoint_path(scan_type: str = "library_scan") -> str:
    if scan_type in {"navidrome", "navidrome_scan"}:
        return get_navidrome_checkpoint_path()

    if scan_type in {"library", "library_scan", "full", "full_scan"}:
        return get_library_checkpoint_path()

    return progress_path(f"{scan_type}_checkpoint.json")


# -------------------------------------------------------------------------
# Progress file helpers
# -------------------------------------------------------------------------

def read_progress_file(path: str) -> dict[str, Any]:
    if not os.path.exists(path):
        return {}

    try:
        with open(path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
        return state if isinstance(state, dict) else {}
    except Exception:
        return {}


def write_progress_file(
    path: str,
    scan_type: str,
    is_running: bool,
    extra: dict[str, Any] | None = None,
) -> None:
    _ensure_parent_dir(path)

    state: dict[str, Any] = {
        "scan_type": scan_type,
        "is_running": is_running,
        "last_updated": _now(),
    }

    if extra:
        state.update(extra)

    with open(path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2)


def write_progress_with_current_artist(
    path: str,
    scan_type: str,
    is_running: bool,
    current_artist: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """
    Improved version:
    - Preserves ALL existing fields (critical for stop_requested)
    - Preserves current_artist if not explicitly set
    - Falls back to resume_from if needed
    """

    existing = read_progress_file(path)

    state: dict[str, Any] = {
        **existing,
        "scan_type": scan_type,
        "is_running": is_running,
        "last_updated": _now(),
    }

    # Resolve current artist
    final_artist = current_artist

    if not final_artist and extra:
        final_artist = extra.get("current_artist") or extra.get("resume_from")

    if not final_artist:
        final_artist = existing.get("current_artist") or existing.get("resume_from")

    if final_artist:
        state["current_artist"] = final_artist

    if extra:
        state.update(extra)

    _ensure_parent_dir(path)

    with open(path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2)


def clear_progress_file(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


# -------------------------------------------------------------------------
# Stop / cancel helpers
# -------------------------------------------------------------------------

def request_scan_stop(path: str, scan_type: str = "library_scan") -> None:
    state = read_progress_file(path)

    state.update({
        "scan_type": state.get("scan_type") or scan_type,
        "is_running": False,
        "status": "stop_requested",
        "stop_requested": True,
        "last_updated": _now(),
    })

    _ensure_parent_dir(path)

    with open(path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2)


def is_stop_requested(path: str) -> bool:
    state = read_progress_file(path)
    return bool(state.get("stop_requested")) or state.get("status") == "stop_requested"


def clear_stop_request(path: str) -> None:
    state = read_progress_file(path)

    if not state:
        return

    state["stop_requested"] = False

    if state.get("status") == "stop_requested":
        state["status"] = "running" if state.get("is_running") else "idle"

    state["last_updated"] = _now()

    _ensure_parent_dir(path)

    with open(path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2)


# -------------------------------------------------------------------------
# Checkpoint helpers
# -------------------------------------------------------------------------

def load_scan_checkpoint(path: str | None = None) -> dict[str, Any]:
    checkpoint_path = path or get_library_checkpoint_path()

    if not os.path.exists(checkpoint_path):
        return {}

    try:
        with open(checkpoint_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_artist_scan_checkpoint(
    artist_name: str,
    checkpoint_path: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    path = checkpoint_path or get_library_checkpoint_path()
    _ensure_parent_dir(path)

    payload: dict[str, Any] = {
        "last_scanned_artist": artist_name,
        "updated_at": _now(),
    }
    if extra:
        payload.update(extra)

    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)


def get_last_scanned_artist(path: str | None = None) -> str | None:
    checkpoint = load_scan_checkpoint(path)
    value = checkpoint.get("last_scanned_artist")
    return str(value) if value else None


def clear_scan_checkpoint(checkpoint_path: str | None = None) -> None:
    path = checkpoint_path or get_library_checkpoint_path()

    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


# -------------------------------------------------------------------------
# Compatibility aliases
# -------------------------------------------------------------------------

def get_resume_artist(path: str | None = None) -> str | None:
    return get_last_scanned_artist(path)


def save_progress(artist_name: str, checkpoint_path: str | None = None) -> None:
    save_artist_scan_checkpoint(artist_name, checkpoint_path)


# -------------------------------------------------------------------------
# Boot marker
# -------------------------------------------------------------------------

def mark_navidrome_first_full_import_complete(scan_source: str = "unknown") -> None:
    path = os.environ.get(
        "NAVIDROME_FIRST_IMPORT_MARKER",
        progress_path("navidrome_first_import_complete.json"),
    )

    _ensure_parent_dir(path)

    with open(path, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "complete": True,
                "scan_source": scan_source,
                "timestamp": _now(),
            },
            handle,
            indent=2,
        )