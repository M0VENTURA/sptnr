"""Essentia mood scan: local ML mood tagging via the Essentia-to-Metadata script.

This module runs the ``tag_music.py`` script from
https://github.com/WB2024/Essentia-to-Metadata as a subprocess for each
track that has a resolvable ``file_path``, then reads the MOOD (and
optionally GENRE) tag the external tool wrote back from the audio file and
persists it in the sptnr database.

Configuration (config.yaml, ``essentia`` section):
    script_path     – absolute path to ``tag_music.py``  (required; skip scan if blank)
    models_dir      – directory containing the Essentia ``.pb`` / ``.json`` models
                      (optional; the external script defaults to ``~/essentia_models``)
    mood_threshold  – minimum activation probability for a mood tag (raw value 0.001–0.5;
                      default: 0.005 = 0.5%).  Converted to percent when passed to CLI.
    per_file_timeout – seconds to wait per file before skipping (default: 300)
    tag_genres      – also write genre tags (default: False)
    num_genres      – number of genre tags to write per track (default: 3)
    genre_threshold – genre confidence threshold in percent (default: 15.0)
    genre_format    – genre tag format: parent_child | child_parent | child_only | raw
                      (default: parent_child)
    tag_moods       – write mood tags (default: True).  Set False for genres-only.
    parse_json_features – import BPM / danceability from Essentia JSON sidecars (default: True)
    delete_json_after_import – delete consumed Essentia JSON sidecars after import (default: False)
    json_output_dir – optional directory where Essentia writes JSON sidecars
"""

import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from helpers.db_utils import get_db_connection, _is_postgres_connection
from helpers.logging_config import log_unified
from helpers.tag_manager import sync_track_tags_to_file, update_file_tags

try:
    from scan_history import log_album_scan as _log_album_scan
except Exception:
    def _log_album_scan(*a, **kw):  # type: ignore[misc]
        pass

try:
    import psycopg2 as _psycopg2
    _PG_OPERATIONAL_ERROR: Any = _psycopg2.OperationalError
except ImportError:
    _PG_OPERATIONAL_ERROR = None

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bundled defaults (set as ENV vars in the official Docker image)
# ---------------------------------------------------------------------------
_BUNDLED_SCRIPT_PATH = "/opt/Essentia-to-Metadata/tag_music.py"
_BUNDLED_MODELS_DIR = "/opt/essentia_models"
# Phrase emitted by MusicExtractorSVM when no SVM classifier models are
# configured.  Essentia exits with code 1 in this case even though
# MusicExtractor itself ran successfully and may have written tags.
# This constant centralises the detection string so it stays in sync if the
# Essentia message wording ever changes.
_ESSENTIA_NO_MODELS_PHRASE = "no classifier models were configured by default"

# Separator used by Essentia-to-Metadata for hierarchical genre labels
# (e.g. "Rock---Heavy Metal").
_ESSENTIA_GENRE_SEPARATOR = "---"


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

    Returns all mood labels as a semicolon-separated string (the script may
    write up to three moods), or ``None`` if nothing was found.
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
    # Return the full semicolon-separated mood string (may contain multiple moods).
    return mood.strip() if mood.strip() else None


def _read_essentia_genre_from_file(file_path: str) -> Optional[str]:
    """Read the GENRE tag that Essentia-to-Metadata wrote to *file_path*.

    tag_music.py writes genres to the standard GENRE / ©gen field.
    Returns a semicolon-separated string of genre labels, or ``None``.
    """
    try:
        import mutagen
        from mutagen.flac import FLAC
        from mutagen.id3 import ID3
        from mutagen.mp4 import MP4
        from mutagen.oggvorbis import OggVorbis
        from mutagen.oggopus import OggOpus
        from mutagen.asf import ASF
    except ImportError as exc:
        logger.warning("mutagen not available, cannot read Essentia genre: %s", exc)
        return None

    ext = os.path.splitext(file_path)[1].lower()
    genres: Optional[str] = None

    try:
        if ext == ".flac":
            audio = FLAC(file_path)
            raw = audio.get("GENRE") or audio.get("genre")
            if raw:
                genres = "; ".join(str(v) for v in raw if v)

        elif ext == ".mp3":
            audio = ID3(file_path)
            tcon = audio.get("TCON")
            if tcon:
                texts = getattr(tcon, "text", [])
                if texts:
                    genres = "; ".join(str(t) for t in texts if t)

        elif ext in (".ogg", ".oga"):
            audio = OggVorbis(file_path)
            raw = audio.get("GENRE") or audio.get("genre")
            if raw:
                genres = "; ".join(str(v) for v in raw if v)

        elif ext == ".opus":
            audio = OggOpus(file_path)
            raw = audio.get("GENRE") or audio.get("genre")
            if raw:
                genres = "; ".join(str(v) for v in raw if v)

        elif ext in (".m4a", ".m4b", ".mp4", ".aac"):
            audio = MP4(file_path)
            raw = audio.get("\xa9gen")
            if raw:
                genres = "; ".join(str(v) for v in raw if v)

        elif ext == ".wma":
            audio = ASF(file_path)
            raw = audio.get("WM/Genre")
            if raw:
                genres = "; ".join(str(v) for v in raw if v)

        else:
            audio = mutagen.File(file_path)
            if audio and audio.tags:
                for key in ("Genre", "GENRE", "genre", "\xa9gen"):
                    val = audio.tags.get(key)
                    if val:
                        genres = "; ".join(str(v) for v in (val if isinstance(val, list) else [val]) if v)
                        break

    except Exception as exc:
        logger.debug("Failed to read Essentia genre from %s: %s", file_path, exc)
        return None

    return genres.strip() if genres and genres.strip() else None


