"""
Audio file tag read/write service.

Physical MP3/FLAC tag writing lives here.
DB tag reads are delegated to repository functions only.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Dict, Any, TYPE_CHECKING, cast


# =============================================================================
# TYPE LAYER (separate from runtime)
# =============================================================================

if TYPE_CHECKING:
    from mutagen.flac import FLAC as FLACType, Picture as FLACPictureType
    from mutagen.id3 import (
        ID3 as ID3Type,
        APIC, COMM, TALB, TBPM, TCOM, TCON,
        TDRC, TIT2, TMOO, TPE1, TPE2, TPOS, TRCK, TXXX
    )
    from mutagen.mp3 import MP3 as MP3Type
else:
    ID3Type = Any
    FLACType = Any
    FLACPictureType = Any


# =============================================================================
# IMPORTS
# =============================================================================

from db.repositories.tag_repository import get_track_tags

logger = logging.getLogger(__name__)


# =============================================================================
# SAFE RUNTIME IMPORTS
# =============================================================================

MUTAGEN_AVAILABLE = True

try:
    from mutagen.flac import FLAC, Picture as FLACPicture
    from mutagen.id3 import (
        ID3,
        APIC, COMM, TALB, TBPM, TCOM, TCON,
        TDRC, TIT2, TMOO, TPE1, TPE2, TPOS, TRCK, TXXX
    )
    from mutagen.mp3 import MP3
except Exception:
    MUTAGEN_AVAILABLE = False

    # ✅ Callable fallback (prevents "not callable" errors)
    class _MissingMutagen:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("Mutagen is not available")

    FLAC = _MissingMutagen
    FLACPicture = _MissingMutagen
    ID3 = _MissingMutagen

    APIC = COMM = TALB = TBPM = TCOM = TCON = _MissingMutagen
    TDRC = TIT2 = TMOO = TPE1 = TPE2 = TPOS = TRCK = TXXX = _MissingMutagen

    MP3 = _MissingMutagen


# =============================================================================
# PUBLIC
# =============================================================================

def write_tags_to_file(file_path: str, tags: Dict[str, Any]) -> bool:
    if not file_path or not os.path.exists(file_path):
        logger.error("File not found: %s", file_path)
        return False

    suffix = Path(file_path).suffix.lower()

    if suffix == ".mp3":
        return write_id3_tags(file_path, tags)

    if suffix == ".flac":
        return write_flac_tags(file_path, tags)

    logger.warning("Unsupported file format: %s", suffix)
    return False


# =============================================================================
# HELPERS
# =============================================================================

def _set_text_frame(tag_obj: ID3Type, frame_id: str, frame_cls, value: Any) -> None:
    if value is None or value == "":
        tag_obj.delall(frame_id)
        return

    tag_obj.delall(frame_id)
    tag_obj.add(frame_cls(encoding=3, text=[str(value)]))


def _clear_txxx_variants(tag_obj: ID3Type, normalized_target: str) -> None:
    def norm(desc: str) -> str:
        return desc.lower().replace(" ", "").replace("_", "").replace("-", "")

    for key in list(tag_obj.keys()):
        if key.startswith("TXXX:") and norm(key[5:]) == normalized_target:
            tag_obj.delall(key)


# =============================================================================
# MP3 / ID3
# =============================================================================

def write_id3_tags(file_path: str, tags: Dict[str, Any]) -> bool:
    if not MUTAGEN_AVAILABLE:
        logger.error("Mutagen not available for ID3 writing")
        return False

    try:
        try:
            audio = cast(MP3Type, MP3(file_path, ID3=ID3))  # type: ignore[arg-type]

            if audio.tags is None:
                audio.add_tags()

            tag_obj = cast(ID3Type, audio.tags)
            save = lambda: audio.save(v2_version=3)

        except Exception:
            try:
                tag_obj = cast(ID3Type, ID3(file_path))
            except Exception:
                tag_obj = cast(ID3Type, ID3())

            save = lambda: tag_obj.save(file_path, v2_version=3)

        for field, value in tags.items():

            if field == "title":
                _set_text_frame(tag_obj, "TIT2", TIT2, value)

            elif field == "artist":
                _set_text_frame(tag_obj, "TPE1", TPE1, value)

            elif field == "album":
                _set_text_frame(tag_obj, "TALB", TALB, value)

            elif field in {"album_artist", "albumartist"}:
                _set_text_frame(tag_obj, "TPE2", TPE2, value)

            elif field == "composer":
                _set_text_frame(tag_obj, "TCOM", TCOM, value)

            elif field == "track_number":
                _set_text_frame(tag_obj, "TRCK", TRCK, value)

            elif field == "disc_number":
                _set_text_frame(tag_obj, "TPOS", TPOS, value)

            elif field in {"year", "date"}:
                _set_text_frame(tag_obj, "TDRC", TDRC, value)

            elif field in {"genre", "genres"}:
                tag_obj.delall("TCON")

                if value:
                    import re

                    if isinstance(value, list):
                        genres = [str(v).strip() for v in value if str(v).strip()]
                    else:
                        genres = [g.strip() for g in re.split(r"[,;/]+", str(value)) if g.strip()]

                    if genres:
                        tag_obj.add(TCON(encoding=3, text=genres))

            elif field == "comment":
                tag_obj.delall("COMM")

                if value:
                    tag_obj.add(COMM(encoding=3, lang="eng", desc="", text=[str(value)]))

            elif field in {"mbid", "musicbrainz_trackid"}:
                _clear_txxx_variants(tag_obj, "musicbrainztrackid")

                if value:
                    tag_obj.add(TXXX(encoding=3, desc="MUSICBRAINZ TRACK ID", text=[str(value)]))

            elif field in {"musicbrainz_album_mbid", "musicbrainz_albumid"}:
                _clear_txxx_variants(tag_obj, "musicbrainzalbumid")

                if value:
                    tag_obj.add(TXXX(encoding=3, desc="MUSICBRAINZ ALBUM ID", text=[str(value)]))

            elif field == "cover_art_data" and value:
                tag_obj.delall("APIC")

                tag_obj.add(
                    APIC(
                        encoding=3,
                        mime=tags.get("cover_art_mime", "image/jpeg"),
                        type=3,
                        desc="Cover",
                        data=bytes(value),
                    )
                )

        save()
        return True

    except Exception as exc:
        logger.error("Failed to write ID3 tags: %s", exc, exc_info=True)
        return False


# =============================================================================
# FLAC
# =============================================================================

def write_flac_tags(file_path: str, tags: Dict[str, Any]) -> bool:
    if not MUTAGEN_AVAILABLE:
        logger.error("Mutagen not available for FLAC writing")
        return False

    try:
        audio = cast(FLACType, FLAC(file_path))

        for field, value in tags.items():
            if not value:
                continue

            audio[field] = [str(value)]

        audio.save()
        return True

    except Exception as exc:
        logger.error("Failed to write FLAC tags: %s", exc, exc_info=True)
        return False


# =============================================================================
# PUBLIC WRAPPERS
# =============================================================================

def update_file_tags(file_path: str, tag_updates: Dict[str, Any]) -> bool:
    if not file_path or not tag_updates:
        return False

    return write_tags_to_file(file_path, tag_updates)


def _get_track_file_path(track_id: str) -> str | None:
    """Return the track's stored ``file_path`` (or None)."""
    try:
        from sqlalchemy import text as _text
        from db.engine import db_session as _db_session
        with _db_session() as session:
            row = session.execute(
                _text("SELECT file_path FROM tracks WHERE CAST(id AS TEXT) = :id"),
                {"id": str(track_id)},
            ).fetchone()
            value = row[0] if row else None
            return str(value).strip() if value and str(value).strip() else None
    except Exception as exc:
        logger.error("Failed to load file path for track %s: %s", track_id, exc)
        return None


