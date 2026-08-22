"""Navidrome ratings routes.

Pushes local star ratings to the configured Navidrome user.

Architecture:
    The sync operation is dispatched to a background thread so the HTTP
    handler returns immediately — never blocking the Quart async event
    loop with synchronous ``requests``-based API calls.
"""

from __future__ import annotations

import threading
from typing import Any

import structlog
from quart import jsonify, session

from db.repositories.tracks import get_all_ratings
from routes.navidrome import get_navidrome_client, navidrome_bp

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------

def _sync_ratings_worker(client: Any, username: str) -> None:
    """Run the rating sync loop in a background thread.

    Iterates over locally-starred tracks and pushes each rating to
    Navidrome via its Subsonic API.  All ``client.set_rating()`` calls
    are synchronous HTTP — running them here keeps them off the event loop.
    """
    try:
        rows = get_all_ratings()
        if not rows:
            logger.info("Rating sync skipped: no rated tracks found", username=username)
            return

        synced = 0
        failed = 0

        for row in rows:
            track_id = str(row.get("id") or "")
            stars = int(row.get("stars") or 0)
            if not track_id or stars < 1:
                continue
            try:
                rating = max(1, min(stars, 5))
                if client.set_rating(track_id, rating):
                    synced += 1
                else:
                    failed += 1
            except Exception:
                failed += 1

        logger.info(
            "Rating sync complete",
            username=username,
            synced=synced,
            failed=failed,
            total=len(rows),
        )
    except Exception as exc:
        logger.error("Rating sync failed", username=username, error=str(exc), exc_info=True)


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

@navidrome_bp.route("/api/navidrome/ratings/sync-now", methods=["POST"])
def api_navidrome_sync_ratings_now() -> Any:
    """Push local star ratings to the configured Navidrome user.

    Dispatches the work to a background thread and returns ``202 Accepted``
    immediately so the Quart event loop is never blocked by synchronous
    HTTP calls to Navidrome's Subsonic API.
    """
    if "username" not in session:
        return jsonify({"success": False, "error": "Authentication required"}), 401

    client = get_navidrome_client()
    if not client:
        return jsonify({"success": False, "error": "Navidrome not configured"}), 400

    username: str = str(session.get("username", "unknown"))

    thread = threading.Thread(
        target=_sync_ratings_worker,
        args=(client, username),
        daemon=True,
    )
    thread.start()

    logger.info("Rating sync dispatched to background thread", username=username)

    return jsonify({
        "success": True,
        "message": "Rating sync started in background",
        "username": username,
    }), 202