def _extract_child_genres(genre_str: str) -> List[str]:
    """Extract child genre labels from an Essentia hierarchical genre string.

    Handles formats like:
      - "Rock---Heavy Metal; Rock---Death Metal"
      - "Rock---Heavy Metal: 22.04%, Rock---Death Metal: 16.75%"
      - "Heavy Metal; Death Metal"  (already child-only)

    Returns a deduplicated list of child genre labels.
    """
    if not genre_str:
        return []

    child_genres: List[str] = []
    seen_lower: set = set()
    # Split on semicolons or commas
    for part in re.split(r"[;,]", genre_str):
        part = part.strip()
        if not part:
            continue
        # Strip trailing confidence percentage: "Heavy Metal: 22.04%"
        part = re.sub(r":\s*\d+\.?\d*\s*%?\s*$", "", part).strip()
        # Extract the child part from "Parent---Child" hierarchy
        if _ESSENTIA_GENRE_SEPARATOR in part:
            child = part.split(_ESSENTIA_GENRE_SEPARATOR, 1)[-1].strip()
        else:
            child = part
        if child and child.lower() not in seen_lower:
            seen_lower.add(child.lower())
            child_genres.append(child)
    return child_genres


def _read_existing_tcon_genres(file_path: str) -> List[str]:
    """Read the existing TCON/genre tags from a file, excluding hierarchical Essentia entries."""
    try:
        from mutagen.id3 import ID3
        from mutagen.flac import FLAC
        from mutagen.mp4 import MP4
        from mutagen.oggvorbis import OggVorbis
        from mutagen.oggopus import OggOpus
        from mutagen.asf import ASF
    except ImportError:
        return []

    ext = os.path.splitext(file_path)[1].lower()
    raw_genres: List[str] = []

    try:
        if ext == ".mp3":
            audio = ID3(file_path)
            tcon = audio.get("TCON")
            if tcon:
                raw_genres = [str(t).strip() for t in getattr(tcon, "text", []) if str(t).strip()]
        elif ext == ".flac":
            audio = FLAC(file_path)
            raw = audio.get("GENRE") or audio.get("genre") or []
            raw_genres = [str(v).strip() for v in raw if str(v).strip()]
        elif ext in (".ogg", ".oga"):
            audio = OggVorbis(file_path)
            raw = audio.get("GENRE") or audio.get("genre") or []
            raw_genres = [str(v).strip() for v in raw if str(v).strip()]
        elif ext == ".opus":
            audio = OggOpus(file_path)
            raw = audio.get("GENRE") or audio.get("genre") or []
            raw_genres = [str(v).strip() for v in raw if str(v).strip()]
        elif ext in (".m4a", ".m4b", ".mp4", ".aac"):
            audio = MP4(file_path)
            raw = audio.get("\xa9gen") or []
            raw_genres = [str(v).strip() for v in raw if str(v).strip()]
        elif ext == ".wma":
            audio = ASF(file_path)
            raw = audio.get("WM/Genre") or []
            raw_genres = [str(v).strip() for v in raw if str(v).strip()]
    except Exception as exc:
        logger.debug("Failed to read existing genres from %s: %s", file_path, exc)

    # Filter out hierarchical Essentia entries ("Parent---Child") so they are
    # replaced by the clean child-only values.
    return [g for g in raw_genres if _ESSENTIA_GENRE_SEPARATOR not in g]


