#!/usr/bin/env python3
"""Helper functions for scanning and rating operations."""

import logging
import json
import os
import re
import time
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo
from .db_utils import get_db_connection, _is_postgres_connection
from colorama import Fore, Style
from .logging_config import log_debug, log_info, log_unified
from api_clients.navidrome import NavidromeClient

try:
    from scan_history import log_album_scan
    _scan_history_available = True
except ImportError as e:
    # Fallback if scan_history module not available
    logging.warning(f"scan_history module not available: {e}")
    _scan_history_available = False
    def log_album_scan(*args, **kwargs):
        logging.debug(f"log_album_scan called but scan_history not available: {args}")


# Color constants
LIGHT_RED = Fore.RED + Style.BRIGHT
LIGHT_GREEN = Fore.GREEN + Style.BRIGHT
LIGHT_BLUE = Fore.BLUE + Style.BRIGHT
LIGHT_YELLOW = Fore.YELLOW + Style.BRIGHT
LIGHT_CYAN = Fore.CYAN + Style.BRIGHT
RESET = Style.RESET_ALL

# Configuration constants
PROGRESS_UPDATE_INTERVAL = 10  # Update progress every N items
API_RATE_LIMIT_DELAY = 0.1  # Delay between API calls to avoid rate limiting
LOCAL_TZ = os.environ.get("TIMEZONE") or os.environ.get("TZ") or "UTC"

def _now_local_iso() -> str:
    """Return ISO timestamp in configured local timezone."""
    try:
        return datetime.now(ZoneInfo(LOCAL_TZ)).isoformat()
    except Exception:
        return datetime.now().isoformat()


def _normalize_artist_key(value: str) -> str:
    """Normalize artist text for matching equivalent variants."""
    if not value:
        return ""
    text = unicodedata.normalize("NFKD", str(value))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _clean_artist_name_for_storage(value: str) -> str:
    """Conservative canonicalization for artist/album_artist fields."""
    if not value:
        return ""

    cleaned = " ".join(str(value).strip().split())
    if not cleaned:
        return ""

    # Collapse multi-valued artist fields that can appear from malformed metadata.
    # Keep exactly one canonical artist token.
    # Supported separators are intentionally conservative to avoid breaking names
    # like "AC/DC" (no surrounding spaces) while handling values like
    # "Artist A • Artist B", "Artist A / Artist B", "Artist A | Artist B".
    parts = [
        p.strip() for p in re.split(r"\s*[•·]+\s*|\s+[/|;]+\s+", cleaned) if p.strip()
    ]
    if len(parts) > 1:
        buckets = {}
        for idx, part in enumerate(parts):
            key = _normalize_artist_key(part)
            if not key:
                continue
            if key not in buckets:
                buckets[key] = {
                    "count": 0,
                    "first_index": idx,
                    "value": part,
                }
            buckets[key]["count"] += 1

        if buckets:
            various_keys = {
                "various artists",
                "various",
                "va",
                "v a",
                "compilation",
                "original soundtrack",
                "soundtrack",
            }

            def _sort_key(item):
                key, info = item
                is_various = 1 if key in various_keys else 0
                # Prefer: highest frequency, then Various Artists-like token,
                # then earliest appearance.
                return (info["count"], is_various, -info["first_index"])

            best_key, best_info = max(buckets.items(), key=_sort_key)
            cleaned = best_info["value"]

    # Normalize pure alpha-numeric all-caps/all-lower variants (e.g. EELS/eels -> Eels).
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9' .-]*", cleaned):
        letters = re.sub(r"[^A-Za-z]+", "", cleaned)
        if letters and (letters.islower() or letters.isupper()):
            cleaned = cleaned.title()

    return " ".join(cleaned.split())


def _normalize_existing_artist_rows(conn, canonical_artist_name: str, aliases: list[str] | None = None) -> int:
    """Rewrite known case/variant aliases for a scanned artist to one canonical value."""
    if not canonical_artist_name:
        return 0

    canonical_key = _normalize_artist_key(canonical_artist_name)
    if not canonical_key:
        return 0

    cursor = conn.cursor()
    placeholder = "%s"
    updates = 0

    alias_candidates = set(a for a in (aliases or []) if a)
    alias_candidates.update({
        canonical_artist_name,
        canonical_artist_name.lower(),
        canonical_artist_name.upper(),
        canonical_artist_name.title(),
    })

    for original in alias_candidates:
        if not original:
            continue
        if _normalize_artist_key(original) != canonical_key:
            continue
        if original == canonical_artist_name:
            continue

        for col in ["album_artist", "artist"]:
            cursor.execute(
                f"UPDATE tracks SET {col} = {placeholder} WHERE {col} = {placeholder}",
                (canonical_artist_name, original),
            )
            updates += max(cursor.rowcount or 0, 0)

    if updates:
        conn.commit()
    return updates


def _normalize_album_artist_file_tag(file_path: str, album_artist_value: str) -> bool:
    """Best-effort normalization of album artist tag for imported files (MP3/FLAC)."""
    if not file_path or not album_artist_value or not os.path.exists(file_path):
        return False

    try:
        lower_path = file_path.lower()
        if lower_path.endswith('.mp3'):
            from mutagen.easyid3 import EasyID3
            from mutagen.id3 import ID3NoHeaderError

            try:
                audio = EasyID3(file_path)
            except ID3NoHeaderError:
                return False

            current = (audio.get('albumartist') or [''])[0].strip() if audio.get('albumartist') else ''
            if current == album_artist_value:
                return False

            audio['albumartist'] = [album_artist_value]
            audio.save()
            return True

        if lower_path.endswith('.flac'):
            from mutagen.flac import FLAC

            audio = FLAC(file_path)
            current_values = audio.get('albumartist', [])
            current = current_values[0].strip() if current_values else ''
            if current == album_artist_value:
                return False

            audio['albumartist'] = [album_artist_value]
            audio.save()
            return True
    except Exception as e:
        logging.debug(f"[ARTIST_NORMALIZE] Could not normalize album artist tag for {file_path}: {e}")

    return False


def _resolve_navidrome_file_path_for_storage(path_value: str) -> str:
    """Normalize Navidrome path values into stable absolute DB paths."""
    raw = str(path_value or "").strip()
    if not raw:
        return ""
    if raw.startswith("__queued_for_download__"):
        return raw

    normalized = raw.replace("\\", "/")
    if os.path.isabs(normalized):
        return os.path.normpath(normalized)

    music_root = os.environ.get("MUSIC_FOLDER") or os.environ.get("MUSIC_ROOT") or "/music"
    return os.path.normpath(os.path.join(music_root, normalized))


def _sanitize_artist_file_paths_and_duplicates(conn, artist_name: str) -> tuple[int, int]:
    """Fix legacy Navidrome path variants and collapse duplicate rows by normalized file path."""
    if not artist_name:
        return 0, 0

    cursor = conn.cursor()
    placeholder = "%s"
    cursor.execute(
        f"""
        SELECT id, file_path, mbid, suggested_mbid, last_scanned
        FROM tracks
        WHERE COALESCE(NULLIF(album_artist, ''), artist) = {placeholder}
        """,
        (artist_name,),
    )
    rows = cursor.fetchall()

    updates = 0
    buckets = {}

    for row in rows:
        row_dict = dict(row) if hasattr(row, "keys") else {
            "id": row[0],
            "file_path": row[1],
            "mbid": row[2],
            "suggested_mbid": row[3],
            "last_scanned": row[4],
        }
        track_id = row_dict.get("id")
        file_path = str(row_dict.get("file_path") or "").strip()
        if not track_id or not file_path:
            continue

        normalized_path = _resolve_navidrome_file_path_for_storage(file_path)
        if normalized_path and normalized_path != file_path:
            cursor.execute(
                f"UPDATE tracks SET file_path = {placeholder} WHERE id = {placeholder}",
                (normalized_path, track_id),
            )
            updates += int(cursor.rowcount or 0)

        effective_path = normalized_path or file_path
        if not effective_path or effective_path.startswith("__queued_for_download__"):
            continue

        buckets.setdefault(effective_path.lower(), []).append({
            "id": track_id,
            "mbid": str(row_dict.get("mbid") or "").strip(),
            "suggested_mbid": str(row_dict.get("suggested_mbid") or "").strip(),
            "last_scanned": str(row_dict.get("last_scanned") or ""),
        })

    deleted = 0
    for _, dup_rows in buckets.items():
        if len(dup_rows) <= 1:
            continue

        def _score(entry):
            return (
                1 if entry.get("mbid") else 0,
                1 if entry.get("suggested_mbid") else 0,
                entry.get("last_scanned") or "",
                str(entry.get("id") or ""),
            )

        keeper = max(dup_rows, key=_score)
        keeper_id = str(keeper.get("id"))
        for entry in dup_rows:
            entry_id = str(entry.get("id"))
            if entry_id == keeper_id:
                continue
            cursor.execute(f"DELETE FROM tracks WHERE id = {placeholder}", (entry_id,))
            deleted += int(cursor.rowcount or 0)

    if updates or deleted:
        conn.commit()

    return updates, deleted


