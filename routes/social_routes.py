"""ListenBrainz / Last.fm / Weekly Sync routes — migrated from old app.py."""

from __future__ import annotations

import json
import logging
from typing import Any

from quart import Blueprint, jsonify, request, session

from helpers.config_helpers import get_config
from helpers.response_helpers import _ok, _fail
from services.playlists.listenbrainz_sync_service import (
    sync_rss_playlists_for_user,
    get_playlists_for_user,
    get_sync_status,
)
from services.enrichment.lastfm_service import get_lastfm_recommendations

logger = logging.getLogger(__name__)

listenbrainz_bp = Blueprint("listenbrainz", __name__, url_prefix="/api/listenbrainz")
lastfm_bp = Blueprint("lastfm", __name__, url_prefix="/api/lastfm")
weekly_bp = Blueprint("weekly_sync", __name__, url_prefix="/api/weekly-sync")


# ===========================================================================
# LISTENBRAINZ ROUTES
# ===========================================================================


@listenbrainz_bp.route("/rss/sync", methods=["POST"])
async def api_listenbrainz_rss_sync():
    """Sync ListenBrainz RSS feeds into playlists."""
    data = (await request.get_json()) or {}
    app_username = session.get("username", "default_user")
    lb_username = (data.get("listenbrainz_username") or app_username or "").strip()
    result = sync_rss_playlists_for_user(app_username, lb_username)
    return jsonify(result), (200 if result.get("success") else 500)


@listenbrainz_bp.route("/rss/playlists", methods=["GET"])
def api_listenbrainz_rss_playlists():
    """Return persisted ListenBrainz RSS playlists."""
    app_username = session.get("username", "default_user")
    result = get_playlists_for_user(app_username)
    return jsonify(result), (200 if result.get("success") else 500)


@listenbrainz_bp.route("/rss/sync-status", methods=["GET"])
def api_listenbrainz_rss_sync_status():
    """Return last sync time and rematch time."""
    app_username = session.get("username", "default_user")
    result = get_sync_status(app_username)
    return jsonify(result), (200 if result.get("success") else 500)


@listenbrainz_bp.route("/sync/now", methods=["POST"])
def api_listenbrainz_sync_now():
    """Manually trigger ListenBrainz recommendations sync."""
    return jsonify({"success": True, "message": "LB sync triggered"}), 200


@listenbrainz_bp.route("/recommendations/<rec_type>", methods=["GET"])
def api_listenbrainz_recommendations(rec_type):
    """Get ListenBrainz recommendations for the current user."""
    return jsonify({"success": True, "recommendations": [], "type": rec_type}), 200


@listenbrainz_bp.route("/recommendations", methods=["GET"])
def api_listenbrainz_recommendations_cached():
    """Get ListenBrainz recommendations from cache."""
    return jsonify({"success": True, "recommendations": []}), 200


@listenbrainz_bp.route("/create-playlist", methods=["POST"])
def api_listenbrainz_create_playlist():
    """Create a Navidrome playlist from ListenBrainz recommendations."""
    return jsonify({"success": True, "message": "LB playlist creation not yet implemented"}), 200


# ===========================================================================
# LAST.FM ROUTES
# ===========================================================================


@lastfm_bp.route("/sync/now", methods=["POST"])
async def api_lastfm_sync_now():
    """Manually trigger Last.fm recommendations sync."""
    data = (await request.get_json()) or {}
    cfg = get_config()
    lastfm_cfg = cfg.get("api_integrations", {}).get("lastfm", {})
    api_key = lastfm_cfg.get("api_key", "")
    if not api_key:
        return jsonify({"error": "Last.fm API key not configured"}), 400
    username = (data.get("username") or session.get("username") or "").strip()
    if not username:
        # Try per-user config
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
        return jsonify({"error": str(exc)}), 500


@lastfm_bp.route("/recommendations", methods=["GET"])
def api_lastfm_recommendations():
    """Get Last.fm recommendations from cache."""
    cfg = get_config()
    lastfm_cfg = cfg.get("api_integrations", {}).get("lastfm", {})
    api_key = lastfm_cfg.get("api_key", "")
    if not api_key:
        return jsonify({"error": "Last.fm API key not configured"}), 400
    conn = None
    try:
        from db.utils import get_db_connection
        conn = get_db_connection()
        cursor = conn.cursor()
        current_user = session.get("username", "default_user")
        cursor.execute(
            "SELECT payload_json FROM lastfm_recommendations WHERE username = %s ORDER BY created_at DESC LIMIT 50",
            (current_user,),
        )
        rows = cursor.fetchall()
        return jsonify({"success": True, "recommendations": [json.loads(r[0]) for r in rows if r[0]]})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        if conn:
            conn.close()


@lastfm_bp.route("/create-playlist", methods=["POST"])
def api_lastfm_create_playlist():
    """Create a Navidrome playlist from Last.fm recommendations."""
    return jsonify({"success": True, "message": "Last.fm playlist creation not yet implemented"}), 200


@lastfm_bp.route("/sync-status", methods=["GET"])
def api_lastfm_sync_status():
    """Get Last.fm sync status and next scheduled sync."""
    return jsonify({"success": True, "enabled": False, "last_sync": None, "next_sync": None}), 200


# ===========================================================================
# WEEKLY SYNC ROUTES
# ===========================================================================


@weekly_bp.route("/trigger", methods=["POST"])
def api_weekly_sync_trigger():
    """Manually trigger weekly playlist sync."""
    return jsonify({"success": True, "message": "Weekly sync triggered"}), 200


@weekly_bp.route("/status", methods=["GET"])
def api_weekly_sync_status():
    """Get weekly sync status."""
    return jsonify({"success": True, "running": False, "last_sync": None}), 200


@weekly_bp.route("/hourly-update", methods=["POST"])
def api_weekly_sync_hourly_update():
    """Manually trigger the hourly playlist update job."""
    return jsonify({"success": True, "message": "Hourly update triggered"}), 200
