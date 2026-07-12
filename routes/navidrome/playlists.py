"""Navidrome playlist routes."""

from __future__ import annotations

import logging

from quart import jsonify

from routes.navidrome import get_navidrome_client, navidrome_bp


@navidrome_bp.route("/api/navidrome/playlists", methods=["GET"])
def api_navidrome_playlists():
    """Return Navidrome playlists grouped by smart/regular type."""
    client = get_navidrome_client()
    if not client:
        return jsonify({"error": "Navidrome not configured"}), 400

    try:
        playlists = client.fetch_all_playlists()
        return jsonify({
            "smart": [{"id": item.get("id"), "name": item.get("name")} for item in playlists if item.get("type") == "smart"],
            "regular": [{"id": item.get("id"), "name": item.get("name")} for item in playlists if item.get("type") != "smart"],
        })
    except Exception as exc:
        logging.error("Failed to fetch Navidrome playlists: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500


@navidrome_bp.route("/api/navidrome/playlist/<playlist_id>", methods=["GET"])
def api_navidrome_playlist_detail(playlist_id):
    """Return one Navidrome playlist with track details."""
    client = get_navidrome_client()
    if not client:
        return jsonify({"error": "Navidrome not configured"}), 400

    playlist = client.fetch_playlist(playlist_id)
    if not playlist:
        return jsonify({"error": f"Playlist {playlist_id} not found"}), 404
    playlist["navidromeUrl"] = client.base_url
    return jsonify(playlist)
