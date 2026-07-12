"""Beets integration routes.

Provides a blueprint for interacting with the ``beets`` music tagger
(https://beets.io).  All endpoints gracefully degrade if beets is not
installed on the system, so no special Docker dependencies are required.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from typing import Any

from flask import Blueprint, jsonify, request

from sqlalchemy import text

from db.engine import db_session

logger = logging.getLogger(__name__)

beets_bp = Blueprint("beets", __name__, url_prefix="/api/beets")

BEETS_CONFIG_PATH = os.environ.get("BEETS_CONFIG", "/config/config.yaml")
MUSIC_ROOT = os.environ.get("MUSIC_ROOT", "/music")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _beets_installed() -> bool:
    return shutil.which("beet") is not None


def _beets_config_exists() -> bool:
    candidates = [
        BEETS_CONFIG_PATH,
        os.path.expanduser("~/.config/beets/config.yaml"),
        os.path.expanduser("~/.beetsconfig"),
    ]
    return any(os.path.isfile(p) for p in candidates)


def _run_beet(*args: str, timeout: int = 300) -> tuple[int, str, str]:
    if not _beets_installed():
        return 1, "", "beet CLI not found on PATH"
    try:
        proc = subprocess.run(
            ["beet", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "Command timed out"
    except FileNotFoundError:
        return 1, "", "beet not found"
    except Exception as exc:
        return 1, "", str(exc)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@beets_bp.route("/status", methods=["GET"])
def beets_status():
    """Return beets installation status and library statistics."""
    installed = _beets_installed()
    config_exists = _beets_config_exists()

    status: dict[str, Any] = {
        "enabled": installed,
        "installed": installed,
        "config_exists": config_exists,
        "version": "",
    }
    stats: dict[str, Any] = {"total_tracks": 0, "total_albums": 0, "total_artists": 0}

    if installed:
        try:
            _rc, out, _err = _run_beet("version")
            if _rc == 0:
                status["version"] = out.strip()
        except Exception:
            pass

    try:
        with db_session() as session:
            stats["total_tracks"] = session.execute(text("SELECT COUNT(*) AS c FROM tracks")).scalar()
            stats["total_albums"] = session.execute(text("SELECT COUNT(DISTINCT album) AS c FROM tracks")).scalar()
            stats["total_artists"] = session.execute(text("SELECT COUNT(DISTINCT COALESCE(NULLIF(album_artist, ''), artist)) AS c FROM tracks")).scalar()
    except Exception:
        pass

    return jsonify({"success": True, "status": status, "stats": stats})


@beets_bp.route("/import", methods=["POST"])
def beets_import():
    """Import files from a given path using ``beet import``."""
    data = request.get_json(silent=True) or {}
    path = str(data.get("path", "")).strip()
    if not path:
        return jsonify({"error": "path required"}), 400
    if not _beets_installed():
        return jsonify({"error": "beets not installed"}), 400

    _rc, out, err = _run_beet("import", "-q", path)
    return jsonify({"success": _rc == 0, "message": out.strip() or err.strip() or "Import finished", "returncode": _rc})


@beets_bp.route("/configure", methods=["POST"])
def beets_configure():
    """Create a default beets configuration file if one does not exist."""
    if _beets_config_exists():
        return jsonify({"success": True, "message": "Configuration already exists"})
    default_config = {
        "directory": MUSIC_ROOT,
        "library": "/config/beets/library.db",
        "import": {"copy": True, "write": True, "resume": True, "quiet": True},
        "plugins": "fetchart lastgenre embedart lyrics",
        "fetchart": {"auto": True},
        "lastgenre": {"auto": True, "separator": ", "},
        "embedart": {"auto": True},
    }
    try:
        os.makedirs(os.path.dirname(BEETS_CONFIG_PATH), exist_ok=True)
        import yaml
        with open(BEETS_CONFIG_PATH, "w") as f:
            yaml.dump(default_config, f)
        return jsonify({"success": True, "message": f"Configuration created at {BEETS_CONFIG_PATH}"})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@beets_bp.route("/auto-import", methods=["POST"])
def beets_auto_import():
    """Auto-import the entire library (``beet import -A``)."""
    data = request.get_json(silent=True) or {}
    artist_path = str(data.get("artist", "")).strip()
    if not _beets_installed():
        return jsonify({"error": "beets not installed"}), 400
    args = ["import", "-q", "-A"]
    args.append(artist_path) if artist_path else args.append(MUSIC_ROOT)
    _rc, out, err = _run_beet(*args)
    return jsonify({"success": _rc == 0, "message": out.strip() or err.strip() or "Auto-import finished", "returncode": _rc})


@beets_bp.route("/sync-metadata", methods=["POST"])
def beets_sync_metadata():
    """Sync beets metadata (MBIDs, genres) to the popularr database."""
    if not _beets_installed():
        return jsonify({"error": "beets not installed"}), 400

    _rc, out, _err = _run_beet("list", "-f", '{"id":"$id","mbid":"$mbid","artist":"$artist","album":"$album","title":"$title","year":"$year","genre":"$genre"}', timeout=600)
    if _rc != 0:
        return jsonify({"error": "beets query failed"}), 500

    updated = 0
    conn = get_db_connection()
    cursor = conn.cursor()
    for line in out.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
            track_mbid = item.get("mbid", "")
            if track_mbid:
                cursor.execute("UPDATE tracks SET mbid = %s, beets_mbid = %s WHERE id = %s AND (mbid IS NULL OR mbid = '')", (track_mbid, track_mbid, item.get("id", "")))
                updated += cursor.rowcount
        except (json.JSONDecodeError, Exception):
            continue
    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": f"Synced {updated} track(s)", "updated": updated})


@beets_bp.route("/update-album", methods=["POST"])
def beets_update_album():
    """Write beets metadata for a specific album, then trigger ``beet write``."""
    data = request.get_json(silent=True) or {}
    artist = str(data.get("artist", "")).strip()
    album = str(data.get("album", "")).strip()
    if not artist or not album:
        return jsonify({"error": "artist and album required"}), 400
    if not _beets_installed():
        return jsonify({"error": "beets not installed"}), 400
    _rc, out, err = _run_beet("write", "-q", f"album:{album} artist:{artist}")
    return jsonify({"success": _rc == 0, "message": out.strip() or err.strip() or f"Tags written for {artist} - {album}"})


@beets_bp.route("/album-folders/<path:artist>", methods=["GET"])
def beets_album_folders(artist: str):
    """Return a list of album directories for an artist, grouped by filesystem path."""
    if not _beets_installed():
        return jsonify({"error": "beets not installed"}), 400
    _rc, out, _err = _run_beet("list", "-f", "$path", f"artist:{artist}")
    if _rc != 0:
        return jsonify({"albums": []})
    albums: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in out.strip().split("\n"):
        path = line.strip()
        if not path or path in seen:
            continue
        seen.add(path)
        albums.append({"path": path, "album": path.replace("\\", "/").strip("/").split("/")[-1] if path else path})
    return jsonify({"success": True, "albums": albums})
