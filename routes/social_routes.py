"""ListenBrainz / Last.fm / Weekly Sync routes — migrated from old app.py."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from quart import Blueprint, jsonify, request, session
import structlog
from sqlalchemy import text

from db.engine import db_session
from helpers.config_helpers import get_config
from services.playlists.listenbrainz_sync_service import (
    sync_rss_playlists_for_user,
    get_playlists_for_user,
    get_sync_status,
)
from services.enrichment.lastfm_service import get_lastfm_recommendations

logger = structlog.get_logger(__name__)

listenbrainz_bp = Blueprint("listenbrainz", __name__, url_prefix="/api/listenbrainz")
lastfm_bp = Blueprint("lastfm", __name__, url_prefix="/api/lastfm")
weekly_bp = Blueprint("weekly_sync", __name__, url_prefix="/api/weekly-sync")


# ===========================================================================
# LISTENBRAINZ ROUTES
# ===========================================================================

@listenbrainz_bp.route("/rss/sync", methods=["POST"])
async def api_listenbrainz_rss_sync() -> Any:
    """Sync ListenBrainz RSS feeds into playlists."""
    data = (await request.get_json()) or {}
    app_username = session.get("username", "default_user")
    lb_username = (data.get("listenbrainz_username") or app_username or "").strip()
    result = await asyncio.to_thread(sync_rss_playlists_for_user, app_username, lb_username)
    return jsonify(result), (200 if result.get("success") else 500)


@listenbrainz_bp.route("/rss/playlists", methods=["GET"])
def api_listenbrainz_rss_playlists() -> Any:
    """Return persisted ListenBrainz RSS playlists."""
    app_username = session.get("username", "default_user")
    result = get_playlists_for_user(app_username)
    return jsonify(result), (200 if result.get("success") else 500)


@listenbrainz_bp.route("/rss/sync-status", methods=["GET"])
def api_listenbrainz_rss_sync_status() -> Any:
    """Return last sync time and rematch time."""
    app_username = session.get("username", "default_user")
    result = get_sync_status(app_username)
    return jsonify(result), (200 if result.get("success") else 500)


@listenbrainz_bp.route("/sync/now", methods=["POST"])
async def api_listenbrainz_sync_now() -> Any:
    """Manually trigger ListenBrainz recommendations sync."""
    data = (await request.get_json()) or {}
    app_username = session.get("username", "default_user")
    lb_username = (data.get("listenbrainz_username") or app_username or "").strip()
    result = await asyncio.to_thread(sync_rss_playlists_for_user, app_username, lb_username)
    return jsonify(result), (200 if result.get("success") else 500)


@listenbrainz_bp.route("/recommendations/<rec_type>", methods=["GET"])
async def api_listenbrainz_recommendations(rec_type: str) -> Any:
    """Get ListenBrainz recommendations for the current user."""
    app_username = session.get("username", "default_user")
    lb_username = (request.args.get("listenbrainz_username") or app_username or "").strip()

    playlists = (get_playlists_for_user(app_username) or {}).get("playlists", {})
    tracks = playlists.get(rec_type) or []
    if not tracks:
        await asyncio.to_thread(sync_rss_playlists_for_user, app_username, lb_username)
        playlists = (get_playlists_for_user(app_username) or {}).get("playlists", {})
        tracks = playlists.get(rec_type) or []

    return jsonify({"success": True, "recommendations": tracks, "type": rec_type, "count": len(tracks)})


@listenbrainz_bp.route("/recommendations", methods=["GET"])
def api_listenbrainz_recommendations_cached() -> Any:
    """Get ListenBrainz recommendations from cache."""
    app_username = session.get("username", "default_user")
    result = get_playlists_for_user(app_username)
    return jsonify({"success": result.get("success", False), "recommendations": result.get("playlists", {})})


@listenbrainz_bp.route("/create-playlist", methods=["POST"])
async def api_listenbrainz_create_playlist() -> Any:
    """Create a Navidrome playlist from ListenBrainz recommendations."""
    data = (await request.get_json(silent=True)) or {}
    app_username = session.get("username", "default_user")

    playlist_name = (data.get("name") or "").strip()
    if playlist_name and data.get("songs"):
        track_ids = [s.get("id") for s in data.get("songs", []) if isinstance(s, dict) and s.get("id")]
        if not track_ids:
            return jsonify({"error": "No matched tracks with IDs to add"}), 400
        from services.playlists.playlist_create_service import create_playlist_file
        description = (data.get("description") or "ListenBrainz recommendations").strip()
        try:
            file_path = create_playlist_file(playlist_name, description, track_ids)
            return jsonify({
                "success": True,
                "file_path": file_path,
                "track_count": len(track_ids),
            })
        except Exception as exc:
            logger.error("Playlist creation failed", error=str(exc))
            return jsonify({"error": str(exc)}), 500

    rec_type = (data.get("type") or "weekly_jams").strip()
    lb_username = (data.get("username") or app_username or "").strip()
    await asyncio.to_thread(sync_rss_playlists_for_user, app_username, lb_username)
    playlists = (get_playlists_for_user(app_username) or {}).get("playlists", {})
    recommendations = playlists.get(rec_type) or []
    if not recommendations:
        return jsonify({"error": f"No ListenBrainz recommendations for type '{rec_type}'"}), 404

    matched, missing = _match_recommendations(recommendations, source="listenbrainz")
    return jsonify({
        "success": True,
        "total_recommendations": len(recommendations),
        "matched": len(matched),
        "missing": len(missing),
        "matched_tracks": matched[:100],
        "missing_tracks": missing[:100],
        "recommendation_type": rec_type,
    })


def _match_recommendations(
    recommendations: list[dict[str, Any]],
    source: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Match recommendation entries against the local library."""
    from db.repositories.tracks import find_library_track

    matched: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []

    for rec in recommendations:
        if not isinstance(rec, dict):
            continue
        if source == "listenbrainz":
            title = str(rec.get("track_name") or rec.get("title") or "").strip()
            artist = str(rec.get("artist_name") or rec.get("artist") or "").strip()
            mbid = str(rec.get("recording_mbid") or "").strip()
            if not title and not artist:
                continue
            row = _match_lb_track(artist, title, mbid)
            if row:
                matched.append({
                    "id": row.get("id"),
                    "artist": row.get("artist") or artist,
                    "title": row.get("title") or title,
                })
            else:
                missing.append({"artist": artist, "title": title, "mbid": mbid or None})
            continue

        title = str(rec.get("name") or rec.get("title") or "").strip()
        artist = str(rec.get("artist") or "").strip()
        playcount = rec.get("playcount", 0)
        if not artist:
            row = find_library_track(artist=title, title="", strict_album=False)
            if row:
                matched.append({
                    "id": row.get("id"),
                    "artist": row.get("artist") or title,
                    "title": row.get("title") or "(multiple tracks)",
                })
            else:
                missing.append({"artist": title, "title": "(multiple tracks)", "playcount": playcount})
            continue
        if not title:
            continue
        row = find_library_track(artist=artist, title=title, strict_album=False)
        if row:
            matched.append({
                "id": row.get("id"),
                "artist": row.get("artist") or artist,
                "title": row.get("title") or title,
            })
        else:
            missing.append({"artist": artist, "title": title, "playcount": playcount})

    return matched, missing


