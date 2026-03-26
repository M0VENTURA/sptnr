"""Essentia mood scan: local ML mood tagging via the Essentia-to-Metadata script.

This module runs the ``tag_music.py`` script from
https://github.com/WB2024/Essentia-to-Metadata as a subprocess for each
track that has a resolvable ``file_path``, then reads the MOOD tag the
external tool wrote back from the audio file and persists it in the sptnr
database.

Configuration (config.yaml, ``essentia`` section):
    script_path  – absolute path to ``tag_music.py``   (required; skip scan if blank)
    models_dir   – directory containing the Essentia ``.pb`` / ``.json`` models
                   (optional; the external script defaults to ``~/essentia_models``)
    mood_threshold – activation threshold forwarded to the external script
                   (default: 0.005, which is Essentia's own default)
"""

import json
import logging
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional

from helpers.db_utils import get_db_connection, _is_postgres_connection
from helpers.tag_manager import sync_track_tags_to_file

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bundled defaults (set as ENV vars in the official Docker image)
# ---------------------------------------------------------------------------
_BUNDLED_SCRIPT_PATH = "/opt/Essentia-to-Metadata/tag_music.py"
_BUNDLED_MODELS_DIR = "/opt/essentia_models"


# ---------------------------------------------------------------------------
# Helpers shared with mood_scan.py
# ---------------------------------------------------------------------------

def _row_get(row: Any, key: str, index: int = 0, default=None):
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    if hasattr(row, "keys"):
        try:
            return row[key]
        except Exception:
            pass
    try:
        return row[index]
    except Exception:
        return default