def _merge_genres(existing: List[str], new_genres: List[str]) -> List[str]:
    """Merge two genre lists, preserving order and avoiding case-insensitive duplicates."""
    seen_lower = {g.lower() for g in existing}
    result = list(existing)
    for g in new_genres:
        if g.lower() not in seen_lower:
            seen_lower.add(g.lower())
            result.append(g)
    return result


def _coerce_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        if isinstance(value, str):
            value = value.strip()
            if not value:
                return None
        return float(value)
    except Exception:
        return None


def _read_numeric_tag_from_file(file_path: str, *keys: str) -> Optional[float]:
    """Read a numeric metadata tag from common audio formats."""
    try:
        import mutagen
        audio = mutagen.File(file_path)
        if not audio or not audio.tags:
            return None

        for key in keys:
            raw = audio.tags.get(key) or audio.tags.get(key.lower()) or audio.tags.get(key.upper())
            if raw is None:
                continue
            if isinstance(raw, list) and raw:
                value = _coerce_float(raw[0])
                if value is not None:
                    return value
            if hasattr(raw, "text"):
                text_values = getattr(raw, "text", [])
                if text_values:
                    value = _coerce_float(text_values[0])
                    if value is not None:
                        return value
            value = _coerce_float(raw)
            if value is not None:
                return value
    except Exception as exc:
        logger.debug("Failed to read numeric tag from %s: %s", file_path, exc)
    return None


def _candidate_sidecar_paths(file_path: str, json_output_dir: str = "") -> List[Path]:
    src = Path(file_path)
    candidates = [
        src.with_suffix(src.suffix + ".json"),
        src.with_suffix(".json"),
        src.parent / f"{src.stem}.json",
    ]
    if json_output_dir:
        out = Path(json_output_dir)
        candidates.extend([
            out / f"{src.name}.json",
            out / f"{src.stem}.json",
        ])

    dedup: List[Path] = []
    seen = set()
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        dedup.append(path)
    return dedup


def _extract_first_numeric(payload: Any, key_candidates: List[str]) -> Optional[float]:
    """Recursively scan dict/list payload for first numeric key match."""
    if isinstance(payload, dict):
        for key in key_candidates:
            if key in payload:
                value = _coerce_float(payload.get(key))
                if value is not None:
                    return value
        for value in payload.values():
            nested = _extract_first_numeric(value, key_candidates)
            if nested is not None:
                return nested
    elif isinstance(payload, list):
        for item in payload:
            nested = _extract_first_numeric(item, key_candidates)
            if nested is not None:
                return nested
    return None


