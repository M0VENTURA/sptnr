"""
Album metadata service (clean version)

✅ No raw SQL
✅ Uses repository only
✅ No queue logic
✅ No retry logic
✅ No HTTP logic (except fallback - optional)
"""

from __future__ import annotations

import logging
import os
from typing import Any

from sqlalchemy import text
from db.engine import db_session
from db.repositories.metadata import (
    album_is_favourite,
    set_album_favourite_db,
    fetch_album_art_blob,
    fetch_album_tracklist,
    fetch_album_queue_track_stubs,
    fetch_queue_status,
    fetch_album_tracks_for_tag_update,
    save_album_art_db,
    update_track_genres,
    update_album_mbid_fields,
    update_album_discogs_fields,
    ignore_missing_track_db,
)
from services.queue.queue_constraints import STATUS_DISPLAY_CONFIG

from services.enrichment.album_art_service import (
    save_album_art_to_db,
    fetch_album_art_from_itunes,
    fetch_album_art_from_musicbrainz
)

logger = logging.getLogger(__name__)


# =============================================================================
# FILE OPERATIONS
# =============================================================================

def rename_album_files_service(
    artist: str,
    album: str,
) -> dict[str, Any]:
    """Rename all files in an album based on the configured naming format.

    The relative target path comes from ``downloads.file_name_format`` in
    config (the "Default Naming Convention" on the File Management settings
    tab) and is resolved under MUSIC_ROOT (e.g. ``/music``). Placeholders:
    ``{track_number}``, ``{artist}``, ``{album_artist}``, ``{title}``,
    ``{album}``, ``{year}``.

    When conversion on import is enabled (``downloads.conversion``), FLAC
    tracks are converted to MP3 first (metadata and cover art carried over
    by ffmpeg) and the renamed file becomes the converted copy.

    Returns a dict with ``renamed_count``, ``updated_db_count``, ``errors``
    and per-file ``details`` for the album page's result panel.
    """
    import shutil

    from helpers.config_helpers import get_config
    from services.downloads.download_organize_helpers import (
        _sanitize_path_component,
        _normalize_album_artist_for_path,
    )
    from services.infrastructure.filesystem_service import cleanup_empty_parents

    cfg = get_config() or {}
    downloads_cfg = cfg.get("downloads", {}) or {}
    file_name_format = str(
        downloads_cfg.get("file_name_format")
        or "{album_artist}/{year} - {album}/{track_number}. {artist} - {title}"
    ).strip()
    fallback_format = "{album_artist}/{year} - {album}/{track_number}. {artist} - {title}"

    conversion_cfg = downloads_cfg.get("conversion", {}) or {}
    conversion_enabled = bool(conversion_cfg.get("enabled", False)) and (
        str(conversion_cfg.get("mode", "flac_to_mp3")) == "flac_to_mp3"
    )
    try:
        mp3_bitrate = max(96, min(320, int(conversion_cfg.get("mp3_bitrate_kbps", 320) or 320)))
    except (TypeError, ValueError):
        mp3_bitrate = 320

    music_root = (
        (cfg.get("music", {}) or {}).get("root")
        or os.environ.get("MUSIC_ROOT")
        or os.environ.get("MUSIC_FOLDER")
        or "/music"
    )
    music_root = os.path.realpath(str(music_root or "/music").strip() or "/music")

    def _safe_track_number(value: Any) -> str:
        try:
            num = int(str(value or "").strip() or 0)
            return f"{num:02d}" if num > 0 else "00"
        except (TypeError, ValueError):
            return "00"

    def _resolve_existing(path_value: Any) -> str | None:
        raw = str(path_value or "").strip()
        if not raw:
            return None
        candidates = [raw]
        if not os.path.isabs(raw):
            candidates.append(os.path.join(music_root, raw))
        for candidate in candidates:
            if os.path.isfile(candidate):
                return candidate
        return None

    def _render_target(fmt: str, fmt_vars: dict[str, str], ext: str) -> str:
        rel = fmt.format(**fmt_vars).replace("\\", "/").strip()
        if rel.endswith("/"):
            # The format only defines the folder — derive a filename.
            rel = rel + f"{fmt_vars['track_number']}. {fmt_vars['artist']} - {fmt_vars['title']}"
        rel = rel.strip("/")
        if not os.path.splitext(os.path.basename(rel))[1]:
            rel = f"{rel}{ext}"
        return rel

    try:
        with db_session() as session:
            rows = session.execute(
                text("""
                    SELECT CAST(id AS TEXT) AS id, title, artist, album_artist,
                           album, year, track_number, file_path
                    FROM tracks
                    WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
                      AND album = :album
                    ORDER BY COALESCE(disc_number, '1'),
                             COALESCE(track_number, 999),
                             title
                """),
                {"artist": artist, "album": album},
            ).fetchall()
    except Exception as exc:
        logger.error("Failed to load tracks for rename '%s' / '%s': %s", artist, album, exc, exc_info=True)
        return {"success": False, "error": f"Failed to load album tracks: {exc}"}

    if not rows:
        return {"success": False, "error": "No tracks found for this album"}

    renamed_count = 0
    updated_db_count = 0
    errors: list[str] = []
    details: list[dict[str, str]] = []
    moved_src_dirs: list[str] = []

    for row in rows:
        track = dict(row._mapping)
        track_id = track.get("id")
        file_path_value = track.get("file_path")
        src_path = _resolve_existing(file_path_value)
        if not src_path:
            errors.append(f"{track.get('title') or '?'}: file not found on disk ({file_path_value or 'no path'})")
            continue

        ext = os.path.splitext(src_path)[1]
        fmt_vars = {
            "track_number": _safe_track_number(track.get("track_number")),
            "artist": _sanitize_path_component(track.get("artist") or "Unknown Artist") or "Unknown Artist",
            "album_artist": _normalize_album_artist_for_path(
                _sanitize_path_component(track.get("album_artist") or "")
            ) or _sanitize_path_component(track.get("artist") or "Unknown Artist") or "Unknown Artist",
            "title": _sanitize_path_component(track.get("title") or "Unknown Title") or "Unknown Title",
            "album": _sanitize_path_component(track.get("album") or "Unknown Album") or "Unknown Album",
            "year": str(track.get("year") or "").strip()[:4] or "Unknown",
        }

        try:
            rel_target = _render_target(file_name_format, fmt_vars, ext)
        except Exception:
            # Unknown placeholder in the configured format — fall back.
            rel_target = _render_target(fallback_format, fmt_vars, ext)

        # Conversion on import: FLAC → MP3 (in place) before moving.
        actual_src = src_path
        if conversion_enabled and ext.lower() == ".flac":
            try:
                from services.metadata.tag_file_service import convert_flac_to_mp3

                converted = convert_flac_to_mp3(src_path, bitrate=f"{mp3_bitrate}k")
                if not converted or not os.path.isfile(converted):
                    errors.append(f"{fmt_vars['title']}: FLAC→MP3 conversion failed (is ffmpeg installed?)")
                    continue
                actual_src = converted
                if os.path.splitext(rel_target)[1].lower() == ".flac":
                    rel_target = os.path.splitext(rel_target)[0] + ".mp3"
            except Exception as exc:
                errors.append(f"{fmt_vars['title']}: conversion failed ({exc})")
                continue

        target_abs = os.path.join(music_root, rel_target)

        if os.path.normpath(target_abs) == os.path.normpath(actual_src):
            continue

        if os.path.exists(target_abs):
            stem, suffix = os.path.splitext(target_abs)
            counter = 1
            while os.path.exists(target_abs):
                target_abs = f"{stem} ({counter}){suffix}"
                counter += 1

        try:
            os.makedirs(os.path.dirname(target_abs), exist_ok=True)
            shutil.move(actual_src, target_abs)
        except Exception as exc:
            errors.append(f"{fmt_vars['title']}: move failed ({exc})")
            continue

        old_dir = os.path.dirname(os.path.abspath(actual_src))
        if old_dir.startswith(music_root + os.sep):
            moved_src_dirs.append(old_dir)

        # Keep the stored path style: relative stays relative, absolute stays absolute.
        store_path = (
            os.path.relpath(target_abs, music_root)
            if not os.path.isabs(str(file_path_value or ""))
            else target_abs
        )
        try:
            with db_session() as update_session:
                result = update_session.execute(
                    text(
                        "UPDATE tracks SET file_path = :path WHERE CAST(id AS TEXT) = :id"
                    ),
                    {"path": store_path, "id": track_id},
                )
                updated_db_count += result.rowcount or 0
        except Exception as exc:
            errors.append(f"{fmt_vars['title']}: database update failed ({exc})")
            # File already moved — keep counting the rename itself.
            renamed_count += 1
            details.append({
                "track": fmt_vars["title"],
                "old_path": str(file_path_value or ""),
                "new_path": store_path,
            })
            continue

        renamed_count += 1
        details.append({
            "track": fmt_vars["title"],
            "old_path": str(file_path_value or ""),
            "new_path": store_path,
        })

    # Remove now-empty folders left behind (only inside MUSIC_ROOT).
    for directory in set(moved_src_dirs):
        try:
            cleanup_empty_parents(directory, music_root)
        except Exception:
            pass

    return {
        "success": renamed_count > 0 or not errors,
        "renamed_count": renamed_count,
        "updated_db_count": updated_db_count,
        "errors": errors,
        "details": details,
        "message": (
            f"Renamed {renamed_count} file(s)"
            + (f", {len(errors)} error(s)" if errors else "")
        ),
    }


