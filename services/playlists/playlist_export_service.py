"""Playlist export: zip a playlist's local audio files with job progress.

``create_export_job`` starts a background thread that streams the playlist's
tracks into a ZIP archive (stored, not compressed — audio is already
compressed) in the system temp dir.  The frontend polls
``GET /api/playlists/export/status/<job_id>`` and downloads the finished
archive from ``GET /api/playlists/export/download/<job_id>``.
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
import threading
import time
import uuid
import zipfile

from helpers.config_helpers import get_config

_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()
# Keep at most this many finished/in-flight jobs; older ZIPs are deleted.
_MAX_JOBS = 5

_UNSAFE_RE = re.compile(r'[\\/:*?"<>|]+')


def music_root() -> str:
    """Absolute music library root (config navidrome.music_folder first)."""
    cfg = get_config() or {}
    root = (
        (cfg.get("navidrome") or {}).get("music_folder")
        or os.environ.get("MUSIC_FOLDER")
        or os.environ.get("MUSIC_DIR")
        or "/music"
    )
    return os.path.realpath(str(root))


def resolve_track_file(file_path) -> str | None:
    """Resolve a stored track path to an existing local audio file."""
    try:
        raw = str(file_path or "").strip()
        if not raw or "__queued_for_download__" in raw:
            return None
        direct = os.path.realpath(raw)
        if os.path.isfile(direct):
            return direct
        joined = os.path.realpath(os.path.join(music_root(), raw))
        if os.path.isfile(joined):
            return joined
    except Exception:
        pass
    return None


def safe_arcname(name: str) -> str:
    """Sanitize a playlist/folder name for use inside a ZIP archive."""
    cleaned = _UNSAFE_RE.sub("_", str(name or "Playlist")).strip()
    return cleaned or "Playlist"


def _lookup_track_files(ids: list[str]) -> dict[str, str]:
    """Map track ids (text form) to existing local file paths, batched."""
    unique = list(dict.fromkeys(str(i).strip() for i in ids if str(i).strip()))
    found: dict[str, str] = {}
    if not unique:
        return found
    try:
        from sqlalchemy import text
        from db.engine import db_session
        for start in range(0, len(unique), 200):
            chunk = unique[start:start + 200]
            placeholders = ", ".join(f":p{i}" for i in range(len(chunk)))
            params = {f"p{i}": cid for i, cid in enumerate(chunk)}
            with db_session() as session:
                rows = session.execute(
                    text(
                        "SELECT CAST(id AS TEXT), file_path FROM tracks "
                        f"WHERE CAST(id AS TEXT) IN ({placeholders})"
                    ),
                    params,
                ).fetchall()
            for row in rows or []:
                path = resolve_track_file(row[1])
                if path:
                    found[str(row[0])] = path
    except Exception:
        pass
    return found


def _build_export_entries(playlist_name: str, tracks: list[dict]) -> tuple[list[dict], list[dict]]:
    """Map playlist tracks to ``{file_path, arcname}`` entries.

    Returns ``(entries, skipped)``; skipped entries carry ``{title, reason}``.
    Entries with a direct ``file_path`` (m3u) are used as-is; entries with a
    DB track id (nsp trackIds / embedded) are resolved via a batched lookup.
    """
    entries: list[dict] = []
    skipped: list[dict] = []
    seen: set[str] = set()
    folder = safe_arcname(playlist_name)
    id_entries: list[tuple[dict, str]] = []

    def _add(resolved: str, track: dict) -> None:
        title = safe_arcname(str(track.get("title") or os.path.basename(resolved)))
        artist = safe_arcname(str(track.get("artist") or "Unknown Artist"))
        ext = os.path.splitext(resolved)[1].lower() or ".bin"
        arcname = f"{folder}/{artist} - {title}{ext}"
        base, count = arcname, 2
        while arcname.lower() in seen:
            arcname = f"{base[:-len(ext)]} ({count}){ext}"
            count += 1
        seen.add(arcname.lower())
        entries.append({"file_path": resolved, "arcname": arcname})

    for track in tracks or []:
        if not isinstance(track, dict):
            continue
        file_path = str(track.get("file_path") or "").strip()
        resolved = resolve_track_file(file_path) if file_path else None
        if resolved:
            _add(resolved, track)
            continue
        tid = str(track.get("id") or "").strip()
        if tid and "#" not in tid:
            id_entries.append((track, tid))
        else:
            skipped.append({"title": str(track.get("title") or ""), "reason": "no local audio file"})

    if id_entries:
        path_map = _lookup_track_files([tid for _, tid in id_entries])
        for track, tid in id_entries:
            resolved = path_map.get(tid)
            if resolved:
                _add(resolved, track)
            else:
                skipped.append({"title": str(track.get("title") or ""), "reason": "no local audio file"})

    return entries, skipped


def _export_worker(job: dict) -> None:
    zip_path = os.path.join(tempfile.gettempdir(), f"popularr_export_{job['id']}.zip")
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as archive:
            total = len(job["entries"])
            for index, entry in enumerate(job["entries"], start=1):
                job["current"] = os.path.basename(entry["file_path"])
                job["done"] = index - 1
                try:
                    archive.write(entry["file_path"], entry["arcname"])
                except Exception as exc:
                    job["skipped"].append({"title": entry["arcname"], "reason": str(exc)})
        job["status"] = "done"
        job["zip_path"] = zip_path
        job["done"] = len(job["entries"])
    except Exception as exc:
        job["status"] = "error"
        job["error"] = str(exc)
    finally:
        job["finished"] = time.time()


def create_export_job(playlist_name: str, tracks: list[dict]) -> dict:
    """Start a background ZIP job for a playlist's tracks; returns the job."""
    entries, skipped = _build_export_entries(playlist_name, tracks)
    job: dict = {
        "id": uuid.uuid4().hex,
        "name": playlist_name,
        "status": "pending" if entries else "error",
        "total": len(entries),
        "done": 0,
        "current": "",
        "skipped": skipped,
        "zip_path": None,
        "error": "No local audio files found for this playlist" if not entries else None,
        "created": time.time(),
        "finished": None,
    }
    with _LOCK:
        _prune_locked()
        _JOBS[job["id"]] = job
    if entries:
        asyncio.get_running_loop().create_task(asyncio.to_thread(_export_worker, job))
    return job


def get_export_job(job_id: str) -> dict | None:
    """Public job view (no internal entry list)."""
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return None
        return {key: value for key, value in job.items() if key != "entries"}


def _prune_locked() -> None:
    if len(_JOBS) < _MAX_JOBS:
        return
    oldest = sorted(_JOBS.values(), key=lambda j: j["created"])[: len(_JOBS) - _MAX_JOBS + 1]
    for job in oldest:
        zip_path = job.get("zip_path")
        if zip_path and os.path.isfile(zip_path):
            try:
                os.remove(zip_path)
            except OSError:
                pass
        _JOBS.pop(job["id"], None)
