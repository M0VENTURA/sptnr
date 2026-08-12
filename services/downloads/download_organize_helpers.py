"""Download organisation helpers.

Low-level file operations for organising downloaded tracks into
the music library structure. Handles:
- Moving files from downloads to library.
- Applying naming conventions (``downloads.file_name_format``).
- FLAC -> MP3 conversion (``downloads.conversion.*``).
- Cleaning up source directories.
"""

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

from helpers.config_helpers import get_config

logger = logging.getLogger(__name__)

# ffmpeg conversion guard rail (mirrors old_system's transfer timeout).
_TRANSFER_TIMEOUT_SECONDS = 300


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


def _extract_year_for_path(year) -> str:
    """Pull a 4-digit year out of a (possibly full-date) value."""
    if not year:
        return "Unknown"
    m = re.search(r"(19|20)\d{2}", str(year))
    return m.group(0) if m else "Unknown"


def _format_track_number_for_rename(track_number, disc_number=None) -> str:
    """Format a track number for use in path format strings (2-digit, disc-aware)."""
    try:
        disc_num = int(str(disc_number).split("/")[0]) if disc_number else 1
        track_num = int(str(track_number).split("/")[0]) if track_number else 0
        # Disc "0" is a tagging artifact (many rips tag disc 0 for a single
        # disc) — normalize it to 1 so disc-0 copies land in the SAME folder
        # as disc-1 copies instead of splitting the album in two.
        if disc_num <= 0:
            disc_num = 1
        if disc_num > 1:
            return f"{disc_num}{track_num:02d}"
        return f"{track_num:02d}"
    except Exception:
        return "00"


def _build_target_path(root, album_artist, year, album, artist, title, track_number, source_file, disc_number=None):
    """Build the destination path using ``downloads.file_name_format`` from config.

    The format string may contain ``/`` separators and any of the placeholders
    ``{album_artist}``, ``{year}``, ``{album}``, ``{track_number}``, ``{artist}``,
    ``{title}``.  Falls back to the default layout when rendering fails, and
    sanitizes every path segment so config typos cannot escape the music root.
    """
    ext = os.path.splitext(source_file)[1]
    file_name_format = _read_track_file_name_format()

    track_artist = artist or "Unknown Artist"
    album_artist_val = _normalize_album_artist_for_path(album_artist) or track_artist

    format_vars = {
        "album_artist": _sanitize_path_component(album_artist_val),
        "year": _extract_year_for_path(year),
        "album": _sanitize_path_component(album or "Unknown Album"),
        "track_number": _format_track_number_for_rename(track_number, disc_number),
        "artist": _sanitize_path_component(track_artist),
        "title": _sanitize_path_component(title or "Unknown Title"),
    }

    try:
        relative_path = file_name_format.format(**format_vars)
    except Exception:
        relative_path = (
            f"{format_vars['album_artist']}/{format_vars['year']} - {format_vars['album']}/"
            f"{format_vars['track_number']}. {format_vars['artist']} - {format_vars['title']}"
        )

    # Sanitize each path segment (the config format may contain slashes).
    relative_path = relative_path.strip().replace("\\", "/").lstrip("/")
    parts = []
    for part in relative_path.split("/"):
        clean = _sanitize_path_component(part)
        if clean and clean not in (".", ".."):
            parts.append(clean)
    relative_path = "/".join(parts) or "Unknown Artist"

    return os.path.join(root, f"{relative_path}{ext}")






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


