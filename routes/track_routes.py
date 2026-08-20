"""Track API routes — migrated from the old monolithic app.py."""

from __future__ import annotations

import logging
import os
from typing import Any

from quart import Blueprint, jsonify, request, Response, send_file

from sqlalchemy import text

from db.engine import db_session
from helpers.config_helpers import get_config
from helpers.response_helpers import _ok, _fail

logger = logging.getLogger(__name__)

track_bp = Blueprint("track_api", __name__, url_prefix="/api/track")


def _coerce_optional_int(value: Any, allow_prefix: bool = False) -> int | None:
    """Return an int for numeric input, otherwise None."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    candidate = text
    if allow_prefix and "/" in text:
        candidate = text.split("/", 1)[0].strip()
    if not candidate:
        return None
    signless = candidate[1:] if candidate.startswith("-") else candidate
    if not signless.isdigit():
        return None
    try:
        return int(candidate)
    except (TypeError, ValueError):
        return None


def _parse_flag_bool(value: Any) -> bool:
    """Coerce JSON 0/1, ``true``/``false`` and checkbox strings to a bool."""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "checked"}


def _values_equal(a: Any, b: Any) -> bool:
    """Compare a DB value and an incoming API value loosely.

    ``None`` and the empty string are treated as equivalent so that forms
    which always submit every field (e.g. ``album_artist: null``) never count
    as a change when the stored value is already empty.
    """
    a = "" if a is None else str(a).strip()
    b = "" if b is None else str(b).strip()
    return a == b


def _get_track_column_types(session) -> dict[str, str]:
    """Return tracks column name -> normalized data type."""
    try:
        result = session.execute(
            text("""
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_name = 'tracks'
            """)
        )
        return {
            str(row[0]).lower(): str(row[1]).lower()
            for row in result.fetchall()
        }
    except Exception:
        return {}


def _normalize_track_updates(
    updates: dict[str, Any],
    column_types: dict[str, str],
) -> dict[str, Any]:
    """Coerce incoming values to match the real tracks column types.

    The frontend sends flags as JSON 0/1 integers; PostgreSQL BOOLEAN
    columns reject those with a DatatypeMismatch. Integer/numeric columns
    also need their values cast so text payloads don't error either.
    """
    int_types = {"integer", "bigint", "smallint"}
    numeric_types = {"numeric", "double precision", "real", "decimal"}

    normalized: dict[str, Any] = {}
    for column_name, value in updates.items():
        column_type = column_types.get(column_name, "")

        if column_type == "boolean":
            normalized[column_name] = _parse_flag_bool(value)
        elif column_type in int_types:
            int_value = _coerce_optional_int(
                value,
                allow_prefix=(column_name == "track_number"),
            )
            normalized[column_name] = int_value
        elif column_type in numeric_types:
            try:
                normalized[column_name] = float(value) if str(value).strip() else None
            except (TypeError, ValueError):
                normalized[column_name] = None
        else:
            normalized[column_name] = value

    return normalized


# ---------------------------------------------------------------------------
# GET /api/track/<track_id> — single track metadata
# ---------------------------------------------------------------------------

@track_bp.route("/<track_id>", methods=["GET"])
def api_get_track(track_id):
    """Get track metadata by ID."""
    try:
        with db_session() as session:
            result = session.execute(text("SELECT * FROM tracks WHERE CAST(id AS TEXT) = :id"), {"id": track_id})
            row = result.fetchone()
            if not row:
                return jsonify({"error": "Track not found"}), 404
            track = dict(row._mapping)
            # Return track fields at the top level (legacy frontend contract used
            # by the album/downloads edit-track modals, genre removal, etc.), and
            # also expose them under ``track`` for API consumers.
            payload = {"success": True, "track": track}
            payload.update(track)
            return jsonify(payload)
    except Exception as exc:
        logger.error("Error fetching track %s: %s", track_id, exc)
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# GET /api/track/<track_id>/audio — stream audio file
# ---------------------------------------------------------------------------

@track_bp.route("/<track_id>/audio")
async def api_track_audio(track_id):
    """Stream an audio file for in-browser playback."""
    try:
        with db_session() as session:
            result = session.execute(text("SELECT file_path FROM tracks WHERE CAST(id AS TEXT) = :id"), {"id": track_id})
            row = result.fetchone()
            if not row or not row[0]:
                return Response("", status=404)
            file_path = row[0]
        if "__queued_for_download__" in file_path:
            return Response("", status=404)
        resolved = os.path.realpath(file_path)
        if not os.path.isfile(resolved):
            return Response("", status=404)
        cfg = get_config()
        music_folder = os.path.realpath(
            cfg.get("navidrome", {}).get("music_folder", "")
            or os.environ.get("MUSIC_FOLDER", "")
            or os.environ.get("MUSIC_DIR", "/music")
        )
        if not resolved.startswith(music_folder + os.sep) and resolved != music_folder:
            return Response("", status=403)
        ext = os.path.splitext(resolved)[1].lower()
        mime_map = {
            ".mp3": "audio/mpeg", ".flac": "audio/flac", ".ogg": "audio/ogg",
            ".opus": "audio/ogg; codecs=opus", ".m4a": "audio/mp4",
            ".aac": "audio/aac", ".wav": "audio/wav",
        }
        return await send_file(resolved, mimetype=mime_map.get(ext, "application/octet-stream"), conditional=True)
    except Exception as exc:
        logger.error("Error streaming track %s: %s", track_id, exc)
        return Response("", status=500)


# ---------------------------------------------------------------------------
# POST /api/track/<track_id>/rename-file
# ---------------------------------------------------------------------------

@track_bp.route("/<track_id>/rename-file", methods=["POST"])
def api_track_rename_file(track_id):
    """Rename/move a single track's file using the configured naming format.

    The destination is resolved under MUSIC_ROOT from
    ``downloads.file_name_format`` (same convention the album rename flow and
    the download organizer use), and is containment-checked so a crafted
    metadata value or format string cannot move the file outside the music
    library.
    """
    try:
        from services.downloads.download_organize_helpers import _build_target_path
        from services.infrastructure.filesystem_service import is_path_under_directory

        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT file_path, artist, album_artist, album, title,
                           track_number, disc_number, year
                    FROM tracks WHERE CAST(id AS TEXT) = :id
                """),
                {"id": track_id},
            )
            row = result.fetchone()
            if not row:
                return jsonify({"success": False, "error": "Track not found"}), 404
            src = str(row[0] or "").strip()
            artist = str(row[1] or "").strip()
            album_artist = str(row[2] or "").strip()
            album = str(row[3] or "").strip()
            title = str(row[4] or "").strip()
            track_number = row[5]
            disc_number = row[6]
            year = row[7]
        cfg = get_config() or {}
        music_root = os.path.realpath(
            (cfg.get("music", {}) or {}).get("root")
            or os.environ.get("MUSIC_ROOT")
            or os.environ.get("MUSIC_FOLDER")
            or "/music"
        )
        # Navidrome imports store RELATIVE paths ("Artist/Album/01 - Song.mp3").
        # Resolve against the music root so the existence check, the unchanged
        # comparison and the rename work regardless of the process CWD.
        src_resolved = src if os.path.isabs(src) else os.path.join(music_root, src)
        if not src or not os.path.isfile(src_resolved):
            return jsonify({"success": False, "error": f"File not found: {src}"}), 404

        # Build the relative destination from the configured naming format and
        # resolve it under the music root.
        dest = _build_target_path(
            music_root,
            album_artist or artist,
            year,
            album,
            artist,
            title,
            track_number,
            src_resolved,
            disc_number=disc_number,
        )
        dest = os.path.realpath(dest)
        if not is_path_under_directory(dest, music_root):
            return jsonify({"success": False, "error": "Refusing to move file outside the music library"}), 403
        if os.path.normpath(dest) == os.path.normpath(src_resolved):
            return jsonify({"success": True, "renamed": False, "unchanged": True, "old_path": src, "new_path": dest})

        # Avoid clobbering an existing file.
        if os.path.exists(dest):
            stem, suffix = os.path.splitext(dest)
            counter = 1
            while os.path.exists(dest):
                dest = f"{stem} ({counter}){suffix}"
                counter += 1

        os.makedirs(os.path.dirname(dest), exist_ok=True)
        os.rename(src_resolved, dest)

        # Remove now-empty source folders (only inside MUSIC_ROOT) so a
        # successful rename does not leave an empty album shell behind.
        try:
            from services.infrastructure.filesystem_service import cleanup_empty_parents
            if is_path_under_directory(src_resolved, music_root):
                cleanup_empty_parents(src_resolved, music_root)
        except Exception:
            pass

        # Keep the stored path style: relative stays relative, absolute stays absolute.
        store_path = (
            os.path.relpath(dest, music_root)
            if not os.path.isabs(src)
            else dest
        )
        with db_session() as session:
            session.execute(
                text("UPDATE tracks SET file_path = :path WHERE CAST(id AS TEXT) = :id"),
                {"path": store_path, "id": track_id},
            )
        return jsonify({"success": True, "renamed": True, "old_path": src, "new_path": dest})
    except Exception as exc:
        logger.error("Error renaming track file %s: %s", track_id, exc, exc_info=True)
        return jsonify({"success": False, "error": str(exc)}), 500