def _resolve_music_file_path(path_value: str | None) -> str | None:
    """Resolve a stored path to an absolute existing file.

    Navidrome stores paths relative to its music folder (e.g.
    ``Artist/Album/01 - Track.mp3``); the DB may also hold a fully absolute
    path.  Try the stored value first, then each configured music root joined
    with the relative path.  Returns the first path that exists on disk, or
    None.
    """
    if not path_value:
        return None
    raw = str(path_value).strip()
    if not raw:
        return None

    candidates = [raw]
    if not os.path.isabs(raw):
        for root in [
            os.environ.get("MUSIC_FOLDER"),
            os.environ.get("MUSIC_ROOT"),
            os.environ.get("MUSIC_DIR"),
            "/music",
        ]:
            if root:
                candidates.append(os.path.join(str(root).strip(), raw))

    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if os.path.isfile(candidate):
            return candidate
    return None


def sync_track_tags_to_file(track_id: str) -> bool:
    tags = get_track_tags(track_id)

    file_path = _resolve_music_file_path(_get_track_file_path(track_id))

    if not file_path:
        logger.warning("No audio file resolved for track %s", track_id)
        return False

    # Only write non-empty values — passing None/"" for a frame would make the
    # writer DELETE that frame from the file (see ``_set_text_frame``).
    tags_to_write = {
        k: v for k, v in tags.items()
        if v is not None and str(v).strip() != ""
    }

    if not tags_to_write:
        logger.warning("No editable tags to write for track %s", track_id)
        return False

    return write_tags_to_file(file_path, tags_to_write)