def _extract_writer_from_file_tags(file_path: str) -> str:
    """Best-effort writer/lyricist extraction from local audio tags (returns JSON array string)."""
    if not file_path or not os.path.exists(file_path):
        return "[]"

    writer_aliases = [
        "TWRT", "TOLY", "TXXX:WRITER", "TXXX:LYRICIST", "TXXX:AUTHOR",
        "WRITER", "LYRICIST", "AUTHOR", "\u00a9wrt"
    ]

    try:
        writers = []
        lower_path = file_path.lower()

        if lower_path.endswith('.mp3'):
            from mutagen.mp3 import MP3
            from mutagen.id3 import ID3

            audio = MP3(file_path, ID3=ID3)
            tags = audio.tags
            if tags:
                for alias in writer_aliases:
                    if alias.startswith("TXXX:"):
                        desc = alias.split(":", 1)[1].upper()
                        for frame in tags.values():
                            if frame.FrameID == 'TXXX' and hasattr(frame, 'desc'):
                                if frame.desc and frame.desc.upper() == desc and hasattr(frame, 'text') and frame.text:
                                    value = str(frame.text[0])
                                    writers.extend([w.strip() for w in value.replace(';', ',').split(',') if w.strip()])
                    elif alias in tags:
                        frame = tags[alias]
                        if hasattr(frame, 'text') and frame.text:
                            value = str(frame.text[0])
                            writers.extend([w.strip() for w in value.replace(';', ',').split(',') if w.strip()])
                    elif alias.startswith("\u00a9") and alias in tags:
                        frame = tags[alias]
                        if hasattr(frame, 'text') and frame.text:
                            value = str(frame.text[0])
                            writers.extend([w.strip() for w in value.replace(';', ',').split(',') if w.strip()])

        elif lower_path.endswith('.flac'):
            from mutagen.flac import FLAC

            audio = FLAC(file_path)
            for alias in writer_aliases:
                if alias.startswith(("TXXX", "\u00a9")):
                    continue
                key = alias.upper()
                if key in audio and audio[key]:
                    for value in audio[key]:
                        writers.extend([w.strip() for w in str(value).replace(';', ',').split(',') if w.strip()])

        seen = set()
        unique_writers = []
        for writer in writers:
            token = writer.lower()
            if token and token not in seen:
                seen.add(token)
                unique_writers.append(writer)

        if unique_writers:
            return json.dumps(unique_writers)
    except Exception as e:
        logging.debug(f"[WRITER] Could not read writer tags from {file_path}: {e}")

    return "[]"


def _backfill_from_file_tags(file_path: str, extracted: dict) -> None:
    """
    Backfill missing metadata fields in *extracted* by reading the audio file directly.

    Navidrome's standard Subsonic API (``/rest/getAlbum.view``) does not expose many
    extended tags such as ReplayGain, R128, MusicBrainz IDs, record label, copyright,
    release country, etc.  This function fills any fields that are currently empty in
    *extracted* with the corresponding values read from the local file via
    ``metadata_reader.read_mp3_metadata``.  Values already provided by Navidrome are
    never overridden.

    Args:
        file_path: Absolute path to the audio file on disk.
        extracted:  Metadata dict returned by ``NavidromeClient.extract_track_metadata``;
                    mutated in-place.
    """
    if not file_path or not os.path.exists(file_path):
        return

    # Fields to attempt to backfill from file tags when Navidrome didn't expose them.
    # Ordered roughly by importance / likelihood of being missing.
    # Note: musicbrainz_album_mbid is handled via alias after the loop; do not list it here.
    _BACKFILL_FIELDS = [
        # MusicBrainz IDs
        "musicbrainz_albumid",
        "musicbrainz_albumartistid",
        "musicbrainz_releasegroupid", "musicbrainz_releasetrackid",
        "musicbrainz_workid", "musicbrainz_artistid", "musicbrainz_trackid",
        "musicbrainz_releasecountry", "musicbrainz_albumstatus", "musicbrainz_albumtype",
        # ReplayGain / R128
        "replaygain_track_gain", "replaygain_track_peak",
        "replaygain_album_gain", "replaygain_album_peak",
        "r128_track_gain", "r128_album_gain",
        # Release / catalogue info
        "recordlabel", "copyright", "releasedate", "originaldate", "originalyear",
        "releasecountry", "media", "barcode", "catalognumber", "asin", "script",
        "language", "explicitstatus", "releasetype", "releasestatus",
        # Track structure
        "tracktotal", "disctotal", "discsubtitle", "grouping", "subtitle", "key",
        "movement", "movementname", "movementtotal", "albumversion", "compilation",
        # Technical / encoding
        "encodedby", "encodersettings", "license", "website", "isrc", "lyrics",
        # Credits
        "composer", "lyricist", "conductor", "remixer", "producer", "arranger",
        "mixer", "engineer", "director", "djmixer", "performer",
        # Sort tags
        "titlesort", "albumsort", "artistsort", "albumartistsort",
        "albumartistssort", "artistssort", "composersort", "lyricistsort",
    ]

    # Avoid reading the file at all when every target field already has a value.
    needs_backfill = any(not extracted.get(field) for field in _BACKFILL_FIELDS)
    if not needs_backfill:
        return

    try:
        from helpers.metadata_reader import read_mp3_metadata
        file_meta = read_mp3_metadata(file_path)
    except Exception as exc:
        logging.debug(f"[BACKFILL] Could not read file tags from {file_path}: {exc}")
        return

    if not file_meta:
        return

    backfilled = []
    for field in _BACKFILL_FIELDS:
        if not extracted.get(field) and file_meta.get(field):
            extracted[field] = file_meta[field]
            backfilled.append(field)

    # ``label`` is a separate DB column that mirrors recordlabel; derive it when absent.
    if not extracted.get("label") and extracted.get("recordlabel"):
        extracted["label"] = extracted["recordlabel"]
        backfilled.append("label")

    # musicbrainz_album_mbid is a legacy alias for musicbrainz_albumid — always keep them
    # identical so the two DB columns never diverge regardless of which one the file backfill
    # populated.
    if extracted.get("musicbrainz_albumid"):
        if extracted.get("musicbrainz_album_mbid") != extracted["musicbrainz_albumid"]:
            extracted["musicbrainz_album_mbid"] = extracted["musicbrainz_albumid"]
            backfilled.append("musicbrainz_album_mbid")
    elif extracted.get("musicbrainz_album_mbid"):
        extracted["musicbrainz_albumid"] = extracted["musicbrainz_album_mbid"]
        backfilled.append("musicbrainz_albumid")

    if backfilled:
        logging.debug(
            f"[BACKFILL] Filled {len(backfilled)} field(s) from file tags for "
            f"'{file_path}': {', '.join(backfilled)}"
        )


def _resolve_navidrome_file_path_for_storage(raw_file_path: str, music_root: str) -> str:
    """Normalize Navidrome file paths to a stable absolute path for DB storage."""
    file_path = str(raw_file_path or "").strip()
    if not file_path:
        return ""

    normalized = file_path.replace("\\", "/")
    if normalized.startswith("__queued_for_download__"):
        return normalized
    if os.path.isabs(normalized):
        return os.path.normpath(normalized)

    rel = normalized.lstrip("/")
    if rel.lower().startswith("music/"):
        rel = rel[6:]

    music_root_clean = os.path.normpath(str(music_root or "/music"))
    return os.path.normpath(os.path.join(music_root_clean, rel))


def _sanitize_artist_file_paths_and_duplicates(conn, artist_name: str) -> dict:
    """Normalize stale relative paths and collapse duplicate rows by normalized file path."""
    try:
        cursor = conn.cursor()
        placeholder = "%s"
        cursor.execute(
            f"""
            SELECT id, file_path, title, album, track_number, duration, mbid, last_scanned
            FROM tracks
            WHERE COALESCE(NULLIF(album_artist, ''), artist) = {placeholder}
              AND file_path IS NOT NULL
              AND TRIM(file_path) != ''
            """,
            (artist_name,),
        )
        rows = [dict(r) for r in cursor.fetchall()]
        if not rows:
            return {"path_updates": 0, "duplicates_removed": 0}

        music_root = os.environ.get("MUSIC_FOLDER") or os.environ.get("MUSIC_ROOT", "/music")
        normalized_to_rows = {}
        path_updates = 0

        for row in rows:
            raw_path = str(row.get("file_path") or "")
            normalized = _resolve_navidrome_file_path_for_storage(raw_path, music_root)
            if normalized and normalized != raw_path:
                cursor.execute(
                    f"UPDATE tracks SET file_path = {placeholder} WHERE id = {placeholder}",
                    (normalized, row.get("id")),
                )
                path_updates += 1
            if normalized:
                normalized_to_rows.setdefault(normalized, []).append({**row, "file_path": normalized})

        duplicates_removed = 0
        for _, dup_rows in normalized_to_rows.items():
            if len(dup_rows) <= 1:
                continue

            def _row_score(r):
                mbid_bonus = 100 if str(r.get("mbid") or "").strip() else 0
                duration_bonus = 10 if r.get("duration") not in (None, "", 0, "0") else 0
                last_scanned = str(r.get("last_scanned") or "")
                return (mbid_bonus + duration_bonus, last_scanned, str(r.get("id") or ""))

            keeper = max(dup_rows, key=_row_score)
            for row in dup_rows:
                if str(row.get("id")) == str(keeper.get("id")):
                    continue
                cursor.execute(
                    f"DELETE FROM tracks WHERE id = {placeholder}",
                    (row.get("id"),),
                )
                duplicates_removed += 1

        if path_updates or duplicates_removed:
            conn.commit()

        return {"path_updates": path_updates, "duplicates_removed": duplicates_removed}
    except Exception as e:
        logging.debug(f"[NAVIDROME_SANITIZE] Failed for '{artist_name}': {e}")
        return {"path_updates": 0, "duplicates_removed": 0}

def save_navidrome_scan_progress(current_artist, processed_artists, total_artists,
                                 progress_file: str = None, scan_type: str = "navidrome_scan"):
    """Save Navidrome scan progress to JSON file (using artist list for progress tracking).

    Args:
        progress_file: Override the target file (default: NAVIDROME_PROGRESS_FILE env var).
                       Pass the caller's own progress file when invoked from a combined scan
                       so that navidrome_scan_progress.json is not written to.
        scan_type: Override the scan_type written into the progress file.
    """
    try:
        target_file = progress_file or os.environ.get(
            "NAVIDROME_PROGRESS_FILE", "/database/navidrome_scan_progress.json"
        )
        progress = {
            "current_artist": current_artist,
            "processed_artists": processed_artists,
            "total_artists": total_artists,
            "is_running": True,
            "scan_type": scan_type,
            "percent_complete": int((processed_artists / total_artists * 100)) if total_artists > 0 else 0,
            "last_updated": datetime.now().isoformat(),
        }
        with open(target_file, 'w') as f:
            json.dump(progress, f, indent=2)
    except Exception as e:
        logging.error(f"Failed to save Navidrome scan progress: {e}")