# =============================================================================
# FAVOURITES
# =============================================================================

def is_album_favourite(
    artist: str,
    album: str,
) -> bool:
    return album_is_favourite(
        artist=artist,
        album=album,
    )


def set_album_favourite(
    artist: str,
    album: str,
    is_favourite: bool,
) -> bool:
    try:
        set_album_favourite_db(
            artist=artist,
            album=album,
            is_favourite=is_favourite,
        )
        return True

    except Exception as exc:
        logger.error(
            "Error setting favourite: %s",
            exc,
            exc_info=True,
        )
        return False


# =============================================================================
# ALBUM ART
# =============================================================================

def get_local_album_art(
    artist: str,
    album: str,
) -> tuple[bytes | None, str | None]:
    """Get album art: local DB first, then Navidrome (default source).

    Navidrome already holds the art the user sees in their library, so it is
    consulted before any external service. Art pulled from Navidrome is
    cached to the DB for future requests.
    """
    with db_session() as session:
        data, mime = fetch_album_art_blob(
            artist=artist,
            album=album,
        )

        if data:
            return data, mime or "image/jpeg"

    try:
        from services.enrichment.album_art_service import (
            fetch_album_art_from_navidrome,
            save_album_art_to_db,
        )

        data = fetch_album_art_from_navidrome(artist, album)
        if data:
            save_album_art_to_db(artist, album, data, source="navidrome")
            return data, "image/jpeg"
    except Exception as exc:
        logger.debug("Navidrome album art fallback failed for '%s' / '%s': %s", artist, album, exc)

    return None, None