def _match_lb_track(artist: str, title: str, recording_mbid: str = "") -> dict[str, Any] | None:
    """Match a ListenBrainz track against the library."""
    if recording_mbid:
        try:
            with db_session() as session_db:
                row = session_db.execute(
                    text("SELECT id, artist, title FROM tracks WHERE musicbrainz_id = :m LIMIT 1"),
                    {"m": recording_mbid},
                ).fetchone()
            if row:
                return {"id": row[0], "artist": row[1], "title": row[2]}
        except Exception:
            pass
    from db.repositories.tracks import find_library_track
    return find_library_track(artist=artist, title=title, strict_album=False)


# ===========================================================================
# LAST.FM ROUTES
# ===========================================================================

@lastfm_bp.route("/sync/now", methods=["POST"])
async def api_lastfm_sync_now() -> Any:
    """Manually trigger Last.fm recommendations sync."""
    data = (await request.get_json()) or {}
    cfg = get_config()
    lastfm_cfg = cfg.get("api_integrations", {}).get("lastfm", {})
    api_key = lastfm_cfg.get("api_key", "")
    if not api_key:
        return jsonify({"error": "Last.fm API key not configured"}), 400
    username = (data.get("username") or session.get("username") or "").strip()
    if not username:
        nav_users = cfg.get("navidrome_users", [])
        for u in nav_users:
            if u.get("user") == session.get("username"):
                username = u.get("lastfm_username") or u.get("user") or ""
                break
    if not username:
        return jsonify({"error": "username required"}), 400
    try:
        recommendations = get_lastfm_recommendations(api_key, username=username)
        return jsonify({"success": True, "recommendations": recommendations})
    except Exception as exc:
        logger.error("Last.fm sync failed", error=str(exc))
        return jsonify({"error": str(exc)}), 500


@lastfm_bp.route("/recommendations", methods=["GET"])
def api_lastfm_recommendations() -> Any:
    """Get Last.fm recommendations from cache."""
    cfg = get_config()
    lastfm_cfg = cfg.get("api_integrations", {}).get("lastfm", {})
    api_key = lastfm_cfg.get("api_key", "")
    if not api_key:
        return jsonify({"error": "Last.fm API key not configured"}), 400
    try:
        current_user = session.get("username", "default_user")
        with db_session() as session_db:
            result = session_db.execute(
                text("SELECT payload_json FROM lastfm_recommendations WHERE username = :user ORDER BY created_at DESC LIMIT 50"),
                {"user": current_user},
            )
            rows = result.fetchall()
        return jsonify({"success": True, "recommendations": [json.loads(r[0]) for r in rows if r[0]]})
    except Exception as exc:
        logger.error("Failed to fetch Last.fm recommendations", error=str(exc))
        return jsonify({"error": str(exc)}), 500