def _read_download_conversion_settings() -> dict:
    """Read ``downloads.conversion`` settings from config with safe defaults.

    Mirrors old_system ``download_queue_manager._read_download_conversion_settings``
    but reads through the central config helper instead of raw YAML.
    """
    settings = {
        "enabled": False,
        "mode": "flac_to_mp3",
        "mp3_bitrate_kbps": 320,
        "original_handling": "move_to_original",
        "original_subfolder": "Original",
    }
    try:
        cfg = get_config() or {}
        conversion_cfg = (cfg.get("downloads") or {}).get("conversion") or {}
        if isinstance(conversion_cfg, dict):
            settings["enabled"] = bool(conversion_cfg.get("enabled", settings["enabled"]))
            mode = str(conversion_cfg.get("mode", settings["mode"]) or settings["mode"]).strip().lower()
            if mode in ("flac_to_mp3", "none"):
                settings["mode"] = mode
            try:
                bitrate = int(conversion_cfg.get("mp3_bitrate_kbps", settings["mp3_bitrate_kbps"]))
                settings["mp3_bitrate_kbps"] = max(96, min(320, bitrate))
            except Exception:
                pass
            handling = str(
                conversion_cfg.get("original_handling", settings["original_handling"])
                or settings["original_handling"]
            ).strip().lower()
            if handling in ("move_to_original", "delete"):
                settings["original_handling"] = handling
            subfolder = str(
                conversion_cfg.get("original_subfolder", settings["original_subfolder"])
                or settings["original_subfolder"]
            ).strip()
            settings["original_subfolder"] = _sanitize_path_component(subfolder) or "Original"
    except Exception as exc:
        logger.debug("Could not read download conversion settings: %s", exc)
    return settings


def _is_under_original_subfolder(path_value, downloads_root, original_subfolder) -> bool:
    """Return True when *path_value* is inside downloads/<original_subfolder>."""
    if not path_value or not downloads_root or not original_subfolder:
        return False
    try:
        abs_path = os.path.abspath(path_value)
        original_root = os.path.abspath(os.path.join(downloads_root, original_subfolder))
        return os.path.commonpath([abs_path, original_root]) == original_root
    except Exception:
        return False


def _build_original_archive_path(source_path, downloads_root, original_subfolder) -> str:
    """Build an archive destination under downloads/<original_subfolder>, preserving the relative path when possible."""
    original_root = os.path.join(downloads_root, original_subfolder)
    abs_source = os.path.abspath(source_path)
    abs_downloads = os.path.abspath(downloads_root)

    try:
        if os.path.commonpath([abs_source, abs_downloads]) == abs_downloads:
            rel = os.path.relpath(abs_source, abs_downloads)
            candidate = os.path.join(original_root, rel)
        else:
            candidate = os.path.join(original_root, os.path.basename(abs_source))
    except Exception:
        candidate = os.path.join(original_root, os.path.basename(abs_source))

    base, ext = os.path.splitext(candidate)
    counter = 1
    unique_candidate = candidate
    while os.path.exists(unique_candidate):
        unique_candidate = f"{base}_{counter}{ext}"
        counter += 1
    return unique_candidate


def _resolve_downloads_root() -> str:
    """Resolve the downloads directory (used for archiving original FLACs)."""
    try:
        from services.infrastructure.filesystem_service import resolve_downloads_dir
        return resolve_downloads_dir(prefer_music_subfolder=False)
    except Exception:
        return os.environ.get("DOWNLOADS_DIR", "/downloads")