def _artist_folder_mtime_gate(artist_name: str) -> bool:
    """Return True if the artist's album folders have not changed since last scan.

    This is a *hint* only – it avoids fetching Navidrome payloads when the local
    filesystem shows no directory-level changes (files added/removed/renamed).
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT file_path, last_scanned
            FROM tracks
            WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s
              AND file_path IS NOT NULL
              AND TRIM(file_path) <> ''
              AND file_path NOT LIKE '__queued_for_download__%%'
            """,
            (artist_name,),
        )
        rows = cursor.fetchall()
        conn.close()

        if not rows:
            return False  # No local files yet – can't gate

        max_mtime = 0.0
        last_scanned_iso = None
        for row in rows:
            fp = str(row[0] or "").strip()
            ls = row[1]
            if fp:
                folder = os.path.dirname(fp)
                if folder and os.path.isdir(folder):
                    try:
                        mtime = os.path.getmtime(folder)
                        if mtime > max_mtime:
                            max_mtime = mtime
                    except OSError:
                        pass
            if ls and (last_scanned_iso is None or str(ls) > last_scanned_iso):
                last_scanned_iso = str(ls)

        if not last_scanned_iso:
            return False

        try:
            from datetime import datetime, timezone
            # Parse ISO timestamp (handles both 'Z' and '+00:00' offsets)
            ls_clean = last_scanned_iso.replace("Z", "+00:00")
            ls_dt = datetime.fromisoformat(ls_clean)
            if ls_dt.tzinfo is None:
                ls_dt = ls_dt.replace(tzinfo=timezone.utc)
            ls_ts = ls_dt.timestamp()
        except Exception:
            return False

        if max_mtime <= ls_ts:
            logging.debug(
                "[NAVIDROME_SCAN] Artist '%s' early-exit: max folder mtime (%.0f) <= last_scanned (%.0f)",
                artist_name, max_mtime, ls_ts,
            )
            return True
    except Exception as exc:
        logging.debug("[NAVIDROME_SCAN] mtime gate failed for '%s': %s", artist_name, exc)
    return False