# ---------------------------------------------------------------------------
# POST /api/track/<track_id>/toggle-manual-single
# ---------------------------------------------------------------------------

@track_bp.route("/<track_id>/toggle-manual-single", methods=["POST"])
def api_toggle_manual_single(track_id):
    """Toggle single_manual_override flag for a track."""
    try:
        with db_session() as session:
            result = session.execute(text("SELECT single_manual_override FROM tracks WHERE CAST(id AS TEXT) = :id"), {"id": track_id})
            row = result.fetchone()
            if not row:
                return jsonify({"error": "Track not found"}), 404
            current = bool(row[0])
            new_val = 0 if current else 1
            session.execute(text("UPDATE tracks SET single_manual_override = :val WHERE CAST(id AS TEXT) = :id"), {"val": new_val, "id": track_id})
            return jsonify({"success": True, "single_manual_override": bool(new_val)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Track favourites (bookmarks-backed)
# ---------------------------------------------------------------------------
# Split into dedicated GET / POST / DELETE handlers (previously one overloaded
# handler dispatched on request.method).

@track_bp.get("/favourite")
async def api_track_favourite_get():
    """Check whether a track is favourited."""
    track_id = request.args.get("track_id", "").strip()
    if not track_id:
        return jsonify({"error": "track_id required"}), 400
    with db_session() as session:
        result = session.execute(
            text("SELECT 1 FROM bookmarks WHERE type = 'track_favourite' AND LOWER(name) = LOWER(:id) LIMIT 1"),
            {"id": track_id},
        )
        return jsonify({"success": True, "is_favourite": result.fetchone() is not None}), 200


@track_bp.post("/favourite")
async def api_track_favourite_add():
    """Add a track to favourites."""
    data = (await request.get_json()) or {}
    track_id = str(data.get("track_id") or "").strip()
    if not track_id:
        return jsonify({"error": "track_id required"}), 400
    with db_session() as session:
        session.execute(
            text("INSERT INTO bookmarks (type, name) VALUES ('track_favourite', :id) ON CONFLICT DO NOTHING"),
            {"id": track_id},
        )
    return jsonify({"success": True, "is_favourite": True}), 200


@track_bp.delete("/favourite")
async def api_track_favourite_remove():
    """Remove a track from favourites."""
    track_id = request.args.get("track_id", "").strip()
    if not track_id:
        return jsonify({"error": "track_id required"}), 400
    with db_session() as session:
        session.execute(
            text("DELETE FROM bookmarks WHERE type = 'track_favourite' AND LOWER(name) = LOWER(:id)"),
            {"id": track_id},
        )
    return jsonify({"success": True, "is_favourite": False}), 200


# ---------------------------------------------------------------------------
# POST /api/track/update-metadata
# ---------------------------------------------------------------------------

@track_bp.route("/update-metadata", methods=["POST"])
async def api_track_update_metadata():
    """Update track metadata comprehensively."""
    try:
        data = (await request.get_json()) or {}
        track_id = str(data.get("track_id") or "").strip()
        if not track_id:
            return jsonify({"error": "track_id required"}), 400
        with db_session() as session:
            # Only fields backed by real tracks columns — anything else would
            # raise an "undefined column" SQL error.
            allowed_fields = {
                "title", "artist", "album", "album_artist", "writer", "work",
                "genres", "stars", "is_single", "single_confidence",
                "year", "track_number", "disc_number", "mbid", "isrc",
                "is_cover", "alternate_take", "is_compilation",
                "is_live", "is_acoustic", "is_remix",
                "musicbrainz_albumid", "musicbrainz_artistid", "musicbrainz_albumartistid",
                "musicbrainz_releasegroupid", "musicbrainz_releasetrackid", "musicbrainz_workid",
            }
            updates = {}
            for field in allowed_fields:
                if field in data:
                    updates[field] = data[field]
            if not updates:
                return jsonify({"error": "No fields to update"}), 400

            # Coerce values to the real column types so PostgreSQL BOOLEAN
            # columns accept the 0/1 integers the edit modals send, and so
            # integer/numeric columns don't reject string payloads either.
            updates = _normalize_track_updates(updates, _get_track_column_types(session))

            # Normalise a genres payload that arrived as a single
            # backslash/comma/semicolon-joined string (the edit modals join
            # with ``\`` — ``metal\nu metal\rock``).  Store a clean
            # comma-joined list so the DB, the file tags and the genre
            # playlist pools all see three genres, never one literal
            # ``metal\nu metal\rock`` string.
            if "genres" in updates and updates["genres"] is not None:
                _g_raw = updates["genres"]
                if isinstance(_g_raw, list):
                    _g_parts = [str(g).strip() for g in _g_raw if str(g).strip()]
                else:
                    import re as _re
                    _g_parts = [
                        g.strip()
                        for g in _re.split(r"[,;/\\]+", str(_g_raw))
                        if g.strip()
                    ]
                updates["genres"] = ", ".join(_g_parts) if _g_parts else None

            # Album-scoped fields describe the release as a whole. By default a
            # single-track edit only touches that one track — fixing a song that
            # was mis-tagged onto the wrong album must not rewrite every sibling
            # on the old album. Callers opt in to album-wide propagation by
            # sending ``apply_to_album: true``.
            #
            # Two guards keep an opted-in album edit from clobbering sibling
            # tracks:
            #   1. Only fields whose value really changed are propagated — the
            #      edit modals submit every field, so an unchanged album/year
            #      must not be re-written onto the rest of the album.
            #   2. Propagation only touches sibling tracks that still hold the
            #      old value of the changed field, so e.g. fixing one bonus
            #      track's year never rewrites a different edition/release
            #      that merely shares the album name.
            album_scoped_fields = {
                "album", "album_artist", "year",
                "musicbrainz_albumid", "musicbrainz_albumartistid",
                "musicbrainz_releasegroupid",
            }
            album_tracks_updated = 0
            apply_to_album = data.get("apply_to_album") is True
            album_updates = {
                k: v for k, v in updates.items() if k in album_scoped_fields
            }
            if apply_to_album and album_updates:
                try:
                    current_row = session.execute(
                        text("SELECT * FROM tracks WHERE CAST(id AS TEXT) = :id"),
                        {"id": track_id},
                    ).fetchone()
                    current = dict(current_row._mapping) if current_row else {}
                    old_album = current.get("album")
                    if old_album:
                        changed = {
                            k: v for k, v in album_updates.items()
                            if not _values_equal(v, current.get(k))
                        }
                        if changed:
                            old_album_artist = (
                                current.get("album_artist")
                                or current.get("artist")
                                or ""
                            )
                            set_clause = ", ".join(
                                f"{k} = :{k}" for k in changed
                            )
                            conditions = [
                                "CAST(id AS TEXT) <> :id",
                                "album = :old_album",
                                "COALESCE(NULLIF(album_artist, ''), artist) = :old_album_artist",
                            ]
                            params = {
                                **changed,
                                "id": track_id,
                                "old_album": old_album,
                                "old_album_artist": old_album_artist,
                            }
                            for k, v in changed.items():
                                if k in ("album", "album_artist"):
                                    continue
                                old = current.get(k)
                                params[f"old_{k}"] = "" if old is None else str(old)
                                conditions.append(
                                    f"COALESCE(NULLIF({k}::text, ''), '') = :old_{k}"
                                )
                            result = session.execute(
                                text(
                                    "UPDATE tracks SET "
                                    + set_clause
                                    + " WHERE "
                                    + " AND ".join(conditions)
                                ),
                                params,
                            )
                            album_tracks_updated = result.rowcount or 0
                except Exception as album_err:
                    logger.warning(
                        "Album-scoped propagation failed for %s: %s",
                        track_id,
                        album_err,
                    )

            set_clause = ", ".join(f"{k} = :{k}" for k in updates)
            params = {**updates, "id": track_id}
            session.execute(text(f"UPDATE tracks SET {set_clause} WHERE CAST(id AS TEXT) = :id"), params)

            # Legacy parity undo: clearing is_live/is_acoustic strips the
            # "(Live)"/"(Acoustic)" suffix the album stage appended, so
            # wrongly-detected live tracks can be fixed from the UI.
            if any(f in updates for f in ("is_live", "is_acoustic")) \
                    and not (updates.get("is_live") or updates.get("is_acoustic")):
                try:
                    from services.popularity.stages.album_stage import revert_track_live_state
                    revert_track_live_state(track_id)
                except Exception as revert_err:
                    logger.debug("Live-state revert failed for %s: %s", track_id, revert_err)

        # Sync tags back to the audio file by default for the album/artist/
        # track editing flows (frontend sends sync_to_file: true).
        file_synced = False
        if data.get("sync_to_file", True):
            try:
                from services.metadata.tag_file_service import sync_track_tags_to_file
                file_synced = bool(sync_track_tags_to_file(track_id))
            except Exception as sync_err:
                logger.warning(
                    "Track metadata DB update succeeded but file sync failed for %s: %s",
                    track_id,
                    sync_err,
                )
        return jsonify({
            "success": True,
            "updated": list(updates.keys()),
            "file_synced": file_synced,
            "album_tracks_updated": album_tracks_updated,
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# GET /api/track/genre-recommendations
# ---------------------------------------------------------------------------

@track_bp.route("/genre-recommendations", methods=["GET"])
def track_genre_recommendations():
    """Get genre recommendations for a track from various sources."""
    track_id = request.args.get("track_id", "").strip()
    if not track_id:
        return jsonify({"error": "track_id required"}), 400
    try:
        with db_session() as session:
            result = session.execute(
                text("SELECT spotify_genres, lastfm_tags, musicbrainz_genres, discogs_genres FROM tracks WHERE CAST(id AS TEXT) = :id"),
                {"id": track_id},
            )
            row = result.fetchone()
            if not row:
                return jsonify({"error": "Track not found"}), 404
            genres = {}
            keys = ["spotify_genres", "lastfm_tags", "musicbrainz_genres", "discogs_genres"]
            for idx, key in enumerate(keys):
                val = row[idx]
                if val:
                    if isinstance(val, str):
                        try:
                            import json
                            parsed = json.loads(val) if val.startswith("[") else [val]
                        except json.JSONDecodeError:
                            parsed = [g.strip() for g in val.replace("\\", ",").split(",") if g.strip()]
                    elif isinstance(val, list):
                        parsed = val
                    else:
                        parsed = []
                    genres[key] = parsed if isinstance(parsed, list) else [str(parsed)]
        return jsonify({"success": True, "genres": genres})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# POST /api/track/<track_id>/rescan-single
# ---------------------------------------------------------------------------

@track_bp.route("/<track_id>/rescan-single", methods=["POST"])
async def api_rescan_single_track(track_id):
    """Force a fresh single detection scan for one track.

    When a ``source`` key is supplied (from the per-source Re-check buttons),
    only that detection source is dropped from the stored ``single_sources``
    JSON so the next scan re-runs the check for that source alone.
    """
    try:
        import json as _json

        with db_session() as session:
            source = ""
            try:
                body = (await request.get_json(silent=True)) or {}
                source = str(body.get("source") or "").strip()
            except Exception:
                source = ""

            if source:
                row = session.execute(
                    text("SELECT single_sources FROM tracks WHERE CAST(id AS TEXT) = :id"),
                    {"id": track_id},
                ).fetchone()
                raw = str(row[0] or "") if row and row[0] else ""
                remaining: list = []
                if raw.strip():
                    try:
                        parsed = _json.loads(raw)
                        if isinstance(parsed, list):
                            remaining = [
                                entry for entry in parsed
                                if not (
                                    isinstance(entry, dict)
                                    and str(entry.get("source") or "") == source
                                ) and not (
                                    isinstance(entry, str) and entry.strip() == source
                                )
                            ]
                    except Exception:
                        remaining = []

                # No other sources remain → drop the single flag too, so a
                # stale "Detected" badge cannot outlive its only evidence.
                if not remaining:
                    session.execute(
                        text("""
                            UPDATE tracks
                            SET single_sources = '',
                                single_detection_last_updated = NULL,
                                is_single = FALSE,
                                single_confidence = 'low',
                                single_confidence_score = 0.0
                            WHERE CAST(id AS TEXT) = :id
                        """),
                        {"id": track_id},
                    )
                else:
                    session.execute(
                        text("""
                            UPDATE tracks
                            SET single_sources = :sources_json,
                                single_detection_last_updated = NULL
                            WHERE CAST(id AS TEXT) = :id
                        """),
                        {
                            "id": track_id,
                            "sources_json": _json.dumps(remaining, default=str),
                        },
                    )
                return jsonify({
                    "success": True,
                    "message": f"Source '{source}' cleared for re-scan",
                })
            else:
                session.execute(
                    text("UPDATE tracks SET single_detection_last_updated = NULL WHERE CAST(id AS TEXT) = :id"),
                    {"id": track_id},
                )
                return jsonify({"success": True, "message": "Single detection cleared for re-scan"})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# POST /api/track/<track_id>/apply-mb-release
# ---------------------------------------------------------------------------

@track_bp.route("/<track_id>/apply-mb-release", methods=["POST"])
async def api_track_apply_mb_release(track_id):
    """Apply a chosen MusicBrainz release MBID to a track."""
    try:
        data = (await request.get_json()) or {}
        release_mbid = str(data.get("release_mbid") or "").strip()
        if not release_mbid:
            return jsonify({"error": "release_mbid required"}), 400
        with db_session() as session:
            session.execute(
                text("UPDATE tracks SET musicbrainz_album_mbid = :mbid WHERE CAST(id AS TEXT) = :id"),
                {"mbid": release_mbid, "id": track_id},
            )
        return jsonify({"success": True})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# POST /api/track/<track_id>/lyrics/fetch — LRCLIB lyrics lookup
# ---------------------------------------------------------------------------

@track_bp.route("/<track_id>/lyrics/fetch", methods=["POST"])
async def api_fetch_track_lyrics(track_id):
    """Fetch plain + synced lyrics from LRCLIB and store them on the track.

    LRCLIB (https://lrclib.net) needs no API key.  The matched lyrics are
    written to the track's ``lyrics`` column (the synced LRC form when
    available, otherwise the plain text) so the track page Lyrics tab renders
    instantly afterwards.  Returns ``{"found": bool, "lyrics": str|null,
    "synced": bool, "source": "lrclib"}``.
    """
    try:
        from api_clients.lrclib import fetch_lyrics

        with db_session() as session:
            row = session.execute(
                text("SELECT * FROM tracks WHERE CAST(id AS TEXT) = :id"),
                {"id": str(track_id)},
            ).fetchone()
            if not row:
                return jsonify({"error": "Track not found"}), 404
            track = dict(row._mapping)

        result = fetch_lyrics(
            track_name=str(track.get("title") or ""),
            artist_name=str(track.get("artist") or ""),
            album_name=str(track.get("album") or "") or None,
            duration=(
                float(track["duration"])
                if track.get("duration")
                else None
            ),
        )
        plain = result.get("plain") or ""
        synced = result.get("synced") or ""
        if not plain and not synced:
            return jsonify({"found": False, "lyrics": None, "synced": False, "source": "lrclib"})

        # Prefer the synced LRC form (renders a scrolling player on the page);
        # fall back to plain text.
        stored = synced or plain
        with db_session() as session:
            session.execute(
                text("UPDATE tracks SET lyrics = :lyrics WHERE CAST(id AS TEXT) = :id"),
                {"lyrics": stored, "id": str(track_id)},
            )
        return jsonify({
            "found": True,
            "lyrics": stored,
            "synced": bool(synced),
            "plain": plain,
            "synced_lrc": synced,
            "source": "lrclib",
        })
    except Exception as exc:
        logger.error("Lyrics fetch failed for %s: %s", track_id, exc)
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# POST /api/track/<track_id>/fetch-credits — MusicBrainz recording credits
# ---------------------------------------------------------------------------

@track_bp.route("/<track_id>/fetch-credits", methods=["POST"])
async def api_fetch_track_credits(track_id):
    """Fetch recording credits from MusicBrainz artist relations.

    Returns composer / lyricist / producer / engineer / conductor names so the
    track page's Credits form can be quick-filled without a full rescan.
    Requires the track to have a MusicBrainz recording MBID resolved.
    """
    try:
        with db_session() as session:
            row = session.execute(
                text("SELECT recording_mbid, musicbrainz_trackid FROM tracks WHERE CAST(id AS TEXT) = :id"),
                {"id": str(track_id)},
            ).fetchone()
            if not row:
                return jsonify({"error": "Track not found"}), 404
            rec_mbid = str(row[0] or row[1] or "").strip()

        if not rec_mbid:
            return jsonify({
                "found": False,
                "error": "Track has no MusicBrainz recording ID — run a MusicBrainz lookup first.",
            })

        from api_clients.musicbrainz_http import MusicBrainzHttpClient
        data = MusicBrainzHttpClient().get_recording(rec_mbid, inc="artist-rels") or {}
        relations = data.get("relations") or []

        credits: dict[str, list[str]] = {
            "composer": [], "lyricist": [], "producer": [],
            "engineer": [], "conductor": [],
        }
        for rel in relations:
            rtype = str(rel.get("type") or "").lower()
            if rtype not in credits:
                continue
            name = str((rel.get("artist") or {}).get("name") or "").strip()
            if name and name not in credits[rtype]:
                credits[rtype].append(name)

        found = {k: "; ".join(v) for k, v in credits.items() if v}
        return jsonify({"found": bool(found), **found})
    except Exception as exc:
        logger.error("Credits fetch failed for %s: %s", track_id, exc)
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# GET /api/track/<track_id>/mb-releases
# ---------------------------------------------------------------------------

@track_bp.route("/<track_id>/mb-releases", methods=["GET"])
def api_track_mb_releases(track_id):
    """Fetch all MusicBrainz releases containing this track's recording."""
    try:
        with db_session() as session:
            result = session.execute(text("SELECT artist, title, mbid FROM tracks WHERE CAST(id AS TEXT) = :id"), {"id": track_id})
            row = result.fetchone()
            if not row:
                return jsonify({"error": "Track not found"}), 404
            artist = row[0]
            title = row[1]
        from api_clients.musicbrainz_http import MusicBrainzHttpClient
        client = MusicBrainzHttpClient(enabled=True)
        recordings = client.search_recordings(
            f'artist:"{artist}" AND recording:"{title}"',
            limit=10,
        )
        return jsonify({"success": True, "recordings": recordings})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# POST /api/track/match-missing
# ---------------------------------------------------------------------------

@track_bp.route("/match-missing", methods=["POST"])
async def api_track_match_missing():
    """Match a MusicBrainz 'missing' track to an existing track."""
    try:
        data = (await request.get_json()) or {}
        track_id = str(data.get("track_id") or "").strip()
        mb_title = str(data.get("mb_title") or "").strip()
        if not track_id or not mb_title:
            return jsonify({"error": "track_id and mb_title required"}), 400
        with db_session() as session:
            session.execute(text("UPDATE tracks SET title = :title WHERE CAST(id AS TEXT) = :id"), {"title": mb_title, "id": track_id})
        return jsonify({"success": True, "updated_title": mb_title})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ---------------------------------------------------------------------------
# POST /api/track/ignore-mb-field
# ---------------------------------------------------------------------------

@track_bp.route("/ignore-mb-field", methods=["POST"])
async def api_track_ignore_mb_field():
    """Permanently ignore a specific MusicBrainz diff field for a track."""
    try:
        data = (await request.get_json()) or {}
        track_id = str(data.get("track_id") or "").strip()
        field = str(data.get("field") or "").strip()
        if not track_id or not field:
            return jsonify({"error": "track_id and field required"}), 400
        with db_session() as session:
            result = session.execute(text("SELECT mb_ignored_fields FROM tracks WHERE CAST(id AS TEXT) = :id"), {"id": track_id})
            row = result.fetchone()
            import json as _json
            ignored = []
            if row:
                raw = row[0]
                if raw:
                    try:
                        ignored = _json.loads(raw) if isinstance(raw, str) else list(raw) if isinstance(raw, (list, tuple)) else []
                    except (TypeError, _json.JSONDecodeError):
                        ignored = []
            if field not in ignored:
                ignored.append(field)
            session.execute(text("UPDATE tracks SET mb_ignored_fields = :fields WHERE CAST(id AS TEXT) = :id"),
                           {"fields": _json.dumps(ignored), "id": track_id})
        return jsonify({"success": True, "ignored_fields": ignored})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
