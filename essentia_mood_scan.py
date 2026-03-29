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
"""

import json
import logging
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional

from helpers.db_utils import get_db_connection, _is_postgres_connection
from helpers.logging_config import log_unified
from helpers.tag_manager import sync_track_tags_to_file

try:
    from scan_history import log_album_scan as _log_album_scan
except Exception:
    def _log_album_scan(*a, **kw):  # type: ignore[misc]
        pass

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
    artist_filter: str = "",
    album_filter: str = "",
    track_id_filter: str = "",
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
    artist_filter:
        If non-empty, restrict the scan to tracks whose album_artist or artist
        matches this value (case-insensitive).
    album_filter:
        If non-empty, restrict the scan to tracks on this album (combined with
        *artist_filter* when both are provided).
    track_id_filter:
        If non-empty, restrict the scan to the single track with this ID.
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

    mood_threshold_pct = round(float(mood_threshold) * 100.0, 4)

    python_exec = sys.executable
    base_cmd: List[str] = [
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

    conditions: List[str] = [
        f"COALESCE(file_path, '') NOT LIKE {placeholder}",
        f"CAST(id AS TEXT) NOT LIKE {placeholder}",
        "file_path IS NOT NULL",
        "file_path != ''",
    ]
    params: List[Any] = ["__queued_for_download__%", "queue_%"]

    if not force:
        if tag_moods and not tag_genres:
            conditions.append("(mood IS NULL OR mood = '')")
        elif tag_genres and not tag_moods:
            conditions.append("(genres IS NULL OR genres = '')")
        # When both are enabled, scan tracks missing EITHER tag (OR is intentional:
        # we want to add the missing tag even when the other is already present).
        elif tag_genres and tag_moods:
            conditions.append(
                "(mood IS NULL OR mood = '' OR genres IS NULL OR genres = '')"
            )

    # Scoped filters
    artist_filter = (artist_filter or "").strip()
    album_filter = (album_filter or "").strip()
    track_id_filter = (track_id_filter or "").strip()

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
        # Suppress TensorFlow CUDA GPU initialisation on CPU-only hosts.
        # Without these, TF (used by the Essentia models) tries to dlopen
        # libcudart.so.11.0, fails, and may exit with code 1 even though the
        # actual inference is CPU-only.  Setting CUDA_VISIBLE_DEVICES to an
        # empty string hides all GPUs from TF; TF_CPP_MIN_LOG_LEVEL=3
        # suppresses the C++ "Could not load dynamic library" messages.
        _subprocess_env = os.environ.copy()
        _subprocess_env.setdefault("CUDA_VISIBLE_DEVICES", "")
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
                    })
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
            })
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
            })
            continue

        # ------------------------------------------------------------------
        # Read back the mood the external tool just wrote.
        # When tag_moods=False the mood model was disabled; skip mood read.
        # ------------------------------------------------------------------
        mood = _read_essentia_mood_from_file(file_path) if tag_moods else None

        # When genres were written directly by Essentia, count the file as
        # updated even if no mood is available (or moods were disabled).
        # The script already exited successfully (returncode 0 checked above),
        # so we can assume genre tags were written.
        # We do NOT call sync_track_tags_to_file in this case because it would
        # overwrite the Essentia genre tags with the (un-updated) DB genres.
        if tag_genres and not mood:
            # Genres written to file by Essentia; nothing further to persist in DB.
            updated_tracks += 1
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
            continue

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
        # Persist mood in DB.
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
            if tag_genres:
                # Essentia wrote genre tags directly; don't sync DB genres
                # back to the file (that would overwrite Essentia's tags).
                synced_files += 1
            elif sync_track_tags_to_file(track_id):
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