def update_file_metadata(file_path: str, metadata: Dict[str, Any]) -> bool:
    if not file_path:
        return False

    tag_updates = {
        "title": metadata.get("title"),
        "artist": metadata.get("artist"),
        "album": metadata.get("album"),
        "album_artist": metadata.get("album_artist"),
        "track_number": metadata.get("track_number"),
        "disc_number": metadata.get("disc_number"),
        "year": metadata.get("year"),
    }

    if metadata.get("recording_mbid"):
        tag_updates["musicbrainz_trackid"] = metadata.get("recording_mbid")

    if metadata.get("release_mbid"):
        tag_updates["musicbrainz_albumid"] = metadata.get("release_mbid")

    return write_tags_to_file(file_path, tag_updates)


# =============================================================================
# ALBUM ART
# =============================================================================


def embed_album_art(file_path: str, image_data: bytes, mime_type: str = "image/jpeg") -> bool:
    """Embed album art image data into an audio file's tags.

    Args:
        file_path: Path to the audio file (MP3 or FLAC).
        image_data: Raw image bytes.
        mime_type: Image MIME type (``image/jpeg`` or ``image/png``).

    Returns:
        True on success, False otherwise.
    """
    if not file_path or not os.path.exists(file_path) or not image_data:
        return False

    ext = Path(file_path).suffix.lower()
    try:
        if ext == ".mp3":
            from mutagen.id3 import ID3  # type: ignore[attr-defined]
            from mutagen.id3._frames import APIC  # type: ignore[import-untyped]
            audio = MP3(file_path, ID3=ID3)
            if audio.tags is None:
                audio.add_tags()
            audio.tags.delall("APIC")
            audio.tags.add(APIC(
                encoding=3, mime=mime_type, type=3, desc="Cover", data=image_data,
            ))
            audio.save()
            return True

        if ext == ".flac":
            from mutagen.flac import FLAC, Picture
            audio = FLAC(file_path)
            pic = Picture()
            pic.data = image_data
            pic.mime = mime_type
            pic.type = 3
            audio.add_picture(pic)
            audio.save()
            return True

        logger.warning("Unsupported format for album art: %s", ext)
        return False
    except Exception as exc:
        logger.error("Failed to embed album art in %s: %s", file_path, exc, exc_info=True)
        return False


# =============================================================================
# FLAC → MP3 CONVERSION
# =============================================================================


def convert_flac_to_mp3(flac_path: str, bitrate: str = "320k") -> str | None:
    """Convert a FLAC file to MP3 using ffmpeg.

    Args:
        flac_path: Path to the FLAC file.
        bitrate: Target bitrate (default ``"320k"``).

    Returns:
        Path to the converted MP3 file, or ``None`` on failure.
        The original FLAC file is deleted after a successful conversion.
    """
    import subprocess

    if not os.path.exists(flac_path):
        logger.error("FLAC file not found: %s", flac_path)
        return None

    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        logger.error("ffmpeg not available — cannot convert FLAC to MP3")
        return None

    mp3_path = os.path.splitext(flac_path)[0] + ".mp3"
    cmd = [
        "ffmpeg", "-i", flac_path,
        "-b:a", bitrate, "-q:a", "0",
        "-v", "error", "-y", mp3_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=300, text=True)
        if result.returncode != 0:
            logger.error("FLAC→MP3 conversion failed: %s", result.stderr)
            return None
        if not os.path.exists(mp3_path):
            logger.error("Conversion succeeded but output file not found")
            return None

        size_mb = os.path.getsize(mp3_path) / (1024 * 1024)
        logger.info("Converted FLAC→MP3: %s (%.1f MB)", mp3_path, size_mb)

        try:
            os.remove(flac_path)
            logger.debug("Deleted original FLAC: %s", flac_path)
        except Exception as exc:
            logger.warning("Could not delete original FLAC: %s", exc)

        return mp3_path
    except subprocess.TimeoutExpired:
        logger.error("FLAC→MP3 conversion timed out after 300s: %s", flac_path)
        return None
    except Exception as exc:
        logger.error("FLAC→MP3 conversion error: %s", exc, exc_info=True)
        return None