def _read_essentia_features_from_json(file_path: str, json_output_dir: str = "") -> Dict[str, Any]:
    """Read BPM / danceability from Essentia JSON sidecar if available."""
    bpm_keys = [
        "bpm", "tempo", "rhythm.bpm", "musicbrainz.bpm",
    ]
    dance_keys = [
        "danceability", "rhythm.danceability", "highlevel.danceability.all.danceable",
    ]

    for candidate in _candidate_sidecar_paths(file_path, json_output_dir=json_output_dir):
        if not candidate.is_file():
            continue
        try:
            with candidate.open("r", encoding="utf-8") as f:
                payload = json.load(f)
            bpm = _extract_first_numeric(payload, bpm_keys)
            danceability = _extract_first_numeric(payload, dance_keys)
            return {
                "bpm": bpm,
                "danceability": danceability,
                "json_path": str(candidate),
            }
        except Exception as exc:
            logger.debug("Failed parsing Essentia JSON sidecar %s: %s", candidate, exc)
    return {"bpm": None, "danceability": None, "json_path": None}


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
    num_genres: int = 3,
    genre_threshold: float = 15.0,
    genre_format: str = "parent_child",
    tag_moods: bool = True,
    parse_json_features: bool = True,
    delete_json_after_import: bool = False,
    json_output_dir: str = "",
    artist_filter: str = "",
    album_filter: str = "",
    track_id_filter: str = "",
    resume_from_artist: str = "",
    cpu_nice: int = 10,
    inter_file_delay: float = 0.0,
) -> Dict[str, Any]:
    """Run Essentia-based mood/genre detection on tracks with a local file path.

    Parameters
    ----------
    script_path:
        Absolute path to ``tag_music.py`` from Essentia-to-Metadata.
        If empty the function returns immediately with an error.
    models_dir:
        Optional override for the Essentia models directory.  When empty the
        external script uses its own default (``~/essentia_models``).
    mood_threshold:
        Minimum activation probability (raw, 0.001–0.5) for a mood tag.
        Converted to percentage before being forwarded via ``--mood-threshold``.
    per_file_timeout:
        Maximum seconds to wait for the external script to finish processing a
        single file.  Defaults to 300 s (5 min); increase for large files or
        slow hardware.
    force:
        When ``True`` re-analyse every track even if it already has a mood/genre.
    progress_file:
        Path to a JSON progress file (shared with the Flask UI).
    tag_genres:
        When ``True`` the ``--no-genres`` flag is omitted so the external
        script also writes genre tags.  Defaults to ``False`` (mood-only).
    num_genres:
        Number of genre tags to write per track (passed via ``--genres``).
        Only used when *tag_genres* is ``True``.
    genre_threshold:
        Genre confidence threshold in percent (passed via ``--genre-threshold``).
        Only used when *tag_genres* is ``True``.
    genre_format:
        Genre tag format style forwarded via ``--genre-format``.
        Choices: ``parent_child``, ``child_parent``, ``child_only``, ``raw``.
        Only used when *tag_genres* is ``True``.
    tag_moods:
        When ``False`` the ``--no-moods`` flag is added so the external script
        skips mood analysis (genres-only run).  Defaults to ``True``.
    parse_json_features:
        When ``True`` attempt to import BPM and danceability values from
        Essentia-generated JSON sidecars.
    delete_json_after_import:
        When ``True`` delete consumed Essentia JSON sidecars after values are
        persisted to DB and tags.
    json_output_dir:
        Optional directory where Essentia sidecar JSON files are written.
    artist_filter:
        If non-empty, restrict the scan to tracks whose album_artist or artist
        matches this value (case-insensitive).
    album_filter:
        If non-empty, restrict the scan to tracks on this album (combined with
        *artist_filter* when both are provided).
    track_id_filter:
        If non-empty, restrict the scan to the single track with this ID.
    cpu_nice:
        Unix process-priority increment passed to ``nice -n`` for each
        per-file subprocess (0 = normal priority, 19 = lowest).  Has no
        effect on Windows.  Defaults to 10 so the scan yields to
        interactive processes.
    inter_file_delay:
        Seconds to sleep between consecutive file-processing subprocesses.
        Use this as a throttle on very constrained hardware where even a
        low-nice process causes noticeable latency.  Defaults to 0.0
        (no extra delay).
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
        log_unified(f"Essentia Scan - Error: {msg}", level=logging.ERROR)
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
        log_unified(f"Essentia Scan - Error: {msg}", level=logging.ERROR)
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
    # mood_threshold is stored as a raw probability (e.g. 0.005 = 0.5 %).
    # tag_music.py --mood-threshold expects a *percentage* value (e.g. 0.5).
    # ------------------------------------------------------------------
    models_dir = (models_dir or "").strip()
    if not models_dir:
        _env = os.environ.get("ESSENTIA_MODELS_DIR", "").strip()
        if _env and os.path.isdir(_env):
            models_dir = _env
    if not models_dir and os.path.isdir(_BUNDLED_MODELS_DIR):
        models_dir = _BUNDLED_MODELS_DIR

    # Determine a version string that identifies the Essentia build used for
    # this scan.  Stored in essentia_model_version on each updated track row so
    # stale analyses from an older model version can be detected and re-run.
    # Suppress TensorFlow CUDA initialization warnings in the main process
    # before importing essentia (essentia-tensorflow pulls in TF, which tries
    # to dlopen libcudart on import even on CPU-only hosts).
    os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")
    os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")
    _essentia_scan_version: Optional[str] = None
    try:
        import essentia  # type: ignore[import]
        _essentia_scan_version = getattr(essentia, "__version__", None)
    except Exception:
        pass
    if not _essentia_scan_version:
        # Fall back to the script's mtime as a proxy for "which version of the
        # external script was used", formatted as an ISO date string.
        try:
            from datetime import datetime as _datetime, timezone as _tz
            _mtime = os.path.getmtime(script_path)
            _essentia_scan_version = "script-" + _datetime.fromtimestamp(_mtime, tz=_tz.utc).strftime("%Y%m%d")
        except Exception:
            _essentia_scan_version = "unknown"

    mood_threshold_pct = round(float(mood_threshold) * 100.0, 4)

    # Build the CPU-throttling prefix for per-file subprocesses.
    # ``nice -n N`` lowers the OS scheduling priority so the scan yields to
    # interactive processes.  Only supported on Unix; silently skipped on
    # Windows or when cpu_nice is 0.
    _nice_prefix: List[str] = []
    _cpu_nice = int(cpu_nice)
    if _cpu_nice != 0 and sys.platform != "win32":
        _nice_prefix = ["nice", "-n", str(max(-20, min(19, _cpu_nice)))]

    python_exec = sys.executable
    base_cmd: List[str] = _nice_prefix + [
        python_exec, script_path,
        "--auto",
        "--single-file",
        "--overwrite",
        "--quiet",
    ]
    if tag_moods:
        base_cmd += ["--mood-threshold", str(mood_threshold_pct)]
    else:
        base_cmd.append("--no-moods")

    if not tag_genres:
        base_cmd.append("--no-genres")
    else:
        valid_formats = {"parent_child", "child_parent", "child_only", "raw"}
        fmt = (genre_format or "parent_child").strip()
        if fmt not in valid_formats:
            fmt = "parent_child"
        base_cmd += [
            "--genres", str(max(1, int(num_genres))),
            "--genre-threshold", str(float(genre_threshold)),
            "--genre-format", fmt,
        ]
    if models_dir:
        base_cmd += ["--model-dir", os.path.expanduser(models_dir)]

    # ------------------------------------------------------------------
    # Query tracks.
    # ------------------------------------------------------------------
    conn = get_db_connection()
    cursor = conn.cursor()
    placeholder = "%s"

    # Ensure the essentia_genres column exists (safe no-op if already present).
    try:
        cursor.execute(
            "ALTER TABLE tracks ADD COLUMN IF NOT EXISTS essentia_genres TEXT"
        )
        conn.commit()
    except Exception as _col_err:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.debug("Could not ensure essentia_genres column: %s", _col_err)

    conditions: List[str] = [
        f"COALESCE(file_path, '') NOT LIKE {placeholder}",
        f"CAST(id AS TEXT) NOT LIKE {placeholder}",
        "file_path IS NOT NULL",
        "file_path != ''",
    ]
    params: List[Any] = ["__queued_for_download__%", "queue_%"]

    if not force:
        if tag_moods and not tag_genres:
            # Skip tracks that already have a mood from Essentia.
            conditions.append("(mood IS NULL OR mood = '')")
        elif tag_genres and not tag_moods:
            # Skip tracks that already have Essentia genres.
            conditions.append("(essentia_genres IS NULL OR essentia_genres = '')")
        # When both are enabled, only skip tracks where BOTH are already populated.
        # A track missing either essentia value still needs a scan.
        elif tag_genres and tag_moods:
            conditions.append(
                "(mood IS NULL OR mood = '' OR essentia_genres IS NULL OR essentia_genres = '')"
            )

    # Scoped filters
    artist_filter = (artist_filter or "").strip()
    album_filter = (album_filter or "").strip()
    track_id_filter = (track_id_filter or "").strip()
    resume_from_artist = (resume_from_artist or "").strip()

    # Auto-resume: when no explicit resume point was given, check the progress
    # file for a mid-scan checkpoint (e.g. after a server restart mid-scan).
    if not resume_from_artist and not artist_filter and not album_filter and not track_id_filter:
        if progress_file and os.path.exists(progress_file):
            try:
                with open(progress_file, "r", encoding="utf-8") as _fp:
                    _saved = json.load(_fp)
                _saved_status = _saved.get("status", "")
                _saved_checkpoint = (_saved.get("resume_from_artist") or "").strip()
                if _saved_status not in ("complete", "completed", "stopped") and _saved_checkpoint:
                    resume_from_artist = _saved_checkpoint
                    logger.info(
                        "Essentia auto-resume: continuing from checkpoint artist '%s'",
                        resume_from_artist,
                    )
                    log_unified(
                        f"Essentia Scan - Auto-resuming from checkpoint artist '{resume_from_artist}'"
                    )
            except Exception as _pr_err:
                logger.debug("Could not read essentia resume checkpoint: %s", _pr_err)

    if track_id_filter:
        conditions.append(f"CAST(id AS TEXT) = {placeholder}")
        params.append(track_id_filter)
    else:
        if artist_filter:
            conditions.append(
                f"(LOWER(COALESCE(album_artist, '')) = LOWER({placeholder})"
                f" OR LOWER(COALESCE(artist, '')) = LOWER({placeholder}))"
            )
            params += [artist_filter, artist_filter]
        if album_filter:
            conditions.append(f"LOWER(COALESCE(album, '')) = LOWER({placeholder})")
            params.append(album_filter)

    where_sql = " AND ".join(conditions)
    cursor.execute(
        f"""
        SELECT id, title, album, artist, album_artist, file_path, mood, essentia_genres
        FROM tracks
        WHERE {where_sql}
        ORDER BY COALESCE(NULLIF(album_artist, ''), artist), album, track_number, title
        """,
        tuple(params),
    )
    rows = cursor.fetchall() or []
    # Close the SELECT transaction immediately so the connection transitions to
    # "idle" (not "idle in transaction") before the per-file Essentia
    # subprocesses start.  Each subprocess can take up to per_file_timeout
    # seconds; with idle_in_transaction_session_timeout=60s the connection
    # would otherwise be killed long before the first UPDATE is issued.
    conn.commit()

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
    current_album: Optional[str] = None
    _album_scan_count: int = 0
    total_tracks = len(rows)

    log_unified(
        f"Essentia Scan - Starting Essentia Scan"
        f" ({total_tracks} track(s) across {total_artists} artist(s))"
    )

    _essentia_milestones_logged: set = set()
    _essentia_milestone_25 = max(1, int(total_tracks * 0.25))
    _essentia_milestone_50 = max(1, int(total_tracks * 0.50))
    _essentia_milestone_75 = max(1, int(total_tracks * 0.75))

    resume_started = not bool(resume_from_artist)

    for row in rows:
        if _stop_requested(progress_file):
            conn.commit()
            conn.close()
            log_unified(
                f"Essentia Scan - Stopped"
                f" ({scanned_tracks}/{total_tracks} tracks scanned,"
                f" {updated_tracks} updated)"
            )
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

        # Resume support: skip artists until we reach the checkpoint artist.
        if not resume_started:
            if artist_key.lower() != resume_from_artist.lower():
                continue
            resume_started = True
            logger.info("Essentia resume: continuing from artist '%s'", artist_key)
            log_unified(f"Essentia Scan - Resuming from Artist {artist_key}")

        # Resolve relative paths stored by the Navidrome importer.
        # Navidrome's Subsonic API returns paths relative to the music root
        # (e.g. "Artist/Album/01 - Title.flac").  os.path.isfile() requires an
        # absolute path, so resolve against the configured music folder.
        if file_path and not os.path.isabs(file_path):
            _music_root = (
                os.environ.get("MUSIC_FOLDER")
                or os.environ.get("MUSIC_ROOT")
                or "/music"
            )
            file_path = os.path.join(_music_root, file_path)

        album_key = (_row_get(row, "album", 2) or "").strip()
        if artist_key != current_artist or album_key != current_album:
            if current_album is not None and _album_scan_count > 0:
                _log_album_scan(
                    current_artist or "Unknown", current_album,
                    "essentia-mood", _album_scan_count, "completed",
                )
            _album_scan_count = 0
            current_album = album_key
            if artist_key != current_artist:
                current_artist = artist_key
                processed_artists = min(processed_artists + 1, total_artists)
                log_unified(
                    f"Essentia Scan - Scanning Artist {current_artist}"
                    f" ({processed_artists}/{total_artists})"
                )
                # Persist the current artist as a resume checkpoint so a restart
                # can skip already-processed artists.
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
                    "resume_from_artist": current_artist,
                })

        scanned_tracks += 1
        _album_scan_count += 1

        # Milestone progress reporting (25 / 50 / 75 %)
        if scanned_tracks == _essentia_milestone_25 and 25 not in _essentia_milestones_logged:
            log_unified(f"Essentia Scan - 25% completed - {scanned_tracks}/{total_tracks} tracks")
            _essentia_milestones_logged.add(25)
        elif scanned_tracks == _essentia_milestone_50 and 50 not in _essentia_milestones_logged:
            log_unified(f"Essentia Scan - 50% completed - {scanned_tracks}/{total_tracks} tracks")
            _essentia_milestones_logged.add(50)
        elif scanned_tracks == _essentia_milestone_75 and 75 not in _essentia_milestones_logged:
            log_unified(f"Essentia Scan - 75% completed - {scanned_tracks}/{total_tracks} tracks")
            _essentia_milestones_logged.add(75)

        # Skip if file no longer exists.
        if not file_path or not os.path.isfile(file_path):
            logger.debug(
                "Essentia scan: skipping track %s — file not found at path %r",
                track_id,
                file_path,
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
                "resume_from_artist": current_artist,
            })
            continue

        # ------------------------------------------------------------------
        # Capture existing genre tags before Essentia overwrites the file.
        # ------------------------------------------------------------------
        pre_existing_genres: List[str] = (
            _read_existing_tcon_genres(file_path) if tag_genres else []
        )

        # ------------------------------------------------------------------
        # Run Essentia on this file.
        # ------------------------------------------------------------------
        cmd = base_cmd + [file_path]
        # Suppress TensorFlow CUDA GPU initialisation on CPU-only hosts.
        # Without these, TF (used by the Essentia models) tries to dlopen
        # libcudart.so.11.0, fails, and may exit with code 1 even though the
        # actual inference is CPU-only.  Setting CUDA_VISIBLE_DEVICES to an
        # empty string hides all GPUs from TF; TF_CPP_MIN_LOG_LEVEL=3
        # suppresses the C++ "Could not load dynamic library" messages.
        _subprocess_env = os.environ.copy()
        _subprocess_env.setdefault("CUDA_VISIBLE_DEVICES", "-1")
        _subprocess_env.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=per_file_timeout,
                env=_subprocess_env,
            )
            if result.returncode != 0:
                stderr_text = (result.stderr or "").strip()
                stdout_text = (result.stdout or "").strip()
                # MusicExtractorSVM prints an INFO-level message and exits with
                # code 1 when no SVM classifier models are configured.  This is
                # benign: MusicExtractor itself still ran and may have written
                # tags to the file.  The message may appear on stdout or stderr
                # depending on the Essentia version/configuration.  Treat it as
                # a soft warning so we don't skip the file and lose any tags
                # that were successfully written.
                _combined_output = (stderr_text + "\n" + stdout_text).lower()
                if result.returncode == 1 and _ESSENTIA_NO_MODELS_PHRASE in _combined_output:
                    logger.debug(
                        "Essentia: no SVM classifier models configured for %s "
                        "(exit code 1 ignored — continuing to read tags)",
                        file_path,
                    )
                    # Fall through so mood/genre tags are still read below.
                else:
                    logger.warning(
                        "Essentia script returned exit code %d for %s: %s",
                        result.returncode, file_path,
                        (stderr_text or stdout_text)[:300],
                    )
                    log_unified(
                        f"Essentia Scan - Error processing {os.path.basename(file_path)}"
                        f" (exit code {result.returncode})",
                        level=logging.WARNING,
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
                        "resume_from_artist": current_artist,
                    })
                    if inter_file_delay > 0:
                        time.sleep(inter_file_delay)
                    continue
        except subprocess.TimeoutExpired:
            logger.warning("Essentia script timed out for %s", file_path)
            log_unified(
                f"Essentia Scan - Timeout processing {os.path.basename(file_path)}",
                level=logging.WARNING,
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
                "resume_from_artist": current_artist,
            })
            if inter_file_delay > 0:
                time.sleep(inter_file_delay)
            continue
        except Exception as exc:
            logger.warning("Error running Essentia script for %s: %s", file_path, exc)
            log_unified(
                f"Essentia Scan - Error processing {os.path.basename(file_path)}: {exc}",
                level=logging.WARNING,
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
                "resume_from_artist": current_artist,
            })
            if inter_file_delay > 0:
                time.sleep(inter_file_delay)
            continue

        # ------------------------------------------------------------------
        # Read back mood/features written by Essentia.
        # ------------------------------------------------------------------
        mood = _read_essentia_mood_from_file(file_path) if tag_moods else None
        essentia_genre = _read_essentia_genre_from_file(file_path) if tag_genres else None
        file_bpm = _read_numeric_tag_from_file(file_path, "bpm", "tempo", "TBPM")
        file_danceability = _read_numeric_tag_from_file(
            file_path,
            "danceability",
            "TXXX:DANCEABILITY",
        )

        sidecar_features = {"bpm": None, "danceability": None, "json_path": None}
        if parse_json_features:
            sidecar_features = _read_essentia_features_from_json(
                file_path, json_output_dir=json_output_dir
            )

        bpm_value = (
            file_bpm if file_bpm is not None else _coerce_float(sidecar_features.get("bpm"))
        )
        danceability_value = (
            file_danceability
            if file_danceability is not None
            else _coerce_float(sidecar_features.get("danceability"))
        )
        json_path = sidecar_features.get("json_path")

        has_mood_update = bool(mood)
        has_genre_update = bool(essentia_genre)
        has_feature_update = bpm_value is not None or danceability_value is not None

        # No updates of any kind – skip this track.
        if not has_mood_update and not has_genre_update and not has_feature_update:
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
                "resume_from_artist": current_artist,
            })
            if inter_file_delay > 0:
                time.sleep(inter_file_delay)
            continue

        # ------------------------------------------------------------------
        # Persist DB fields updated by this scan.
        # ------------------------------------------------------------------
        set_clauses: List[str] = []
        query_params: List[Any] = []

        if has_mood_update:
            set_clauses.extend([
                f"mood = {placeholder}",
                f"mood_confidence = {placeholder}",
                f"mood_source = {placeholder}",
                "mood_last_updated = CURRENT_TIMESTAMP",
            ])
            query_params.extend([mood, None, "essentia"])

        if bpm_value is not None:
            set_clauses.append(f"bpm = {placeholder}")
            query_params.append(float(bpm_value))

        if danceability_value is not None:
            set_clauses.append(f"danceability = {placeholder}")
            query_params.append(float(danceability_value))

        if has_genre_update:
            set_clauses.append(f"essentia_genres = {placeholder}")
            query_params.append(essentia_genre)

        if has_feature_update or has_genre_update:
            set_clauses.append("essentia_last_updated = CURRENT_TIMESTAMP")
            if _essentia_scan_version:
                set_clauses.append(f"essentia_model_version = {placeholder}")
                query_params.append(_essentia_scan_version)

        query_params.append(track_id)

        _update_query = (
            f"UPDATE tracks SET {', '.join(set_clauses)} WHERE id = {placeholder}"
        )
        _update_params = tuple(query_params)

        # Execute the UPDATE, with a single reconnect+retry if PostgreSQL drops
        # the connection mid-scan (e.g. server restart or idle timeout).
        # conn/cursor are function-scoped variables; reassigning them here makes
        # all subsequent loop iterations and the final commit/close use the
        # fresh connection automatically.
        try:
            cursor.execute(_update_query, _update_params)
        except Exception as _db_exc:
            if not (_PG_OPERATIONAL_ERROR and isinstance(_db_exc, _PG_OPERATIONAL_ERROR)):
                raise
            logger.warning(
                "Essentia scan: DB connection lost, reconnecting (track %s): %s",
                track_id, _db_exc,
            )
            log_unified("Essentia Scan - DB connection lost, reconnecting…")
            try:
                conn.close()
            except Exception:
                pass
            conn = get_db_connection()
            cursor = conn.cursor()
            # Second attempt; let any error propagate naturally.
            cursor.execute(_update_query, _update_params)

        # Always commit after the UPDATE to end the transaction before the
        # next per-file subprocess call.  If conn.commit() were only called
        # when rowcount > 0, the connection would sit idle-in-transaction
        # during each subprocess.run() that follows (up to per_file_timeout
        # seconds), triggering idle_in_transaction_session_timeout kills.
        conn.commit()

        if cursor.rowcount and cursor.rowcount > 0:
            updated_tracks += 1

            if tag_genres:
                # Write proper (child-only) genre tags and TXXX:MOOD to the
                # file, merging with any genres already present.
                selective_updates: Dict[str, Any] = {}
                if bpm_value is not None:
                    selective_updates["bpm"] = float(bpm_value)
                if danceability_value is not None:
                    selective_updates["danceability"] = float(danceability_value)

                # Extract child genres (e.g. "Heavy Metal" from "Rock---Heavy Metal")
                # and merge with existing non-hierarchical genres that were captured
                # before the Essentia script ran (it uses --overwrite which would
                # otherwise erase the original genres from the file).
                child_genres = _extract_child_genres(essentia_genre) if essentia_genre else []
                if child_genres:
                    selective_updates["genres"] = _merge_genres(pre_existing_genres, child_genres)

                # Write mood to the standard TMOO frame (Navidrome-compatible)
                # in addition to any COMM frame the external script may have written.
                if mood:
                    selective_updates["mood"] = mood

                if selective_updates:
                    if update_file_tags(file_path, selective_updates):
                        synced_files += 1
                else:
                    synced_files += 1
            else:
                # Mood-only mode: write TMOO frame so Navidrome and players
                # recognise it, then do the full DB->file sync for all other fields.
                if mood:
                    update_file_tags(file_path, {"mood": mood})
                if sync_track_tags_to_file(track_id):
                    synced_files += 1

            if parse_json_features and delete_json_after_import and json_path:
                try:
                    if os.path.isfile(json_path):
                        os.remove(json_path)
                except Exception as exc:
                    logger.debug("Failed to delete Essentia JSON sidecar %s: %s", json_path, exc)

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
            "resume_from_artist": current_artist,
        })
        if inter_file_delay > 0:
            time.sleep(inter_file_delay)

    if current_album is not None and _album_scan_count > 0:
        _log_album_scan(
            current_artist or "Unknown", current_album,
            "essentia-mood", _album_scan_count, "completed",
        )

    conn.commit()
    conn.close()

    log_unified(
        f"Essentia Scan - Completed"
        f" ({scanned_tracks} tracks scanned,"
        f" {updated_tracks} updated,"
        f" {synced_files} file tags synced)"
    )

    return {
        "stopped": False,
        "processed_artists": processed_artists,
        "total_artists": total_artists,
        "scanned_tracks": scanned_tracks,
        "updated_tracks": updated_tracks,
        "synced_files": synced_files,
    }