def _convert_flac_and_handle_original(source_path, dest_path, settings) -> bool:
    """Convert FLAC -> MP3 via ffmpeg into *dest_path*, then handle the original per config.

    The ffmpeg command carries over source metadata (``-map_metadata 0``) so
    tags written before the move survive the conversion.  The original FLAC is
    either deleted or archived under downloads/<original_subfolder> per
    ``downloads.conversion.original_handling``.  Returns True on success.
    """
    bitrate_kbps = int(settings.get("mp3_bitrate_kbps", 320) or 320)
    cmd = [
        "ffmpeg", "-y", "-i", source_path,
        "-vn", "-codec:a", "libmp3lame", "-b:a", f"{bitrate_kbps}k",
        "-map_metadata", "0", "-id3v2_version", "3", dest_path,
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=_TRANSFER_TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired:
        logger.warning("FLAC→MP3 conversion timed out after %ss: %s", _TRANSFER_TIMEOUT_SECONDS, source_path)
        return False
    except FileNotFoundError:
        logger.warning("ffmpeg is required for FLAC→MP3 conversion but is not available in PATH")
        return False
    except Exception as exc:
        logger.warning("FLAC→MP3 conversion launch failed: %s", exc)
        return False

    if proc.returncode != 0:
        stderr_tail = (proc.stderr or "").strip().splitlines()[-5:]
        logger.warning("FLAC→MP3 conversion failed: %s", " | ".join(stderr_tail))
        return False

    downloads_root = _resolve_downloads_root()
    handling = settings.get("original_handling", "move_to_original")
    subfolder = settings.get("original_subfolder", "Original")
    try:
        if handling == "delete":
            os.remove(source_path)
        elif not _is_under_original_subfolder(source_path, downloads_root, subfolder):
            archive_path = _build_original_archive_path(source_path, downloads_root, subfolder)
            os.makedirs(os.path.dirname(archive_path), exist_ok=True)
            shutil.move(source_path, archive_path)
            logger.info("Archived original FLAC to %s", archive_path)
    except Exception as archive_err:
        logger.warning("Conversion succeeded but original handling failed (%s): %s", source_path, archive_err)

    logger.info("Converted FLAC→MP3: %s → %s", source_path, dest_path)
    return True


def is_match(path: str, item: dict) -> bool:
    filename = os.path.basename(path).lower()
    artist = (item.get("artist") or "").lower()
    title = (item.get("title") or "").lower()
    return artist in filename and title in filename


def _read_file_title(file_path: str) -> str:
    """Embedded title of an existing library file (mutagen via metadata_reader)."""
    try:
        from helpers.metadata_reader import read_mp3_metadata
        meta = read_mp3_metadata(file_path) or {}
        return str(meta.get("title") or "").strip()
    except Exception:
        return ""


def _titles_match(a: str, b: str) -> bool:
    """Case-/whitespace-insensitive title comparison for duplicate detection."""
    norm = lambda v: re.sub(r"\s+", " ", str(v or "").strip().casefold())
    return bool(norm(a) and norm(a) == norm(b))


def move_track_to_library(track, release_metadata, music_root):
    """Move a track into the configured library structure.

    Honors ``downloads.file_name_format`` for the destination name and
    ``downloads.conversion.*`` for FLAC → MP3 conversion (the original FLAC is
    deleted or archived to ``downloads/<original_subfolder>`` per config).

    A pre-existing file at the exact target whose embedded title matches the
    incoming track is a DUPLICATE (a re-download, or a disc-0/disc-1 copy
    whose disc numbers normalize to the same path): the existing file is kept
    and ``duplicate=True`` is returned so the caller imports against it and
    the redundant download is cleaned up.  Genuine collisions (a different
    track with the same generated name) keep the legacy PID-suffix behaviour.
    """
    file_path = track.get("file_path")

    if not file_path:
        return {"success": False, "error": "Missing file_path"}

    conversion_settings = _read_download_conversion_settings()
    source_ext = os.path.splitext(file_path)[1].lower()
    converting = bool(
        conversion_settings.get("enabled")
        and conversion_settings.get("mode") == "flac_to_mp3"
        and source_ext == ".flac"
    )

    target_path = _build_target_path(
        music_root,
        release_metadata.get("album_artist"),
        release_metadata.get("year"),
        release_metadata.get("album"),
        track.get("artist"),
        track.get("title"),
        track.get("track_number"),
        file_path,
        disc_number=track.get("disc_number"),
    )

    if converting:
        target_path = f"{os.path.splitext(target_path)[0]}.mp3"

    if os.path.exists(target_path):
        # Same-track duplicate: the target already holds THIS track (verified
        # by embedded title) — keep it, skip writing, and let the caller
        # import against the existing file.  No conversion work is wasted.
        if _titles_match(_read_file_title(target_path), track.get("title")):
            logger.info(
                "[ORGANIZE] Target '%s' already exists and matches '%s - %s' — skipping duplicate import",
                target_path, track.get("artist"), track.get("title"),
            )
            return {"success": True, "target_path": target_path, "duplicate": True}
        stem = Path(target_path).stem
        suffix = Path(target_path).suffix
        target_path = f"{target_path[:-len(suffix)]}_{os.getpid()}{suffix}" if target_path.endswith(suffix) else f"{target_path}_{os.getpid()}"

    try:
        os.makedirs(os.path.dirname(target_path), exist_ok=True)
        if converting:
            if not _convert_flac_and_handle_original(file_path, target_path, conversion_settings):
                return {"success": False, "error": "FLAC→MP3 conversion failed"}
            return {"success": True, "target_path": target_path, "converted": True}
        shutil.move(file_path, target_path)
    except Exception as exc:
        return {"success": False, "error": str(exc)}

    return {"success": True, "target_path": target_path}