@lastfm_bp.route("/create-playlist", methods=["POST"])
async def api_lastfm_create_playlist() -> Any:
    """Create a Navidrome playlist from Last.fm recommendations."""
    data = (await request.get_json(silent=True)) or {}

    playlist_name = (data.get("name") or "").strip()
    if playlist_name and data.get("songs"):
        track_ids = [s.get("id") for s in data.get("songs", []) if isinstance(s, dict) and s.get("id")]
        if not track_ids:
            return jsonify({"error": "No matched tracks with IDs to add"}), 400
        from services.playlists.playlist_create_service import create_playlist_file
        description = (data.get("description") or "Last.fm recommendations").strip()
        try:
            file_path = create_playlist_file(playlist_name, description, track_ids)
            return jsonify({
                "success": True,
                "file_path": file_path,
                "track_count": len(track_ids),
            })
        except Exception as exc:
            logger.error("Last.fm playlist creation failed", error=str(exc))
            return jsonify({"error": str(exc)}), 500

    cfg = get_config()
    lastfm_cfg = cfg.get("api_integrations", {}).get("lastfm", {})
    api_key = lastfm_cfg.get("api_key", "")
    if not api_key:
        return jsonify({"error": "Last.fm API key not configured"}), 400

    rec_type = (data.get("type") or "tracks").strip()
    current_user = session.get("username", "")
    username = (data.get("username") or "").strip()
    if not username and current_user:
        nav_users = cfg.get("navidrome_users", [])
        for u in nav_users:
            if u.get("user") == current_user:
                username = u.get("lastfm_username") or u.get("user") or ""
                break
    if not username:
        return jsonify({"error": "username required"}), 400

    try:
        recommendations = get_lastfm_recommendations(api_key, username=username)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    rec_list = (recommendations or {}).get(rec_type, []) or []
    if not rec_list:
        return jsonify({"error": f"No {rec_type} recommendations found"}), 404

    matched, missing = _match_recommendations(rec_list, source="lastfm")
    return jsonify({
        "success": True,
        "total_recommendations": len(rec_list),
        "matched": len(matched),
        "missing": len(missing),
        "matched_tracks": matched[:100],
        "missing_tracks": missing[:100],
        "recommendation_type": rec_type,
    })


@lastfm_bp.route("/sync-status", methods=["GET"])
def api_lastfm_sync_status() -> Any:
    """Get Last.fm sync status and next scheduled sync."""
    cfg = get_config()
    lastfm_cfg = cfg.get("api_integrations", {}).get("lastfm", {})
    api_key = lastfm_cfg.get("api_key", "")
    enabled = bool(lastfm_cfg.get("enabled")) and bool(api_key)
    last_sync = None
    try:
        current_user = session.get("username", "default_user")
        with db_session() as session_db:
            row = session_db.execute(
                text("SELECT MAX(created_at) FROM lastfm_recommendations WHERE username = :user"),
                {"user": current_user},
            ).fetchone()
            if row and row[0]:
                last_sync = row[0]
    except Exception as exc:
        logger.debug("Last.fm sync-status lookup failed", error=str(exc))
    return jsonify({"success": True, "enabled": enabled, "last_sync": last_sync, "next_sync": None})


# ===========================================================================
# WEEKLY SYNC ROUTES
# ===========================================================================

@weekly_bp.route("/trigger", methods=["POST"])
async def api_weekly_sync_trigger() -> Any:
    """Manually trigger weekly playlist sync."""
    data = (await request.get_json(silent=True)) or {}
    app_username = session.get("username", "default_user")
    lb_username = (data.get("username") or app_username or "").strip()
    result = await asyncio.to_thread(sync_rss_playlists_for_user, app_username, lb_username)
    return jsonify(result), (200 if result.get("success") else 500)


@weekly_bp.route("/status", methods=["GET"])
def api_weekly_sync_status() -> Any:
    """Get weekly sync status."""
    app_username = session.get("username", "default_user")
    result = get_sync_status(app_username)
    return jsonify({"success": result.get("success", False), "running": False, "last_sync": result.get("last_synced_at")})


@weekly_bp.route("/hourly-update", methods=["POST"])
async def api_weekly_sync_hourly_update() -> Any:
    """Manually trigger the hourly playlist update job."""
    app_username = session.get("username", "default_user")
    result = await asyncio.to_thread(sync_rss_playlists_for_user, app_username, app_username)
    return jsonify(result), (200 if result.get("success") else 500)