def _stop_requested(progress_file: str) -> bool:
    if not progress_file or not os.path.exists(progress_file):
        return False
    try:
        with open(progress_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        return bool(data.get("stop_requested")) or (
            data.get("status") == "stopped" and not bool(data.get("is_running", False))
        )
    except Exception:
        return False


def _write_progress(progress_file: str, payload: Dict[str, Any]) -> None:
    if not progress_file:
        return
    try:
        os.makedirs(os.path.dirname(progress_file), exist_ok=True)
        with open(progress_file, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except Exception as exc:
        logger.debug("Failed writing essentia mood scan progress: %s", exc)


# ---------------------------------------------------------------------------
# Tag reading
# ---------------------------------------------------------------------------

def _read_essentia_mood_from_file(file_path: str) -> Optional[str]:
    """Read the MOOD tag that Essentia-to-Metadata wrote to *file_path*.

    Returns the first (highest-confidence) mood label as a plain string, or
    ``None`` if nothing was found.
    """
    try:
        import mutagen  # noqa: F401 – presence check
        from mutagen.flac import FLAC
        from mutagen.id3 import ID3
        from mutagen.mp4 import MP4
        from mutagen.oggvorbis import OggVorbis
        from mutagen.oggopus import OggOpus
        from mutagen.asf import ASF
    except ImportError as exc:
        logger.warning("mutagen not available, cannot read Essentia tags: %s", exc)
        return None

    ext = os.path.splitext(file_path)[1].lower()
    mood: Optional[str] = None

    try:
        if ext == ".flac":
            audio = FLAC(file_path)
            raw = audio.get("MOOD") or audio.get("mood")
            mood = raw[0] if raw else None

        elif ext == ".mp3":
            audio = ID3(file_path)
            # Essentia-to-Metadata writes MP3 mood as a COMM ID3 frame with
            # desc='Essentia Mood' (see TagWriter._write_id3_tags in tag_music.py).
            for tag in audio.getall("COMM"):
                if getattr(tag, "desc", "") == "Essentia Mood":
                    texts = getattr(tag, "text", [])
                    if texts:
                        mood = str(texts[0])
                    break

        elif ext in (".ogg", ".oga"):
            audio = OggVorbis(file_path)
            raw = audio.get("MOOD") or audio.get("mood")
            mood = raw[0] if raw else None

        elif ext == ".opus":
            audio = OggOpus(file_path)
            raw = audio.get("MOOD") or audio.get("mood")
            mood = raw[0] if raw else None

        elif ext in (".m4a", ".m4b", ".mp4", ".aac"):
            audio = MP4(file_path)
            raw = audio.get("----:com.apple.iTunes:MOOD")
            if raw:
                first = raw[0]
                mood = first.decode("utf-8") if hasattr(first, "decode") else str(first)

        elif ext == ".wma":
            audio = ASF(file_path)
            raw = audio.get("WM/Mood")
            mood = str(raw[0]) if raw else None

        else:
            import mutagen as mg
            audio = mg.File(file_path)
            if audio and audio.tags:
                for key in ("Mood", "MOOD", "mood"):
                    val = audio.tags.get(key)
                    if val:
                        mood = str(val[0]) if isinstance(val, list) else str(val)
                        break

    except Exception as exc:
        logger.debug("Failed to read Essentia mood from %s: %s", file_path, exc)
        return None

    if not mood:
        return None
    # The tag may contain multiple moods separated by "; " — take the first.
    primary = mood.split(";")[0].strip()
    return primary if primary else None


# ---------------------------------------------------------------------------
# Main scan
# ---------------------------------------------------------------------------

def run_essentia_mood_scan(
    script_path: str = "",
    models_dir: str = "",
    mood_threshold: float = 0.005,
    per_file_timeout: int = 300,
    force: bool = False,
    progress_file: str = "",
    tag_genres: bool = False,
) -> Dict[str, Any]:
    """Run Essentia-based mood detection on all tracks with a local file path.

    Parameters
    ----------
    script_path:
        Absolute path to ``tag_music.py`` from Essentia-to-Metadata.
        If empty the function returns immediately with an error.
    models_dir:
        Optional override for the Essentia models directory.  When empty the
        external script uses its own default (``~/essentia_models``).
    mood_threshold:
        Minimum activation probability forwarded to the external script via
        ``--mood-threshold``.
    per_file_timeout:
        Maximum seconds to wait for the external script to finish processing a
        single file.  Defaults to 300 s (5 min); increase for large files or
        slow hardware.
    force:
        When ``True`` re-analyse every track even if it already has a mood.
    progress_file:
        Path to a JSON progress file (shared with the Flask UI).
    tag_genres:
        When ``True`` the ``--no-genres`` flag is omitted so the external
        script also writes genre tags from the Essentia genre models (requires
        the genre ``.pb`` / ``.json`` models to be present in ``models_dir``).
    """
    # ------------------------------------------------------------------
    # Validate script_path early so we surface a clear error.
    # ------------------------------------------------------------------
    script_path = (script_path or "").strip()
    if not script_path:
        _env = os.environ.get("ESSENTIA_SCRIPT_PATH", "").strip()
        if _env and os.path.isfile(_env):
            script_path = _env
    if not script_path and os.path.isfile(_BUNDLED_SCRIPT_PATH):
        script_path = _BUNDLED_SCRIPT_PATH
    if not script_path:
        msg = (
            "Essentia script path is not configured. "
            "Set 'essentia.script_path' in config.yaml to the path of tag_music.py."
        )
        logger.error(msg)
        _write_progress(progress_file, {
            "is_running": False,
            "scan_type": "essentia_mood_scan",
            "status": "error",
            "error": msg,
        })
        return {"stopped": False, "error": msg,
                "processed_artists": 0, "total_artists": 0,
                "scanned_tracks": 0, "updated_tracks": 0, "synced_files": 0}

    if not os.path.isfile(script_path):
        msg = f"Essentia script not found at: {script_path}"
        logger.error(msg)
        _write_progress(progress_file, {
            "is_running": False,
            "scan_type": "essentia_mood_scan",
            "status": "error",
            "error": msg,
        })
        return {"stopped": False, "error": msg,
                "processed_artists": 0, "total_artists": 0,
                "scanned_tracks": 0, "updated_tracks": 0, "synced_files": 0}

    # ------------------------------------------------------------------
    # Build the base subprocess command.
    # ------------------------------------------------------------------
    models_dir = (models_dir or "").strip()
    if not models_dir:
        _env = os.environ.get("ESSENTIA_MODELS_DIR", "").strip()
        if _env and os.path.isdir(_env):
            models_dir = _env
    if not models_dir and os.path.isdir(_BUNDLED_MODELS_DIR):
        models_dir = _BUNDLED_MODELS_DIR

    python_exec = sys.executable
    base_cmd: List[str] = [
        python_exec, script_path,
        "--auto",
        "--single-file",
        "--overwrite",
        "--quiet",
        "--mood-threshold", str(mood_threshold),
    ]
    if not tag_genres:
        base_cmd.append("--no-genres")
    if models_dir:
        base_cmd += ["--model-dir", os.path.expanduser(models_dir)]

    # ------------------------------------------------------------------
    # Query tracks.
    # ------------------------------------------------------------------
    conn = get_db_connection()
    cursor = conn.cursor()
    placeholder = "%s" if _is_postgres_connection(conn) else "?"

    conditions: List[str] = [
        f"COALESCE(file_path, '') NOT LIKE {placeholder}",
        f"CAST(id AS TEXT) NOT LIKE {placeholder}",
        "file_path IS NOT NULL",
        "file_path != ''",
    ]
    params: List[Any] = ["__queued_for_download__%", "queue_%"]

    if not force:
        conditions.append("(mood IS NULL OR mood = '')")

    where_sql = " AND ".join(conditions)
    cursor.execute(
        f"""
        SELECT id, title, album, artist, album_artist, file_path, mood
        FROM tracks
        WHERE {where_sql}
        ORDER BY COALESCE(NULLIF(album_artist, ''), artist), album, track_number, title
        """,
        tuple(params),
    )
    rows = cursor.fetchall() or []

    # Collect distinct artist keys for progress reporting.
    artists: List[str] = []
    seen: set = set()
    for row in rows:
        artist_key = (
            _row_get(row, "album_artist", 4) or _row_get(row, "artist", 3) or "Unknown"
        ).strip()
        if artist_key not in seen:
            seen.add(artist_key)
            artists.append(artist_key)

    total_artists = len(artists)
    processed_artists = 0
    scanned_tracks = 0
    updated_tracks = 0
    synced_files = 0
    current_artist: Optional[str] = None

    for row in rows:
        if _stop_requested(progress_file):
            conn.commit()
            conn.close()
            _write_progress(progress_file, {
                "is_running": False,
                "scan_type": "essentia_mood_scan",
                "status": "stopped",
                "processed_artists": processed_artists,
                "total_artists": total_artists,
                "scanned_tracks": scanned_tracks,
                "updated_tracks": updated_tracks,
                "synced_files": synced_files,
                "current_artist": current_artist,
            })
            return {
                "stopped": True,
                "processed_artists": processed_artists,
                "total_artists": total_artists,
                "scanned_tracks": scanned_tracks,
                "updated_tracks": updated_tracks,
                "synced_files": synced_files,
            }

        track_id = _row_get(row, "id", 0)
        file_path = _row_get(row, "file_path", 5)
        artist_key = (
            _row_get(row, "album_artist", 4) or _row_get(row, "artist", 3) or "Unknown"
        ).strip()

        if artist_key != current_artist:
            current_artist = artist_key
            processed_artists = min(processed_artists + 1, total_artists)

        scanned_tracks += 1

        # Skip if file no longer exists.
        if not file_path or not os.path.isfile(file_path):
            _write_progress(progress_file, {
                "is_running": True,
                "scan_type": "essentia_mood_scan",
                "status": "running",
                "processed_artists": processed_artists,
                "total_artists": total_artists,
                "scanned_tracks": scanned_tracks,
                "updated_tracks": updated_tracks,
                "synced_files": synced_files,
                "current_artist": current_artist,
            })
            continue

        # ------------------------------------------------------------------
        # Run Essentia on this file.
        # ------------------------------------------------------------------
        cmd = base_cmd + [file_path]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=per_file_timeout,
            )
            if result.returncode != 0:
                logger.warning(
                    "Essentia script returned exit code %d for %s: %s",
                    result.returncode, file_path,
                    (result.stderr or "").strip()[:300],
                )
                _write_progress(progress_file, {
                    "is_running": True,
                    "scan_type": "essentia_mood_scan",
                    "status": "running",
                    "processed_artists": processed_artists,
                    "total_artists": total_artists,
                    "scanned_tracks": scanned_tracks,
                    "updated_tracks": updated_tracks,
                    "synced_files": synced_files,
                    "current_artist": current_artist,
                })
                continue
        except subprocess.TimeoutExpired:
            logger.warning("Essentia script timed out for %s", file_path)
            _write_progress(progress_file, {
                "is_running": True,
                "scan_type": "essentia_mood_scan",
                "status": "running",
                "processed_artists": processed_artists,
                "total_artists": total_artists,
                "scanned_tracks": scanned_tracks,
                "updated_tracks": updated_tracks,
                "synced_files": synced_files,
                "current_artist": current_artist,
            })
            continue
        except Exception as exc:
            logger.warning("Error running Essentia script for %s: %s", file_path, exc)
            _write_progress(progress_file, {
                "is_running": True,
                "scan_type": "essentia_mood_scan",
                "status": "running",
                "processed_artists": processed_artists,
                "total_artists": total_artists,
                "scanned_tracks": scanned_tracks,
                "updated_tracks": updated_tracks,
                "synced_files": synced_files,
                "current_artist": current_artist,
            })
            continue

        # ------------------------------------------------------------------
        # Read back the mood the external tool just wrote.
        # ------------------------------------------------------------------
        mood = _read_essentia_mood_from_file(file_path)
        if not mood:
            _write_progress(progress_file, {
                "is_running": True,
                "scan_type": "essentia_mood_scan",
                "status": "running",
                "processed_artists": processed_artists,
                "total_artists": total_artists,
                "scanned_tracks": scanned_tracks,
                "updated_tracks": updated_tracks,
                "synced_files": synced_files,
                "current_artist": current_artist,
            })
            continue

        # ------------------------------------------------------------------
        # Persist in DB.
        # ------------------------------------------------------------------
        cursor.execute(
            f"""
            UPDATE tracks
            SET mood = {placeholder},
                mood_confidence = {placeholder},
                mood_source = {placeholder},
                mood_last_updated = CURRENT_TIMESTAMP
            WHERE id = {placeholder}
            """,
            (mood, None, "essentia", track_id),
        )

        if cursor.rowcount and cursor.rowcount > 0:
            updated_tracks += 1
            conn.commit()
            if sync_track_tags_to_file(track_id):
                synced_files += 1

        _write_progress(progress_file, {
            "is_running": True,
            "scan_type": "essentia_mood_scan",
            "status": "running",
            "processed_artists": processed_artists,
            "total_artists": total_artists,
            "scanned_tracks": scanned_tracks,
            "updated_tracks": updated_tracks,
            "synced_files": synced_files,
            "current_artist": current_artist,
        })

    conn.commit()
    conn.close()

    return {
        "stopped": False,
        "processed_artists": processed_artists,
        "total_artists": total_artists,
        "scanned_tracks": scanned_tracks,
        "updated_tracks": updated_tracks,
        "synced_files": synced_files,
    }
