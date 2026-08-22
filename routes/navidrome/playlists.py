"""Navidrome playlist routes."""

from __future__ import annotations

from typing import Any

import structlog
from quart import jsonify

from routes.navidrome import get_navidrome_client, navidrome_bp

logger = structlog.get_logger(__name__)


@navidrome_bp.route("/api/navidrome/playlists", methods=["GET"])
def api_navidrome_playlists() -> Any:
    """Return Navidrome playlists grouped by smart/regular type."""
    client = get_navidrome_client()
    if not client:
        return jsonify({"error": "Navidrome not configured"}), 400

    try:
        playlists = client.fetch_all_playlists()
        return jsonify({
            "smart": [
                {"id": item.get("id"), "name": item.get("name")} 
                for item in playlists if item.get("type") == "smart"
            ],
            "regular": [
                {"id": item.get("id"), "name": item.get("name")} 
                for item in playlists if item.get("type") != "smart"
            ],
        })
    except Exception as exc:
        logger.error("Failed to fetch Navidrome playlists", error=str(exc), exc_info=True)
        return jsonify({"error": str(exc)}), 500


@navidrome_bp.route("/api/navidrome/playlist/<playlist_id>", methods=["GET"])
def api_navidrome_playlist_detail(playlist_id: str) -> Any:
    """Return one Navidrome playlist with track details."""
    client = get_navidrome_client()
    if not client:
        return jsonify({"error": "Navidrome not configured"}), 400

    try:
        playlist = client.fetch_playlist(playlist_id)
        if not playlist:
            return jsonify({"error": f"Playlist {playlist_id} not found"}), 404
            
        playlist["navidromeUrl"] = client.base_url
        return jsonify(playlist)
    except Exception as exc:
        logger.error("Failed to fetch Navidrome playlist detail", playlist_id=playlist_id, error=str(exc), exc_info=True)
        return jsonify({"error": str(exc)}), 500