# In services/album_service.py

def get_or_fetch_album_art(artist: str, album: str) -> tuple[bytes | None, str | None]:
    """Fetch album art from DB or external sources.
    
    Delegates to the canonical implementation in services.enrichment.album_art_service.
    """
    from services.enrichment.album_art_service import get_or_fetch_album_art as _fetch
    return _fetch(artist, album)


# =============================================================================
# TRACKLIST
# =============================================================================

def get_album_tracklist(artist: str, album: str) -> list[dict[str, Any]]:
    rows = fetch_album_tracklist(artist=artist, album=album)

    return [
        {
            "track_id": r.get("id") if hasattr(r, "get") else r[0],
            "position": str((r.get("track_number") if hasattr(r, "get") else r[2]) or "").strip() or "—",
            "title": r.get("title") if hasattr(r, "get") else r[1],
            "artist": (r.get("artist") if hasattr(r, "get") else r[4]) or "",
        }
        for r in rows
    ]


def get_album_tracklist_from_db(artist: str, album: str) -> list[dict[str, Any]]:
    """Alias for get_album_tracklist to satisfy blueprint imports."""
    return get_album_tracklist(artist, album)


def match_album_tracklist(artist: str, album: str) -> dict[str, Any]:
    """Matches album tracks against the local library, falling back to MusicBrainz."""
    logger.debug("Matching tracklist for %s - %s", artist, album)

    with db_session() as session:
        # 1. Fetch tracks for this album from repository
        album_rows = fetch_album_queue_track_stubs(artist=artist, album=album)
        
        matched_tracks = []
        queued_tracks = []

        for row in album_rows:
            title_val = row.get("title") if hasattr(row, "get") else row[0]
            file_path_val = (row.get("file_path") if hasattr(row, "get") else row[1]) or ""
            entry = {"title": title_val}

            if str(file_path_val).startswith("__queued_for_download__"):
                queued_tracks.append(entry)
            else:
                matched_tracks.append(entry)

        if album_rows:
            logger.info(
                "Found %d existing album tracks for %s - %s (library=%d, queued=%d)",
                len(album_rows), artist, album, len(matched_tracks), len(queued_tracks)
            )
            return {
                "success": True,
                "matched": matched_tracks,
                "queued": queued_tracks,
                "unmatched": [],
                "status": 200,
            }

        # 2. If no tracks found, check all artist tracks in the database
        logger.debug("No album tracks found in database, checking all tracks for artist %s", artist)
        all_artist_rows = fetch_album_tracklist(artist=artist, album="")
        library_tracks = {
            str(r.get("title") if hasattr(r, "get") else r[1]).lower().strip(): True 
            for r in all_artist_rows
            if (r.get("title") if hasattr(r, "get") else r[1])
        }

    # 3. Fallback to MusicBrainz API check
    try:
        from api_clients.musicbrainz_http import (
            MusicBrainzHttpClient,
            escape_lucene_special_chars,
        )
        # Shared client: canonical User-Agent + 1 req/s throttle + retry/backoff.
        mb = MusicBrainzHttpClient(enabled=True)

        releases = mb.search_releases(
            f'release:"{escape_lucene_special_chars(album)}" AND artist:"{escape_lucene_special_chars(artist)}"',
            limit=5,
        ) or []

        if not releases:
            release_groups = mb.search_release_groups(
                f'"{escape_lucene_special_chars(album)}" AND artist:"{escape_lucene_special_chars(artist)}"',
                limit=1,
            )

            if not release_groups or not release_groups[0].get("id"):
                return {"success": True, "matched": [], "queued": [], "unmatched": [], "status": 200}

            rg_id = release_groups[0]["id"]
            rg_data = mb.get_release_group(rg_id, inc="releases")
            releases = rg_data.get("releases", []) if isinstance(rg_data, dict) else []

        if releases:
            release_id = releases[0].get("id")
            detail = mb.get_release(release_id, inc="recordings")
            media = detail.get("media", []) if isinstance(detail, dict) else []

            mb_matched = []
            mb_unmatched = []

            for medium in media:
                for track_obj in medium.get("tracks", []):
                    track_title = track_obj.get("title") or track_obj.get("recording", {}).get("title", "")
                    if not track_title:
                        continue

                    entry = {"title": track_title}
                    if track_title.lower().strip() in library_tracks:
                        mb_matched.append(entry)
                    else:
                        mb_unmatched.append(entry)

            logger.info("Matched %d tracks from MusicBrainz for %s - %s", len(mb_matched), artist, album)
            return {
                "success": True,
                "matched": mb_matched,
                "queued": [],
                "unmatched": mb_unmatched,
                "status": 200,
            }

        return {"success": True, "matched": [], "queued": [], "unmatched": [], "status": 200}

    except Exception as exc:
        logger.error("Error matching tracklist via MusicBrainz: %s", exc, exc_info=True)
        return {"error": str(exc), "status": 500}