def _artist_album_name_diff(artist_name: str, artist_id: str) -> tuple[bool, set[str]]:
    """Compare Navidrome album names to DB album names for the artist.

    Returns:
        (skip_artist, changed_album_names)
        - skip_artist: True if no changes detected (early-exit whole artist).
        - changed_album_names: set of album names that are new or missing.
          Empty when skip_artist is True.

    Note: The tracks table does not store Navidrome album IDs, so album
    names are used as the comparison key.
    """
    from start import fetch_artist_albums

    try:
        nav_albums = fetch_artist_albums(artist_id)
    except Exception as exc:
        logging.debug(
            "[NAVIDROME_SCAN] Could not fetch albums for '%s' (id=%s): %s",
            artist_name, artist_id, exc,
        )
        return False, set()  # Degrade gracefully – don't skip on error

    nav_names = {a.get("name") or "" for a in nav_albums if a.get("name")}
    nav_names.discard("")

    conn = None
    db_names: set[str] = set()
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT DISTINCT album
            FROM tracks
            WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s
              AND album IS NOT NULL AND TRIM(album) <> ''
            """,
            (artist_name,),
        )
        for row in cursor.fetchall():
            if row[0]:
                db_names.add(str(row[0]))
        conn.close()
    except Exception as exc:
        logging.debug(
            "[NAVIDROME_SCAN] Could not query DB albums for '%s': %s",
            artist_name, exc,
        )
        return False, set()
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    changed = nav_names.symmetric_difference(db_names)
    if not changed:
        logging.debug(
            "[NAVIDROME_SCAN] Artist '%s' early-exit: album names unchanged (%d albums)",
            artist_name, len(nav_names),
        )
        return True, set()

    logging.debug(
        "[NAVIDROME_SCAN] Artist '%s' album diff: %d changed album(s) (%d nav vs %d db)",
        artist_name, len(changed), len(nav_names), len(db_names),
    )
    return False, changed


def scan_artist_to_db(artist_name: str, artist_id: str, verbose: bool = False, force: bool = False, filter_missing: bool = False, processed_artists: int = 0, total_artists: int = 0, album_filter: str = None, progress_file: str = None, progress_scan_type: str = None, diff_mode: bool = False):
    """
    Scan a single artist from Navidrome and persist tracks to DB.

    Args:
        artist_name: Name of the artist to scan
        artist_id: Navidrome ID of the artist
        verbose: Enable verbose logging
        force: Force re-import even if cached
        filter_missing: Only scan artists/albums with missing fields
        processed_artists: Current artist index (1-based) for progress tracking
        total_artists: Total number of artists for progress tracking
        album_filter: Only scan this specific album (if provided)
        progress_file: Override the progress file written by save_navidrome_scan_progress.
                       Pass the caller's own progress file (e.g. combined_scan_progress.json)
                       so that navidrome_scan_progress.json is not written during a combined scan.
        progress_scan_type: Override the scan_type written into the progress file.
        diff_mode: When True, enable aggressive early-exit gates (mtime + album diff)
                   and only process albums that appear changed.  Used by the library
                   sync worker to avoid redundant work.

    Args:
        artist_name: Name of the artist to scan
        artist_id: Navidrome ID of the artist
        verbose: Enable verbose logging
        force: Force re-import even if cached
        filter_missing: Only scan artists/albums with missing fields
        processed_artists: Current artist index (1-based) for progress tracking
        total_artists: Total number of artists for progress tracking
        album_filter: Only scan this specific album (if provided)
        progress_file: Override the progress file written by save_navidrome_scan_progress.
                       Pass the caller's own progress file (e.g. combined_scan_progress.json)
                       so that navidrome_scan_progress.json is not written during a combined scan.
        progress_scan_type: Override the scan_type written into the progress file.
    """
    # Local import to avoid circular dependency
    from start import fetch_artist_albums, fetch_album_tracks, save_to_db
    
    if not artist_id:
        logging.warning(f"[NAVIDROME_SCAN] scan_artist_to_db called with no artist_id for '{artist_name}' — skipping")
        return

    log_debug(f"[NAVIDROME_SCAN] scan_artist_to_db: artist='{artist_name}' id={artist_id} (force={force}, filter_missing={filter_missing}, {processed_artists}/{total_artists})")
    try:
        canonical_artist_name = _clean_artist_name_for_storage(artist_name) or artist_name

        # Prefetch cached track IDs for this artist and check for missing critical fields
        existing_track_ids: set[str] = set()
        navidrome_track_ids: set[str] = set()  # All track IDs returned by Navidrome during this scan
        existing_album_tracks: dict[str, set[str]] = {}
        existing_album_artists: dict[str, str] = {}  # album_name -> existing album_artist
        albums_needing_reimport: set[str] = set()  # Track albums with missing fields
        albums_logged: set[str] = set()  # Track which albums we've already logged
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            placeholder = "%s"

            # Critical fields that should be imported from Navidrome
            critical_fields = ['duration', 'track_number', 'year', 'file_path']

            # Get existing tracks and check for missing fields
            cursor.execute(f"SELECT album, id, album_artist, {', '.join(critical_fields)} FROM tracks WHERE artist = {placeholder}", (canonical_artist_name,))
            for row in cursor.fetchall():
                alb_name = row['album']
                tid = row['id']
                existing_track_ids.add(tid)
                existing_album_tracks.setdefault(alb_name, set()).add(tid)

                # Capture existing album_artist once per album (used to preserve Various Artists)
                if alb_name not in existing_album_artists:
                    aa = row['album_artist']
                    if aa:
                        existing_album_artists[alb_name] = str(aa)

                # Check if any critical field is missing (NULL or empty)
                field_values = [row[f] for f in critical_fields]
                if any(val is None or val == '' or val == 0 for val in field_values):
                    albums_needing_reimport.add(alb_name)
                    # Only log once per album to avoid duplicate messages
                    if verbose and alb_name not in albums_logged:
                        logging.info(f"Album '{alb_name}' flagged for re-import due to missing fields")
                        albums_logged.add(alb_name)
            # Normalize previously imported variants for this artist so future
            # list/detail queries use the same canonical value.
            try:
                updated_rows = _normalize_existing_artist_rows(
                    conn,
                    canonical_artist_name,
                    aliases=[artist_name],
                )
                if updated_rows:
                    logging.info(f"[ARTIST_NORMALIZE] {canonical_artist_name}: normalized {updated_rows} existing rows")
            except Exception as normalize_err:
                logging.debug(f"[ARTIST_NORMALIZE] Existing-row normalization skipped for {canonical_artist_name}: {normalize_err}")

            try:
                path_updates, duplicate_deletes = _sanitize_artist_file_paths_and_duplicates(conn, canonical_artist_name)
                if path_updates or duplicate_deletes:
                    logging.info(
                        f"[NAVIDROME_SANITIZE] {canonical_artist_name}: normalized_paths={path_updates}, deduped_rows={duplicate_deletes}"
                    )
            except Exception as sanitize_err:
                logging.debug(f"[NAVIDROME_SANITIZE] Path sanitization skipped for {canonical_artist_name}: {sanitize_err}")

            conn.close()
        except Exception as e:
            logging.debug(f"Prefetch existing tracks for artist '{artist_name}' failed: {e}")
            log_debug(f"[NAVIDROME_SCAN] Prefetch failed for '{artist_name}': {e}", exc_info=True)

        # Normalize stale file_path values and remove duplicate rows that point to
        # the same normalized physical file before importing fresh track metadata.
        try:
            conn = get_db_connection()
            sanitize_summary = _sanitize_artist_file_paths_and_duplicates(conn, canonical_artist_name)
            if sanitize_summary.get("path_updates") or sanitize_summary.get("duplicates_removed"):
                logging.info(
                    f"[NAVIDROME_SANITIZE] {canonical_artist_name}: "
                    f"normalized_paths={sanitize_summary.get('path_updates', 0)}, "
                    f"duplicates_removed={sanitize_summary.get('duplicates_removed', 0)}"
                )
            conn.close()
        except Exception as sanitize_err:
            logging.debug(f"[NAVIDROME_SANITIZE] Skipped for {canonical_artist_name}: {sanitize_err}")

        # ------------------------------------------------------------------
        # Diff-mode early-exit gates (library sync worker only)
        # ------------------------------------------------------------------
        changed_album_names: set[str] | None = None
        if diff_mode and not force and not album_filter and not filter_missing:
            # i) Folder mtime gate (hint only)
            if _artist_folder_mtime_gate(canonical_artist_name):
                log_debug(
                    f"[NAVIDROME_SCAN] Artist '{artist_name}' skipped by mtime gate"
                )
                return {"skipped_mtime": True}

            # ii) Album name diff (album ID proxy – DB does not store album IDs)
            skip_artist, changed = _artist_album_name_diff(
                canonical_artist_name, artist_id
            )
            if skip_artist:
                log_debug(
                    f"[NAVIDROME_SCAN] Artist '{artist_name}' skipped by album name diff"
                )
                return {"skipped_album_diff": True}
            changed_album_names = changed

        albums = fetch_artist_albums(artist_id)
        log_debug(f"[NAVIDROME_SCAN] fetch_artist_albums('{artist_name}') returned {len(albums)} albums")

        # If filter_missing is enabled and this artist has no missing fields, skip it
        if filter_missing and len(albums_needing_reimport) == 0 and len(existing_track_ids) > 0:
            logging.debug(f"Skipping artist '{artist_name}' - no albums with missing fields (filter_missing=True)")
            return
        
        if verbose:
            print(f"🎤 Scanning artist: {artist_name} ({len(albums)} albums)")
        logging.info(f"🎤 [Navidrome] Scanning artist: {artist_name} ({len(albums)} albums, force={force}, filter_missing={filter_missing}, album_filter={album_filter or 'None'})")
        # Save artist-level progress
        if total_artists > 0:
            save_navidrome_scan_progress(
                artist_name, processed_artists, total_artists,
                progress_file=progress_file,
                scan_type=progress_scan_type or "navidrome_scan",
            )

        total_albums = len(albums)
        for alb_idx, alb in enumerate(albums, 1):
            album_name = alb.get("name") or ""
            
            # Skip albums that don't match filter_missing (if enabled)
            if filter_missing and album_name not in albums_needing_reimport:
                logging.debug(f"Skipping album '{album_name}' - no missing fields (filter_missing=True)")
                continue
            
            # Skip albums that don't match the filter (if provided)
            if album_filter and album_name.strip() != album_filter.strip():
                logging.debug(f"Skipping album '{album_name}' - does not match filter '{album_filter}'")
                continue

            # In diff_mode, only process albums detected as changed.
            # Albums that exist in both Navidrome and DB with unchanged names are
            # skipped entirely (no track fetch) to save API calls.
            if diff_mode and changed_album_names is not None:
                if album_name not in changed_album_names:
                    logging.debug(
                        f"Skipping album '{album_name}' - not in changed set (diff_mode)"
                    )
                    continue

            album_id = alb.get("id")
            if not album_id:
                continue
            logging.info(f"   💿 [Album {alb_idx}/{total_albums}] {album_name}")
            log_unified(f"Navidrome Import - {artist_name} - Album {alb_idx}/{total_albums}: {album_name}")
            
            # Detect if this is a live/unplugged album
            from .helpers import detect_live_album
            album_context = detect_live_album(album_name)
            if album_context.get("is_live") or album_context.get("is_unplugged"):
                logging.info(f"      🎤 Detected live/unplugged album: {album_name}")
            try:
                album_data = fetch_album_tracks(album_id)
                tracks = album_data.get("tracks", [])
                api_album_artist = album_data.get("artist", "")
            except Exception as e:
                logging.debug(f"Failed to fetch tracks for album '{album_name}': {e}")
                tracks = []
                api_album_artist = ""

            # Collect ALL Navidrome track IDs (even for cached albums) to detect stale DB tracks
            for t in tracks:
                if t.get("id"):
                    navidrome_track_ids.add(t.get("id"))

            cached_ids_for_album = existing_album_tracks.get(album_name, set())

            # Skip album only if it's already cached AND doesn't need re-import due to missing fields
            album_needs_reimport = album_name in albums_needing_reimport
            if not force and not album_needs_reimport and tracks and len(cached_ids_for_album) >= len(tracks):
                if verbose:
                    print(f"   Skipping cached album: {album_name}")
                # Still log skipped albums to scan history
                log_album_scan(artist_name, album_name, 'navidrome', len(cached_ids_for_album), 'skipped')
                continue

            # Skip if Navidrome track IDs exactly match the DB (no additions/removals).
            # When force=True this check is bypassed so the user can explicitly re-import.
            if not force and not album_needs_reimport and tracks and cached_ids_for_album:
                navidrome_album_ids = {t.get("id") for t in tracks if t.get("id")}
                if navidrome_album_ids and navidrome_album_ids == cached_ids_for_album:
                    logging.debug(f"Skipping unchanged album '{album_name}' ({len(cached_ids_for_album)} tracks, IDs match)")
                    log_unified(f"Navidrome Import - {artist_name} - Skipped album: {album_name} (no changes)")
                    log_album_scan(artist_name, album_name, 'navidrome', len(cached_ids_for_album), 'skipped')
                    continue

            if album_needs_reimport and verbose:
                print(f"   Re-importing album with missing fields: {album_name}")
            # Track the number of tracks actually processed for this album
            album_tracks_processed = 0
            album_mbids_seen = set()
            
            # Get the album artist with priority order:
            # 1. api_album_artist - from getAlbum.view response (most reliable)
            # 2. alb.get("artist") - from getArtist.view response 
            # 3. artist_name - the function parameter (artist we're importing)
            # Note: track.albumArtist field can be incorrect (e.g., containing track artist with feat.)
            album_artist_value = _clean_artist_name_for_storage(api_album_artist or alb.get("artist") or canonical_artist_name) or canonical_artist_name

            # Preserve an existing 'Various Artists' (or similar) album_artist from the DB.
            # When scanning a single artist, Navidrome may return their name as the artist for
            # VA compilation/soundtrack albums, incorrectly overwriting the correct value.
            _va_variants = frozenset({'various artists', 'various', 'v/a', 'va', 'compilation', 'original soundtrack'})
            _existing_aa = existing_album_artists.get(album_name, '')
            if _existing_aa and _existing_aa.lower().strip() in _va_variants and album_artist_value.lower().strip() not in _va_variants:
                logging.debug(
                    f"[ARTIST_NORMALIZE] Preserved album_artist='{_existing_aa}' for '{album_name}' "
                    f"(Navidrome returned '{album_artist_value}')"
                )
                album_artist_value = _existing_aa

            for t in tracks:
                track_id = t.get("id")
                if not track_id:
                    continue

                # Use NavidromeClient's extract_track_metadata to avoid duplication
                navi_client = NavidromeClient(base_url="", username="", password="")  # URLs/auth not needed for extraction
                extracted = navi_client.extract_track_metadata(t)

                writer_json = extracted.get("writer", "[]")
                if writer_json in (None, "", "[]"):
                    writer_json = _extract_writer_from_file_tags(extracted.get("file_path", "") or t.get("path", ""))

                # Backfill missing metadata fields from the local audio file.
                # Navidrome's Subsonic API often omits extended tags such as
                # ReplayGain, R128, MusicBrainz IDs, record label, copyright, etc.
                _file_path_for_backfill = extracted.get("file_path", "") or t.get("path", "")
                _backfill_from_file_tags(_file_path_for_backfill, extracted)

                # Extract track-level artist for featured artist detection
                # Fallback to album artist if track artist not available
                track_artist = _clean_artist_name_for_storage(t.get("artist", "") or canonical_artist_name) or canonical_artist_name
                
                td = {
                    "_navidrome_sync": True,
                    "id": track_id,
                    "title": t.get("title", ""),
                    "album": album_name,
                    "artist": track_artist,
                    "score": 0.0,
                    "spotify_score": 0,
                    "lastfm_score": 0,
                    "listenbrainz_score": 0,
                    "age_score": 0,
                    "genres": extracted.get("navidrome_genres", "") or "",  # Initialize with Navidrome genre
                    "navidrome_genres": extracted.get("navidrome_genres", "") or "",  # Store as backslash-separated string
                    "navidrome_genre": extracted.get("navidrome_genre", "") or "",  # First genre only
                    "spotify_genres": json.dumps([]),  # Serialize as JSON string
                    "lastfm_tags": json.dumps([]),  # Serialize as JSON string
                    "discogs_genres": json.dumps([]),  # Serialize as JSON string
                    "audiodb_genres": json.dumps([]),  # Serialize as JSON string
                    "musicbrainz_genres": json.dumps([]),  # Serialize as JSON string
                    "spotify_album": "",
                    "spotify_artist": "",
                    "spotify_popularity": 0,
                    "spotify_release_date": extracted.get("year", "") or "",
                    "spotify_album_art_url": "",
                    "lastfm_track_playcount": 0,
                    # Extract file_path from extracted metadata (populated by extract_track_metadata)
                    "file_path": extracted.get("file_path", ""),
                    "last_scanned": _now_local_iso(),
                    "spotify_album_type": extracted.get("releasetype", "") or extracted.get("musicbrainz_albumtype", "") or "",
                    "spotify_total_tracks": 0,
                    "spotify_id": None,
                    "is_spotify_single": 0,
                    "is_single": False,
                    "single_confidence": "low",
                    "single_sources": json.dumps([]),  # Serialize as JSON string
                    # ── MusicBrainz IDs ───────────────────────────────────────
                    "mbid": extracted.get("mbid", "") or "",
                    # musicbrainz_albumid is the canonical release UUID column.
                    # musicbrainz_album_mbid is a legacy alias — always derive it from
                    # musicbrainz_albumid here so both DB columns are identical.
                    # (_backfill_from_file_tags above has already synced them inside
                    # `extracted`, so both reads land on the same value.)
                    "musicbrainz_albumid": extracted.get("musicbrainz_albumid", "") or "",
                    "musicbrainz_album_mbid": extracted.get("musicbrainz_albumid", "") or "",
                    "musicbrainz_trackid": extracted.get("musicbrainz_trackid", "") or "",
                    "musicbrainz_releasegroupid": extracted.get("musicbrainz_releasegroupid", "") or "",
                    "musicbrainz_releasetrackid": extracted.get("musicbrainz_releasetrackid", "") or "",
                    "musicbrainz_albumstatus": extracted.get("musicbrainz_albumstatus", "") or "",
                    "musicbrainz_albumtype": extracted.get("musicbrainz_albumtype", "") or "",
                    "musicbrainz_releasecountry": extracted.get("musicbrainz_releasecountry", "") or "",
                    "musicbrainz_artistid": extracted.get("musicbrainz_artistid", "") or "",
                    "musicbrainz_artist_id": extracted.get("musicbrainz_artist_id", "") or extracted.get("musicbrainz_artistid", "") or "",
                    "musicbrainz_albumartistid": extracted.get("musicbrainz_albumartistid", "") or "",
                    "musicbrainz_workid": extracted.get("musicbrainz_workid", "") or "",
                    # ── Album-level consistency / Navidrome split-cause fields ─
                    "releasetype": extracted.get("releasetype", "") or "",
                    "releasestatus": extracted.get("releasestatus", "") or "",
                    "releasecountry": extracted.get("releasecountry", "") or "",
                    "media": extracted.get("media", "") or "",
                    "label": extracted.get("label", "") or "",
                    "recordlabel": extracted.get("recordlabel", "") or "",
                    "tracktotal": extracted.get("tracktotal", "") or "",
                    "disctotal": extracted.get("disctotal", "") or "",
                    "compilation": extracted.get("compilation", "") or "",
                    "grouping": extracted.get("grouping", "") or "",
                    "albumversion": extracted.get("albumversion", "") or "",
                    "discsubtitle": extracted.get("discsubtitle", "") or "",
                    "script": extracted.get("script", "") or "",
                    # ── ReplayGain / R128 ─────────────────────────────────────
                    "replaygain_track_gain": extracted.get("replaygain_track_gain", "") or "",
                    "replaygain_track_peak": extracted.get("replaygain_track_peak", "") or "",
                    "replaygain_album_gain": extracted.get("replaygain_album_gain", "") or "",
                    "replaygain_album_peak": extracted.get("replaygain_album_peak", "") or "",
                    "r128_track_gain": extracted.get("r128_track_gain", "") or "",
                    "r128_album_gain": extracted.get("r128_album_gain", "") or "",
                    # ── Release / catalogue metadata ──────────────────────────
                    "releasedate": extracted.get("releasedate", "") or "",
                    "originalyear": extracted.get("originalyear", "") or "",
                    "originaldate": extracted.get("originaldate", "") or "",
                    "copyright": extracted.get("copyright", "") or "",
                    "barcode": extracted.get("barcode", "") or "",
                    "catalognumber": extracted.get("catalognumber", "") or "",
                    "asin": extracted.get("asin", "") or "",
                    # ── Content / structural ──────────────────────────────────
                    "subtitle": extracted.get("subtitle", "") or "",
                    "lyrics": extracted.get("lyrics", "") or "",
                    "language": extracted.get("language", "") or "",
                    "work": extracted.get("work", "") or "",
                    "movement": extracted.get("movement", "") or "",
                    "movementname": extracted.get("movementname", "") or "",
                    "movementtotal": extracted.get("movementtotal", "") or "",
                    "key": extracted.get("key", "") or "",
                    "explicitstatus": extracted.get("explicitstatus", "") or "",
                    # ── Credits ───────────────────────────────────────────────
                    "composer": extracted.get("composer", "") or "",
                    "lyricist": extracted.get("lyricist", "") or "",
                    "conductor": extracted.get("conductor", "") or "",
                    "remixer": extracted.get("remixer", "") or "",
                    "producer": extracted.get("producer", "") or "",
                    "arranger": extracted.get("arranger", "") or "",
                    "mixer": extracted.get("mixer", "") or "",
                    "engineer": extracted.get("engineer", "") or "",
                    "director": extracted.get("director", "") or "",
                    "djmixer": extracted.get("djmixer", "") or "",
                    "performer": extracted.get("performer", "") or "",
                    # ── Sort tags ─────────────────────────────────────────────
                    "titlesort": extracted.get("titlesort", "") or "",
                    "albumsort": extracted.get("albumsort", "") or "",
                    "artistsort": extracted.get("artistsort", "") or "",
                    "albumartistsort": extracted.get("albumartistsort", "") or "",
                    "albumartistssort": extracted.get("albumartistssort", "") or "",
                    "artistssort": extracted.get("artistssort", "") or "",
                    "composersort": extracted.get("composersort", "") or "",
                    "lyricistsort": extracted.get("lyricistsort", "") or "",
                    # ── Multi-value artist arrays ─────────────────────────────
                    "artists": extracted.get("artists", "") or "",
                    "albumartists": extracted.get("albumartists", "") or "",
                    # ── Encoding / technical ──────────────────────────────────
                    "encodedby": extracted.get("encodedby", "") or "",
                    "encodersettings": extracted.get("encodersettings", "") or "",
                    "website": extracted.get("website", "") or "",
                    "license": extracted.get("license", "") or "",
                    # ── Acoustic / playback ───────────────────────────────────
                    "isrc": extracted.get("isrc", "") or "",
                    "bpm": extracted.get("bpm"),
                    "danceability": extracted.get("danceability"),
                    "comment": extracted.get("comment", "") or "",
                    "suggested_mbid": "",
                    "suggested_mbid_confidence": 0.0,
                    "stars": extracted.get("stars", 0),
                    "duration": extracted.get("duration"),
                    "track_number": extracted.get("track_number"),
                    "disc_number": extracted.get("disc_number"),
                    "year": extracted.get("year"),
                    "writer": writer_json,  # JSON array of lyricists/writers from Navidrome or file tags
                    "album_artist": album_artist_value,
                    "bitrate": extracted.get("bitrate"),
                    "sample_rate": extracted.get("sample_rate"),
                    # Store album context for single detection
                    "album_context_live": 1 if album_context.get("is_live") else 0,
                    "album_context_unplugged": 1 if album_context.get("is_unplugged") else 0,
                }

                album_mbid_val = str(td.get("musicbrainz_album_mbid") or "").strip()
                if album_mbid_val:
                    album_mbids_seen.add(album_mbid_val)
                save_to_db(td)

                # Keep embedded tag consistent with normalized DB album_artist.
                _normalize_album_artist_file_tag(td.get("file_path", ""), album_artist_value)
                album_tracks_processed += 1

            # Log this album completion to scan_history
            if album_tracks_processed > 0:
                logging.info(f"Logging to scan_history: {artist_name} - {album_name} ({album_tracks_processed} tracks)")
                log_album_scan(artist_name, album_name, 'navidrome', album_tracks_processed, 'completed')
                logging.info(f"Completed navidrome scan for {artist_name} - {album_name} ({album_tracks_processed} tracks)")
                log_unified(f"Navidrome Import - {artist_name} - Completed album: {album_name} ({album_tracks_processed} tracks)")
            elif album_needs_reimport or (not force and len(cached_ids_for_album) > 0):
                # Album was skipped
                log_unified(f"Navidrome Import - {artist_name} - Skipped album: {album_name} (already cached)")

            if len(album_mbids_seen) > 1:
                mbid_preview = ", ".join(sorted(album_mbids_seen)[:4])
                logging.warning(
                    f"[NAVIDROME_SCAN] Album MBID inconsistency detected for '{artist_name} - {album_name}': "
                    f"{len(album_mbids_seen)} distinct MBIDs ({mbid_preview})"
                )
        if verbose:
            print(f"Artist scan complete: {artist_name}")
            logging.info(f"Artist scan complete: {artist_name}")

        # Remove stale tracks from the database that no longer exist in Navidrome
        # (e.g. files deleted from disk that show as grey in Navidrome).
        # Only perform this cleanup during a full artist scan, not when using
        # album_filter or filter_missing which intentionally skip some albums.
        # diff_mode also skips this because we don't fetch tracks for unchanged albums.
        can_cleanup = not filter_missing and not album_filter and not diff_mode
        if can_cleanup and existing_track_ids:
            stale_ids = existing_track_ids - navidrome_track_ids
            if stale_ids:
                try:
                    conn = get_db_connection()
                    try:
                        cursor = conn.cursor()
                        placeholder = "%s"
                        placeholders = ", ".join([placeholder] * len(stale_ids))
                        cursor.execute(f"DELETE FROM tracks WHERE id IN ({placeholders})", list(stale_ids))
                        conn.commit()
                    finally:
                        conn.close()
                    logging.info(f"Removed {len(stale_ids)} stale track(s) for artist '{artist_name}' that no longer exist in Navidrome")
                    log_unified(f"Navidrome Import - {artist_name} - Removed {len(stale_ids)} stale track(s) no longer in library")
                except Exception as e:
                    logging.error(f"Failed to remove stale tracks for artist '{artist_name}': {e}")

        # Remove empty subdirectories under the artist's music folder.
        # Files deleted from disk leave behind empty album directories; clean
        # those up so the filesystem stays tidy.
        if can_cleanup:
            try:
                music_root = os.environ.get("MUSIC_FOLDER") or os.environ.get("MUSIC_ROOT", "/music")
                artist_dir = os.path.join(music_root, canonical_artist_name)
                if os.path.isdir(artist_dir):
                    for dirpath, dirnames, filenames in os.walk(artist_dir, topdown=False):
                        # Only remove immediate subdirectories (album-level), not the
                        # artist root itself, and only if truly empty (no files).
                        if dirpath == artist_dir:
                            continue
                        if not os.listdir(dirpath):
                            try:
                                os.rmdir(dirpath)
                                logging.info(f"Removed empty directory: {dirpath}")
                                log_unified(f"Navidrome Import - {artist_name} - Removed empty directory: {os.path.basename(dirpath)}")
                            except OSError as rmdir_err:
                                logging.debug(f"Could not remove directory '{dirpath}': {rmdir_err}")
            except Exception as e:
                logging.debug(f"Empty-folder cleanup skipped for artist '{artist_name}': {e}")

        # In diff_mode, return metadata so the caller can observe what happened.
        if diff_mode and changed_album_names is not None:
            return {
                "changed": True,
                "changed_albums": len(changed_album_names),
            }
    except Exception as e:
        logging.error(f"scan_artist_to_db failed for {artist_name}: {e}")
        log_debug(f"[NAVIDROME_SCAN] scan_artist_to_db raised exception for '{artist_name}': {e}", exc_info=True)
        raise


def pre_import_sync_album_artists(artist_id: str = None) -> dict:
    """
    Pre-import sync: Batch fetch album list metadata from Navidrome and ensure
    discovered album artists exist in database.
    
    This is called before the main Navidrome import to quickly identify and insert any new
    album artists in a single pass, avoiding one-by-one checks during track import.

    Important: This stage only reads album metadata (artist/album-level fields).
    Track-level payloads are still fetched in the normal artist scan step.
    
    Args:
        artist_id: Single artist ID to sync (optional). If None, syncs all artists.
        
    Returns:
        Dict with results: {
            'unique_album_artists': int,
            'new_artists_created': int,
            'existing_artists': int,
            'sync_time_ms': float,
            'new_artists': [list of artist names that were created],
            'success': bool
        }
    """
    import time
    from popularity_helpers import _get_nav_client
    
    start_time = time.time()
    
    try:
        nav_client = _get_nav_client()
        if not nav_client:
            return {'error': 'Navidrome client not available', 'success': False}
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Get current artists in database using normalized keys to avoid
        # case/punctuation duplicates.
        cursor.execute("SELECT DISTINCT name FROM artists WHERE name IS NOT NULL AND name != ''")
        existing_artists = set()
        for row in cursor.fetchall():
            existing_name = row[0] if isinstance(row, tuple) else row.get('name', '')
            key = _normalize_artist_key(existing_name)
            if key:
                existing_artists.add(key)
        logging.debug(f"Found {len(existing_artists)} existing artists in database")
        
        # Fetch album list first (single API family) and derive album artists.
        # This avoids per-artist album requests during pre-sync.
        if artist_id:
            albums_to_sync = nav_client.get_albums(artist_id=artist_id)
            logging.info(f"Pre-import sync: Scanning {len(albums_to_sync)} album(s) for artist_id={artist_id}")
        else:
            albums_to_sync = nav_client.get_albums()
            logging.info(f"Pre-import sync: Scanning {len(albums_to_sync)} album(s) from Navidrome")

        # Extract unique album artists from album list
        unique_album_artists = {}  # normalized_key -> {'original': str, 'count': int}

        for album in albums_to_sync:
            album_artist = (album.get('artist') or '').strip()
            if not album_artist:
                continue

            cleaned_album_artist = _clean_artist_name_for_storage(album_artist) or album_artist
            key = _normalize_artist_key(cleaned_album_artist)
            if not key:
                continue
            if key not in unique_album_artists:
                unique_album_artists[key] = {'original': cleaned_album_artist, 'count': 0}
            unique_album_artists[key]['count'] += 1
        
        logging.info(f"Pre-import sync: Found {len(unique_album_artists)} unique album artists across all albums")
        
        # Identify new artists
        new_artists_to_add = []
        for artist_key, artist_info in unique_album_artists.items():
            if artist_key not in existing_artists:
                cleaned_name = _clean_artist_name_for_storage(artist_info['original']) or artist_info['original']
                if cleaned_name not in new_artists_to_add:
                    new_artists_to_add.append(cleaned_name)
        
        logging.info(f"Pre-import sync: {len(new_artists_to_add)} new album artists need to be added to database")
        
        # Batch insert new artists in a single transaction
        if new_artists_to_add:
            try:
                placeholder = "%s"
                
                for artist_name in new_artists_to_add:
                    cursor.execute(f"""
                        INSERT INTO artists (id, name)
                        VALUES ({placeholder}, {placeholder})
                        ON CONFLICT DO NOTHING
                    """, (artist_name.lower().replace(' ', '_'), artist_name))
                    logging.debug(f"Created artist record: {artist_name}")
                
                conn.commit()
                logging.info(f"Pre-import sync: Created {len(new_artists_to_add)} new artist record(s)")
            except Exception as e:
                logging.debug(f"Error batch inserting artists: {e}")
                conn.rollback()
                conn.close()
                return {
                    'error': f'Failed to batch insert artists: {e}',
                    'unique_album_artists': len(unique_album_artists),
                    'new_artists_created': 0,
                    'success': False
                }
        
        sync_time_ms = (time.time() - start_time) * 1000
        
        result = {
            'unique_album_artists': len(unique_album_artists),
            'new_artists_created': len(new_artists_to_add),
            'existing_artists': len(unique_album_artists) - len(new_artists_to_add),
            'sync_time_ms': round(sync_time_ms, 2),
            'new_artists': new_artists_to_add,
            'success': True
        }
        
        logging.info(f"Pre-import sync complete: {result['unique_album_artists']} unique album artists, {result['new_artists_created']} new, {result['existing_artists']} existing")
        
        conn.close()
        return result
        
    except Exception as e:
        logging.debug(f"Pre-import artist sync failed: {e}", exc_info=True)
        return {
            'error': str(e),
            'success': False
        }


def fetch_artist_metadata(artist_name: str, verbose: bool = False):
    """
    Fetch and store artist biography, images, and country from external APIs.
    
    This is called after a successful artist scan to enhance artist metadata.
    Only fetches if data doesn't exist or if force=true in config.
    
    Image sources priority (in order):
    1. The AudioDB (fanart) - 30 requests/min, good quality
    2. Apple Music - unlimited, reliable
    3. MusicBrainz CAA - unlimited, good coverage
    
    Country source:
    - MusicBrainz (area/region of artist origin)
    
    Args:
        artist_name: Name of the artist
        verbose: Enable verbose logging
    """
    from api_clients.discogs import get_discogs_artist_biography
    from api_clients.applemusic import get_artist_artwork
    from api_clients.audiodb import get_artist_fanart
    from api_clients.musicbrainz import get_artist_country, _USER_AGENT as MUSICBRAINZ_USER_AGENT
    from helpers import create_retry_session
    from config_loader import load_config
    
    try:
        config = load_config()
        logging.debug(f"Fetching artist metadata for: {artist_name}")
        
        # Check if force flag is enabled
        force = config.get("features", {}).get("force", False)
        logging.debug(f"Force flag: {force}")
        
        # Check if artist metadata already exists
        conn = get_db_connection()
        placeholder = "%s"
        cursor = conn.cursor()
        
        # Create artist_metadata table if it doesn't exist
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS artist_metadata (
                artist_name TEXT PRIMARY KEY,
                biography TEXT,
                image_url TEXT,
                updated_at TEXT
            )
        """)
        logging.debug(f"DB: Ensured artist_metadata table exists")
        
        # Check for existing metadata
        cursor.execute(f"""
            SELECT biography, image_url 
            FROM artist_metadata 
            WHERE artist_name = {placeholder}
        """, (artist_name,))
        logging.debug(f"DB Query: SELECT biography, image_url FROM artist_metadata WHERE artist_name = '{artist_name}'")
        existing_row = cursor.fetchone()
        
        # Determine what needs to be fetched
        fetch_bio = force
        fetch_image = force
        fetch_country = force
        
        existing_bio = ""
        existing_image = ""
        
        if existing_row and not force:
            existing_bio = existing_row['biography'] or ""
            existing_image = existing_row['image_url'] or ""
            
            # Only fetch if missing
            fetch_bio = not existing_bio
            fetch_image = not existing_image
        
        # Check if country needs to be fetched (from artists table, not artist_metadata)
        cursor.execute(f"""
            SELECT country FROM artists WHERE name = {placeholder}
        """, (artist_name,))
        country_row = cursor.fetchone()
        existing_country = (country_row['country'] if country_row else None) or ""
        
        if not force:
            fetch_country = not existing_country
            
            if not fetch_bio and not fetch_image and not fetch_country:
                logging.info(f"Artist metadata already exists for {artist_name}, skipping fetch")
                logging.debug(f"Metadata exists - Bio: {bool(existing_bio)}, Image: {bool(existing_image)}, Country: {bool(existing_country)}")
                conn.close()
                return
        
        conn.close()
        
        # Get API configurations
        discogs_config = config.get("api_integrations", {}).get("discogs", {})
        discogs_enabled = discogs_config.get("enabled", False)
        discogs_token = discogs_config.get("token", "")
        logging.debug(f"Discogs config - Enabled: {discogs_enabled}, Token present: {bool(discogs_token)}")
        
        audiodb_config = config.get("api_integrations", {}).get("audiodb", {})
        audiodb_enabled = audiodb_config.get("enabled", False)
        audiodb_api_key = audiodb_config.get("api_key", "195003")
        logging.debug(f"AudioDB config - Enabled: {audiodb_enabled}, API key present: {bool(audiodb_api_key)}")
        
        # Try to fetch biography from Discogs (only if needed)
        biography = ""
        if fetch_bio and discogs_enabled and discogs_token:
            logging.info(f"Fetching biography for {artist_name} from Discogs...")
            logging.debug(f"API Call: get_discogs_artist_biography(artist_name={artist_name})")
            bio_data = get_discogs_artist_biography(artist_name, token=discogs_token, enabled=True)
            logging.debug(f"API Response: {bio_data}")
            biography = bio_data.get("profile", "")
            if biography:
                logging.info(f"Retrieved artist biography from Discogs ({len(biography)} characters)")
                logging.debug(f"Biography preview: {biography[:100]}...")
        
        # Try to fetch artist image with fallback chain (only if needed)
        artist_image_url = ""
        if fetch_image:
            # Priority 1: Try The AudioDB
            if audiodb_enabled and audiodb_api_key:
                logging.info(f"Fetching artist image for {artist_name} from The AudioDB...")
                logging.debug(f"API Call: get_artist_fanart(artist_name={artist_name})")
                artist_image_url = get_artist_fanart(artist_name, api_key=audiodb_api_key, enabled=True)
                if artist_image_url:
                    logging.info(f"Retrieved artist image from The AudioDB")
                    logging.debug(f"Image URL: {artist_image_url}")
            
            # Priority 2: Fall back to Apple Music if AudioDB didn't return anything
            if not artist_image_url:
                logging.info(f"Fetching artist image for {artist_name} from Apple Music (AudioDB fallback)...")
                logging.debug(f"API Call: get_artist_artwork(artist_name={artist_name}, size=500)")
                artist_image_url = get_artist_artwork(artist_name, size=500, enabled=True)
                if artist_image_url:
                    logging.info(f"Retrieved artist image from Apple Music")
                    logging.debug(f"Image URL: {artist_image_url}")
            
            # Priority 3: Fall back to MusicBrainz if still nothing found
            if not artist_image_url:
                try:
                    logging.debug(f"Attempting to fetch artist image from MusicBrainz CAA...")
                    # Simple MusicBrainz artist lookup to get MBID
                    mb_search_url = "https://musicbrainz.org/ws/2/artist"
                    mb_params = {"query": f'"{artist_name}"', "fmt": "json", "limit": 1}
                    mb_headers = {"User-Agent": MUSICBRAINZ_USER_AGENT}
                    
                    session = create_retry_session(user_agent=MUSICBRAINZ_USER_AGENT, retries=3, backoff=1.0)
                    mb_resp = session.get(mb_search_url, params=mb_params, headers=mb_headers, timeout=5)
                    mb_resp.raise_for_status()
                    
                    mb_data = mb_resp.json()
                    artists = mb_data.get("artists", [])
                    
                    if artists:
                        mbid = artists[0].get("id")
                        if mbid:
                            # Construct CAA URL for artist
                            artist_image_url = f"https://coverartarchive.org/artist/{mbid}/front-500"
                            logging.info(f"Retrieved artist image from MusicBrainz CAA")
                            logging.debug(f"Image URL: {artist_image_url}")
                except Exception as e:
                    logging.debug(f"MusicBrainz fallback failed: {e}")
        
        # Fetch country from MusicBrainz (only if needed)
        artist_country = ""
        if fetch_country:
            logging.info(f"Fetching country for {artist_name} from MusicBrainz...")
            logging.debug(f"API Call: get_artist_country(artist_name={artist_name})")
            try:
                artist_country = get_artist_country(artist_name, enabled=True)
                if artist_country:
                    logging.info(f"Retrieved artist country from MusicBrainz: {artist_country}")
                    logging.debug(f"Country: {artist_country}")
            except Exception as e:
                logging.debug(f"Failed to fetch artist country: {e}")
        
        # Store in database
        if biography or artist_image_url:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            # Insert or update artist metadata
            placeholder = "%s"

            cursor.execute(f"""
                INSERT INTO artist_metadata (artist_name, biography, image_url, updated_at)
                VALUES ({placeholder}, {placeholder}, {placeholder}, CURRENT_TIMESTAMP)
                ON CONFLICT (artist_name) DO UPDATE SET
                    biography = EXCLUDED.biography,
                    image_url = EXCLUDED.image_url,
                    updated_at = CURRENT_TIMESTAMP
            """, (artist_name, biography, artist_image_url))
            logging.debug(f"DB: Upserted artist_metadata for {artist_name}")
            
            conn.commit()
            conn.close()
            
            logging.info(f"Stored artist metadata for {artist_name}")
            logging.debug(f"Metadata saved - Bio: {bool(biography)}, Image: {bool(artist_image_url)}")
        
        # Store country in artists table and update all tracks
        if artist_country:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            # Update or insert artist record
            placeholder = "%s"

            cursor.execute(f"""
                INSERT INTO artists (name)
                VALUES ({placeholder})
                ON CONFLICT (name) DO NOTHING
            """, (artist_name,))
            cursor.execute(f"""
                UPDATE artists SET country = {placeholder} WHERE name = {placeholder}
            """, (artist_country, artist_name))
            
            # Update all tracks with this artist
            cursor.execute("""
                UPDATE tracks SET artist_country = %s WHERE artist = %s
            """, (artist_country, artist_name))
            
            logging.debug(f"DB: Updated country for artist {artist_name} and all their tracks")
            
            conn.commit()
            conn.close()
            
            logging.info(f"Stored artist country for {artist_name}: {artist_country}")
    
    except Exception as e:
        logging.info(f"Error fetching artist metadata for {artist_name}: {e}")
        logging.debug(f"fetch_artist_metadata error for {artist_name}: {e}", exc_info=True)


