"""Navidrome ratings routes."""

from __future__ import annotations

import logging

from quart import jsonify, session

from sqlalchemy import text
from db.engine import db_session
from routes.navidrome import get_navidrome_client, navidrome_bp


@navidrome_bp.route("/api/navidrome/ratings/sync-now", methods=["POST"])
def api_navidrome_sync_ratings_now():
    """Push local star ratings to the configured Navidrome user."""
    if "username" not in session:
        return jsonify({"success": False, "error": "Authentication required"}), 401

    client = get_navidrome_client()
    if not client:
        return jsonify({"success": False, "error": "Navidrome not configured"}), 400

    try:
        with db_session() as session:
            result = session.execute(text("SELECT id, stars FROM tracks WHERE stars IS NOT NULL AND stars > 0"))
            rows = result.fetchall() or []

        synced = 0
        failed = 0

        for row in rows:
            track_id = str(row[0])
            stars = int(row[1]) if row[1] else 0
            if not track_id:
                continue
            try:
                rating = max(1, min(int(stars), 5))
                if client.set_rating(str(track_id), rating):
                    synced += 1
                else:
                    failed += 1
            except Exception:
                failed += 1

        return jsonify({
            "success": synced > 0,
            "tracks_total": len(rows),
            "synced_total": synced,
            "failed_total": failed,
        })
    except Exception as exc:
        logging.error("Rating sync failed: %s", exc, exc_info=True)
        return jsonify({"success": False, "error": str(exc)}), 500