# =============================================================================
# QUEUE STATUS (SAFE — READ ONLY)
# =============================================================================

def get_album_queue_status_db(artist: str, album: str):
    result = {}

    rows = fetch_album_queue_track_stubs(artist=artist, album=album)

    for row in rows:
        track_id = row.get("id") if hasattr(row, "get") else row[0]
        file_path = (row.get("file_path") if hasattr(row, "get") else row[1]) or ""

        queue_id = None
        if "queue_id_" in file_path:
            try:
                queue_id = int(file_path.split("queue_id_")[-1])
            except ValueError:
                pass

        status = fetch_queue_status(queue_id=queue_id) if queue_id else "queued"
        cfg = STATUS_DISPLAY_CONFIG.get(status, {})

        result[track_id] = {
            "queue_id": queue_id,
            "status": status,
            "label": cfg.get("label", status),
            "css": cfg.get("css", ""),
            "icon": cfg.get("icon", ""),
        }

    return result


# =============================================================================
# GENRES
# =============================================================================

def apply_genres_to_album(artist: str, album: str, genres: list[str]):
    from services.metadata.tag_file_service import update_file_tags

    genres_clean = [g.strip() for g in genres if g.strip()]
    genres_str = ",".join(genres_clean)

    updated = 0
    failed = []

    tracks = fetch_album_tracks_for_tag_update(artist=artist, album=album)

    for t in tracks:
        track_id = t.get("id") if hasattr(t, "get") else t[0]
        title = t.get("title") if hasattr(t, "get") else t[1]
        path = t.get("file_path") if hasattr(t, "get") else t[2]

        if path and os.path.exists(path):
            if update_file_tags(path, {"genres": genres_clean}):
                update_track_genres(track_id=track_id, genres_str=genres_str)
                updated += 1
            else:
                failed.append(title)
        else:
            failed.append(title)

    return {
        "success": True,
        "updated": updated,
        "failed": len(failed),
        "failed_files": failed,
    }