def get_navidrome_library_stats(artist_map: dict) -> dict:
    """
    Calculate total albums and tracks available from Navidrome.
    
    Args:
        artist_map: Artist map from build_artist_index()
        
    Returns:
        Dict with 'total_albums' and 'total_tracks' counts from Navidrome
    """
    # Local import to avoid circular dependency
    from start import fetch_artist_albums, fetch_album_tracks
    
    try:
        total_albums = sum(info.get("album_count", 0) for info in artist_map.values())
        total_tracks = 0
        
        # Count total tracks by fetching each album
        for artist_name, artist_info in artist_map.items():
            artist_id = artist_info.get("id")
            if not artist_id:
                continue
            
            try:
                albums = fetch_artist_albums(artist_id)
                for album in albums:
                    album_id = album.get("id")
                    if not album_id:
                        continue
                    
                    try:
                        album_data = fetch_album_tracks(album_id)
                        tracks = album_data.get("tracks", [])
                        total_tracks += len(tracks)
                    except Exception as e:
                        logging.debug(f"Failed to fetch tracks for album {album.get('name')}: {e}")
                        continue
            except Exception as e:
                logging.debug(f"Failed to fetch albums for artist {artist_name}: {e}")
                continue
        
        logging.debug(f"Navidrome stats: {total_albums} albums, {total_tracks} songs")
        return {
            "total_albums": total_albums,
            "total_tracks": total_tracks
        }
    except Exception as e:
        logging.debug(f"Failed to get Navidrome library stats: {e}", exc_info=True)
        return {"total_albums": 0, "total_tracks": 0}