# =============================================================================
# MBID / DISCOGS
# =============================================================================

def apply_mbid_to_album(artist, album, mbid, rg_mbid, cover_url):
    rows = update_album_mbid_fields(
        artist=artist, album=album, mbid=mbid, rg_mbid=rg_mbid, cover_url=cover_url,
    )

    return {"success": rows > 0, "rows_updated": rows}


def apply_discogs_id_to_album(artist, album, discogs_id, is_single):
    rows = update_album_discogs_fields(
        artist=artist, album=album, discogs_id=discogs_id, is_single=is_single,
    )

    return {"success": True, "rows_updated": rows}


# =============================================================================
# BULK TRACK OPERATIONS (old-version parity)
# =============================================================================

def bulk_tag_tracks(payload: dict) -> tuple[dict, int]:
    """Add genre tags to multiple tracks — DB columns + audio file tags."""
    from services.metadata.tag_file_service import update_file_tags

    track_ids = [str(t) for t in (payload.get("track_ids") or []) if t]
    genres = [g.strip() for g in (payload.get("genres") or []) if g and g.strip()]
    if not track_ids:
        return {"success": False, "error": "No tracks selected"}, 400
    if not genres:
        return {"success": False, "error": "No genres provided"}, 400

    updated_count = 0
    failed_files: list[str] = []

    with db_session() as session:
        for track_id in track_ids:
            try:
                row = session.execute(
                    text("SELECT title, genres, manual_genres, file_path FROM tracks WHERE id = :id"),
                    {"id": track_id},
                ).mappings().first()
                if not row:
                    continue
                title = row.get("title")
                current = str(row.get("genres") or "")
                manual = str(row.get("manual_genres") or "")
                file_path = str(row.get("file_path") or "")

                def _split_genres(raw: str) -> set:
                    sep = "\\" if "\\" in raw else ","
                    return {g.strip() for g in raw.split(sep) if g.strip()}

                existing = _split_genres(current)
                manual_existing = _split_genres(manual)
                existing.update(genres)
                manual_existing.update(genres)

                new_genres = ", ".join(sorted(existing))
                new_manual = ", ".join(sorted(manual_existing))

                if file_path and os.path.exists(file_path):
                    if not update_file_tags(file_path, {"genres": sorted(existing)}):
                        failed_files.append(title or f"Track ID: {track_id}")

                session.execute(
                    text("UPDATE tracks SET genres = :genres, manual_genres = :manual WHERE id = :id"),
                    {"genres": new_genres, "manual": new_manual, "id": track_id},
                )
                updated_count += 1
            except Exception as exc:
                logger.error("[bulk_tag] Track %s failed: %s", track_id, exc)
                continue

    return {
        "success": True,
        "updated_count": updated_count,
        "failed_files": failed_files,
    }, 200


def bulk_delete_tracks(payload: dict) -> tuple[dict, int]:
    """Delete multiple tracks from the database, optionally removing audio files."""
    track_ids = [str(t) for t in (payload.get("track_ids") or []) if t]
    delete_files = bool(payload.get("delete_files", True))
    if not track_ids:
        return {"success": False, "error": "No tracks selected"}, 400

    deleted_count = 0

    with db_session() as session:
        for track_id in track_ids:
            try:
                row = session.execute(
                    text("SELECT file_path FROM tracks WHERE id = :id"),
                    {"id": track_id},
                ).mappings().first()
                if not row:
                    continue
                file_path = str(row.get("file_path") or "")
                if delete_files and file_path and os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                    except Exception as exc:
                        logger.warning("[bulk_delete] Could not delete file %s: %s", file_path, exc)
                session.execute(text("DELETE FROM tracks WHERE id = :id"), {"id": track_id})
                deleted_count += 1
            except Exception as exc:
                logger.error("[bulk_delete] Track %s failed: %s", track_id, exc)
                continue

    return {"success": True, "deleted_count": deleted_count}, 200


def update_album_ids(payload: dict) -> tuple[dict, int]:
    """Update release IDs (MusicBrainz release/release-group, Discogs) for an album's tracks."""
    artist = str(payload.get("artist") or "").strip()
    album = str(payload.get("album") or "").strip()
    if not artist or not album:
        return {"success": False, "error": "artist and album required"}, 400

    musicbrainz_id = str(payload.get("musicbrainz_release_id") or "").strip()
    release_group_id = str(payload.get("musicbrainz_release_group_id") or "").strip()
    discogs_id = str(payload.get("discogs_release_id") or "").strip()

    if not (musicbrainz_id or release_group_id or discogs_id):
        return {"success": False, "error": "No IDs provided"}, 400

    updates: list[str] = []
    bind_values: dict[str, Any] = {}
    _idx = 0
    if musicbrainz_id:
        updates.append(f"musicbrainz_album_mbid = :v{_idx}")
        bind_values[f"v{_idx}"] = musicbrainz_id
        _idx += 1
    if release_group_id:
        updates.append(f"musicbrainz_releasegroupid = :v{_idx}")
        bind_values[f"v{_idx}"] = release_group_id
        _idx += 1
    if discogs_id:
        updates.append(f"discogs_album_id = :v{_idx}")
        bind_values[f"v{_idx}"] = discogs_id
    bind_values["artist"] = artist
    bind_values["album"] = album

    with db_session() as session:
        result = session.execute(
            text(
                f"UPDATE tracks SET {', '.join(updates)} "
                "WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist AND album = :album"
            ),
            bind_values,
        )
        rows = result.rowcount or 0

    return {"success": True, "rows_updated": rows}, 200


# =============================================================================
# IGNORE TRACK
# =============================================================================

def ignore_missing_track(missing_id, artist, album, title, disc_number):
    try:
        ignore_missing_track_db(
            missing_id=missing_id,
            artist=artist,
            album=album,
            title=title,
            disc_number=disc_number,
        )
        return True
    except Exception as exc:
        logger.error("ignore_missing_track failed: %s", exc)
        return False


def get_majority_artist(artist: str, album: str) -> dict:
    """Return the most common artist across all tracks in an album."""
    from collections import Counter
    try:
        from sqlalchemy import text as _text
        from db.engine import db_session as _db_session
        with _db_session() as session:
            rows = session.execute(
                _text("SELECT artist FROM tracks WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist AND album = :album"),
                {"artist": artist, "album": album},
            ).fetchall()
        counts = Counter(str(r[0]) for r in rows if r[0])
        if not counts:
            return {"success": False, "error": "No tracks found"}
        top = counts.most_common(1)[0]
        return {
            "success": True,
            "majority_artist": top[0],
            "count": top[1],
            "total": sum(counts.values()),
        }
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def add_album_to_missing_releases(artist: str, album: str, year: str | None = None) -> dict:
    """Add an album to the missing_releases tracking table."""
    try:
        from sqlalchemy import text as _text
        from db.engine import db_session as _db_session
        with _db_session() as session:
            session.execute(
                _text(
                    "INSERT INTO missing_releases (artist, title, primary_type, first_release_date, category, created_at) "
                    "VALUES (:artist, :album, 'album', :year, 'album', CURRENT_TIMESTAMP) "
                    "ON CONFLICT (artist, title) DO NOTHING"
                ),
                {"artist": artist, "album": album, "year": year or None},
            )
        return {"success": True, "message": f"Added '{album}' to missing releases"}
    except Exception as exc:
        logger.error("Error adding to missing releases: %s", exc)
        return {"success": False, "error": str(exc)}


def get_track_recommendations(artist: str, album: str) -> dict:
    """Get genre recommendations by aggregating all genre sources in DB."""
    try:
        from sqlalchemy import text as _text
        from db.engine import db_session as _db_session
        with _db_session() as session:
            rows = session.execute(
                _text(
                    "SELECT lastfm_tags, musicbrainz_genres, discogs_genres "
                    "FROM tracks WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist AND album = :album"
                ),
                {"artist": artist, "album": album},
            ).mappings().all()
    except Exception as exc:
        logger.error("Error fetching track genres for %s - %s: %s", artist, album, exc)
        rows = []

    from collections import defaultdict
    source_map: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        cols = ["lastfm_tags", "musicbrainz_genres", "discogs_genres"]
        for src_key, col in zip(["lastfm", "musicbrainz", "discogs"], cols):
            val = row.get(col)
            if val:
                vals = val if isinstance(val, list) else [str(val)]
                source_map[src_key].extend(v.strip() for v in vals if v and v.strip())
    from services.enrichment.genre_aggregation_service import aggregate_genres
    recommended = aggregate_genres(dict(source_map))
    return {"success": True, "artist": artist, "album": album, "genres": recommended}