def get_database_library_stats() -> dict:
    """
    Get library statistics from the local database.
    
    Note: Uses COUNT(DISTINCT album) which should be fast enough for typical
    library sizes. If performance becomes an issue, consider adding an index
    on the album column.
    
    Returns:
        Dict with 'total_albums' and 'total_tracks' counts from the database
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        
        # Count distinct albums
        cursor.execute("SELECT COUNT(DISTINCT album) AS cnt FROM tracks WHERE album IS NOT NULL AND album != ''")
        row = cursor.fetchone()
        total_albums = (row['cnt'] if row else 0) or 0
        
        # Count total songs/tracks
        cursor.execute("SELECT COUNT(*) AS cnt FROM tracks")
        row = cursor.fetchone()
        total_tracks = (row['cnt'] if row else 0) or 0
        
        conn.close()
        
        logging.debug(f"Database stats: {total_albums} albums, {total_tracks} songs")
        return {
            "total_albums": total_albums,
            "total_tracks": total_tracks
        }
    except Exception as e:
        logging.debug(f"Failed to get database library stats: {e}", exc_info=True)
        return {"total_albums": 0, "total_tracks": 0}


def scan_library_to_db(verbose: bool = False, force: bool = False, pre_sync_artists: bool = True):
    """
    Scan the entire Navidrome library (artists -> albums -> tracks) and persist
    a lightweight representation of each track into the local DB.

    Behavior:
      - Uses NavidromeClient API helpers: build_artist_index(), fetch_artist_albums(), fetch_album_tracks()
      - For each track, writes a minimal `track_data` record via `save_to_db()`
    - Uses upsert semantics (so re-running is safe and refreshes `last_scanned`)
      - Supports auto-resume: If an interrupted scan is detected, resumes from last scanned artist
      - Optional pre-sync of all album artists: If pre_sync_artists=True, batch-creates missing album artists
        before the main import loop (significantly faster than creating per-item during main loop)
    
    Args:
        verbose (bool): Enable verbose output logging
        force (bool): Force re-import of all tracks
        pre_sync_artists (bool): Enable pre-import batch sync of album artists (default: True)
    """
    from popularity_helpers import build_artist_index
    from scan_resume import should_resume_scan, get_artists_to_scan, mark_scan_completed
    
    # Check for interrupted scan
    should_resume, resume_from_artist = should_resume_scan("navidrome")
    
    # Unified log: Simple start notification
    if should_resume:
        log_unified(f"Navidrome Import Scan - Resuming from {resume_from_artist}")
    else:
        log_unified("Navidrome Import Scan - Starting Navidrome Import")
    
    # Info log: Detailed start information
    log_info(f"Starting Navidrome library scan")
    log_info(f"Scan parameters - Verbose: {verbose}, Force: {force}, Resume: {should_resume}")
    if should_resume:
        log_info(f"Resuming scan from artist: {resume_from_artist}")
    log_info(f"Start time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    # Debug log: Technical details
    log_debug(f"scan_library_to_db called with verbose={verbose}, force={force}, resume={should_resume}")
    
    def _safe_int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    
    log_info("Building artist index from Navidrome...")
    log_debug("API Call: build_artist_index()")
    artist_map_local = build_artist_index(verbose=verbose) or {}
    log_debug(f"API Response: build_artist_index returned {len(artist_map_local)} artists")
    
    if not artist_map_local:
        log_unified("Navidrome Import Scan - ERROR: No artists available from Navidrome")
        log_info("No artists available from Navidrome; aborting library scan")
        log_debug("build_artist_index returned empty artist map")
        return
    
    # Optimization: Check if library totals match before scanning each album
    # Skip individual album checks if force=False and totals match
    # Note: This optimization checks both album and track counts.
    # If either count differs, the scan will proceed to update.
    # Use --force to bypass this check and always scan.
    if not force:
        log_info("Checking if library is already up-to-date (comparing album and track counts)...")
        log_debug("Getting library stats from Navidrome and database")
        
        # Get Navidrome stats
        nav_stats = get_navidrome_library_stats(artist_map_local)
        navidrome_album_count = nav_stats.get("total_albums", 0)
        navidrome_track_count = nav_stats.get("total_tracks", 0)
        
        # Get database stats
        db_stats = get_database_library_stats()
        db_album_count = db_stats.get("total_albums", 0)
        db_track_count = db_stats.get("total_tracks", 0)
        
        log_info(f"Navidrome: {navidrome_album_count} albums, {navidrome_track_count} songs")
        log_info(f"Database: {db_album_count} albums, {db_track_count} songs")
        log_debug(f"Library comparison - Albums: Nav={navidrome_album_count} vs DB={db_album_count}, Tracks: Nav={navidrome_track_count} vs DB={db_track_count}")
        
        # Skip scan only if BOTH album and track counts match
        if (navidrome_album_count > 0 and navidrome_track_count > 0 and
            navidrome_album_count == db_album_count and 
            navidrome_track_count == db_track_count):
            log_unified("Navidrome Import Scan - Library already up-to-date, skipping scan")
            log_info(f"Library is already up-to-date ({db_album_count} albums, {db_track_count} songs)")
            log_info("Use --force to re-import all tracks")
            log_debug("Early exit: both album and track counts match, skipping detailed scan")
            return
        
        # If counts don't match, log which count(s) differ
        if navidrome_album_count != db_album_count:
            log_info(f"Album count mismatch: Navidrome has {navidrome_album_count}, database has {db_album_count}")
        if navidrome_track_count != db_track_count:
            log_info(f"Track count mismatch: Navidrome has {navidrome_track_count}, database has {db_track_count}")
        log_info("Proceeding with full library scan to sync differences")

    # Cache existing track IDs to avoid re-writing cached rows unless force=True
    existing_track_ids: set[str] = set()
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM tracks")
        log_debug("DB Query: SELECT id FROM tracks")
        existing_track_ids = {row['id'] for row in cursor.fetchall()}
        log_debug(f"Found {len(existing_track_ids)} existing tracks in database")
        conn.close()
    except Exception as e:
        log_debug(f"Prefetch existing track IDs failed: {e}", exc_info=True)

    # Get list of artists already in database and their track counts
    db_artists: dict[str, int] = {}  # artist_name -> track_count
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT artist, COUNT(*) as track_count FROM tracks GROUP BY artist")
        log_debug("DB Query: SELECT artist, COUNT(*) as track_count FROM tracks GROUP BY artist")
        db_artists = {row['artist']: row['track_count'] for row in cursor.fetchall() if row['artist']}
        log_debug(f"Found {len(db_artists)} artists in database with track counts")
        conn.close()
    except Exception as e:
        log_debug(f"Failed to fetch existing artists from database: {e}", exc_info=True)

    # Local import to avoid circular dependency
    from start import fetch_artist_albums, fetch_album_tracks
    
    # Detect missing artists (in Navidrome but not in database)
    missing_artists = []
    artists_with_mismatched_counts = []
    
    for artist_name in artist_map_local.keys():
        if artist_name not in db_artists:
            missing_artists.append(artist_name)
            log_info(f"🆕 Missing artist detected: {artist_name}")
            log_debug(f"Artist '{artist_name}' is in Navidrome but not in database")
        else:
            # Get track count from Navidrome for this artist
            try:
                artist_id = artist_map_local[artist_name].get("id")
                if artist_id:
                    albums = fetch_artist_albums(artist_id)
                    nav_track_count = 0
                    for album in albums:
                        album_id = album.get("id")
                        if album_id:
                            try:
                                album_data = fetch_album_tracks(album_id)
                                tracks = album_data.get("tracks", [])
                                nav_track_count += len(tracks)
                            except Exception as e:
                                log_debug(f"Failed to fetch tracks for album {album.get('name')}: {e}")
                                continue
                    
                    db_track_count = db_artists[artist_name]
                    if nav_track_count != db_track_count:
                        artists_with_mismatched_counts.append({
                            "name": artist_name,
                            "navidrome_count": nav_track_count,
                            "database_count": db_track_count
                        })
                        log_info(f"⚠️ Track count mismatch for {artist_name}: Navidrome={nav_track_count}, Database={db_track_count}")
                        log_debug(f"Artist '{artist_name}' has different track counts: Nav={nav_track_count} vs DB={db_track_count}")
            except Exception as e:
                log_debug(f"Failed to get track count for existing artist '{artist_name}': {e}")
    
    if missing_artists:
        log_unified(f"Navidrome Import Scan - Found {len(missing_artists)} missing artists to import")
        log_info(f"Found {len(missing_artists)} missing artists from Navidrome")
        log_debug(f"Missing artists: {missing_artists}")
    
    if artists_with_mismatched_counts:
        log_unified(f"Navidrome Import Scan - Found {len(artists_with_mismatched_counts)} artists with mismatched track counts")
        log_info(f"Found {len(artists_with_mismatched_counts)} artists with different track counts in Navidrome vs database")
        for artist_info in artists_with_mismatched_counts:
            log_debug(f"Mismatch: {artist_info['name']} (Nav={artist_info['navidrome_count']} vs DB={artist_info['database_count']})")

    total_written = 0
    total_skipped = 0
    total_albums_skipped = 0
    
    # Get artist list and apply resume logic
    all_artists = list(artist_map_local.keys())
    total_artists = len(all_artists)
    
    # Get artists to scan (may skip already scanned if resuming)
    artists_to_scan = get_artists_to_scan(all_artists, resume_from_artist if should_resume else None)
    artists_to_scan_count = len(artists_to_scan)
    
    # Calculate starting index for progress tracking
    artist_start_index = total_artists - artists_to_scan_count
    artist_count = artist_start_index
    
    if should_resume:
        log_info(f"Resuming scan: {artists_to_scan_count} artists remaining ({artist_start_index} already scanned)")
        log_debug(f"Resume: Starting from index {artist_start_index + 1}/{total_artists}")
    else:
        log_info(f"Starting scan of {total_artists} artists from Navidrome")
        log_debug(f"Total artists to scan: {total_artists}")
    
    log_info(f"Missing artists found: {len(missing_artists)}, Artists with mismatched counts: {len(artists_with_mismatched_counts)}")
    
    # Optional: Pre-import batch sync of all album artists before main loop
    # This creates all unique album_artist entries in a single transaction,
    # much faster than creating them one-by-one during the main import loop
    if pre_sync_artists:
        log_info("Pre-syncing album artists before main import (batch mode)...")
        try:
            sync_result = pre_import_sync_album_artists()
            if sync_result.get("success"):
                log_unified(f"Navidrome Import Scan - Pre-sync complete: {sync_result.get('new_artists_created', 0)} new artists, {sync_result.get('sync_time_ms', 0):.0f}ms")
                log_info(f"Pre-sync results: Created {sync_result.get('new_artists_created', 0)} new artists, "
                         f"Found {sync_result.get('unique_album_artists', 0)} unique album artists, "
                         f"Already had {sync_result.get('existing_artists', 0)} artists ({sync_result.get('sync_time_ms', 0):.0f}ms)")
                log_debug(f"New artists created: {sync_result.get('new_artists', [])}")
            else:
                log_info(f"Pre-sync encountered an error: {sync_result.get('error', 'Unknown error')}")
                log_debug(f"Pre-sync error details: {sync_result}")
        except Exception as e:
            log_info(f"Pre-sync of album artists failed: {e}")
            log_debug(f"Pre-sync exception: {e}", exc_info=True)
            # Continue with main loop anyway - album artists will be created per-item if needed
    
    for name in artists_to_scan:
        artist_count += 1
        info = artist_map_local.get(name)
        if not info:
            log_debug(f"Artist '{name}' not found in artist map")
            continue
            
        artist_id = info.get("id")
        if not artist_id:
            log_info(f"Skipping artist '{name}' - no artist ID available")
            log_debug(f"Artist '{name}' has no ID in artist map: {info}")
            continue
        
        log_debug(f"Processing artist {artist_count}/{total_artists}: {name} (ID: {artist_id})")

        try:
            # Use the consolidated scan_artist_to_db function
            scan_artist_to_db(name, artist_id, verbose=verbose, force=force, processed_artists=artist_count, total_artists=total_artists)
        except Exception as e:
            log_info(f"Failed to scan artist '{name}': {e}")
            log_debug(f"scan_artist_to_db failed for '{name}': {e}", exc_info=True)
    
    # Info log: Detailed completion summary
    log_unified(f"Navidrome Import Scan - Complete: {len(missing_artists)} new artists added")
    log_info(f"Navidrome library scan complete")
    log_info(f"End time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log_info(f"Summary:")
    log_info(f"  - Total artists scanned: {total_artists}")
    log_info(f"  - Missing artists imported: {len(missing_artists)}")
    log_info(f"  - Artists with mismatched track counts: {len(artists_with_mismatched_counts)}")
    if missing_artists:
        log_info(f"  - Newly imported: {', '.join(missing_artists[:5])}" + (" and more..." if len(missing_artists) > 5 else ""))
    if artists_with_mismatched_counts:
        log_info(f"  - Track mismatches detected in: {', '.join([a['name'] for a in artists_with_mismatched_counts[:5]])}" + (" and more..." if len(artists_with_mismatched_counts) > 5 else ""))
    
    # Debug log: Technical summary
    log_debug(f"Library scan complete - Artists: {total_artists}, Missing: {len(missing_artists)}, Mismatched: {len(artists_with_mismatched_counts)}, Verbose: {verbose}, Force: {force}")
    
    # Mark scan as completed and clear resume state
    mark_scan_completed("navidrome")
    log_info("Scan completed successfully, progress file cleared")
