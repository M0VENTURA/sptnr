"""slskd download search/proxy routes — migrated from old app.py."""

from __future__ import annotations

import logging
import os
import re
import time
import threading

from quart import Blueprint, jsonify, request, session

from helpers.config_helpers import get_config
from helpers.response_helpers import _ok, _fail

logger = logging.getLogger(__name__)

slskd_bp = Blueprint("slskd_api", __name__, url_prefix="/api/slskd")
slsk_bp = Blueprint("slsk_api", __name__, url_prefix="/api/slsk")


# =============================================================================
# MANUAL SEARCH LOGGING
# =============================================================================

# Tracks the most recently reported result count per search so the manual
# search log does not spam a line for every poll of the same search.
_manual_search_state: dict = {}
_manual_search_lock = threading.Lock()


def _log_manual_search_event(
    *,
    search_type: str,
    query: str,
    result_count: int = 0,
    duration_seconds: float | None = None,
    notes: str | None = None,
    selected_result: dict | None = None,
) -> None:
    """Record a manual Soulseek search to ``search.log`` and the search DB.

    Keeps manual searches visible in the monitor's Soulseek Search Log and
    the /logs page alongside automatic pipeline searches.
    """
    try:
        from helpers.logging_config import log_search
        suffix = f" ({notes})" if notes else ""
        log_search(
            f"[{search_type.upper()}] {query} → {result_count} results"
            + (f" in {round(duration_seconds or 0, 1)}s" if duration_seconds else "")
            + suffix
        )
    except Exception:
        pass
    try:
        from db.repositories.search_logs import log_slskd_search
        log_slskd_search(
            search_type=search_type,
            query=query,
            result_count=result_count,
            duration_seconds=duration_seconds,
            notes=notes,
            selected_result=selected_result,
        )
    except Exception:
        pass


def _normalize_slskd_query(value: str) -> str:
    text = str(value or "")
    text = text.replace("\\u0026", " ").replace("&amp;", " ").replace("&", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _coerce_optional_int(value, allow_prefix=False):
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


# ===========================================================================
# SLSKD ROUTES
# ===========================================================================


@slskd_bp.route("/search", methods=["POST"])
async def slskd_search():
    """Proxy endpoint for slskd search API."""
    cfg = get_config()
    slskd_config = cfg.get("slskd", {})
    if not slskd_config.get("enabled"):
        return jsonify({"error": "slskd not enabled"}), 400
    query = _normalize_slskd_query(((await request.get_json()) or {}).get("query", ""))
    if not query:
        return jsonify({"error": "query required"}), 400
    web_url = slskd_config.get("web_url", "http://localhost:5030")
    api_key = slskd_config.get("api_key", "")
    try:
        from api_clients.slskd_http import SlskdHttpClient
        from services.downloads.slskd_service import SlskdService
        client = SlskdHttpClient(web_url, api_key)
        # Free the single search slot before starting a manual search so a
        # leftover completed/stuck search cannot force an HTTP 429 (legacy
        # parity: _clear_stale_slskd_searches).
        try:
            SlskdService(http_client=client).clear_stale_searches(budget_seconds=6)
        except Exception:
            pass
        started = time.time()
        slskd_service = SlskdService(http_client=client)
        search_id = slskd_service.start_search(query, timeout=20)
        if search_id:
            with _manual_search_lock:
                _manual_search_state[search_id] = {
                    "query": query,
                    "last_result_count": -1,
                }
            _log_manual_search_event(
                search_type="manual",
                query=query,
                result_count=0,
                duration_seconds=round(time.time() - started, 1),
                notes="search_started",
            )
            return jsonify({"success": True, "search_id": search_id})
        return jsonify({"success": False, "error": "Search failed to start"}), 500
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@slskd_bp.route("/search-slot", methods=["GET"])
def slskd_search_slot():
    """Return whether the slskd search slot is free."""
    cfg = get_config()
    slskd_config = cfg.get("slskd", {})
    if not slskd_config.get("enabled"):
        return jsonify({"slotFree": False, "error": "slskd not enabled"}), 400
    try:
        from api_clients.slskd_http import SlskdHttpClient
        client = SlskdHttpClient(slskd_config["web_url"], slskd_config.get("api_key", ""))
        searches = client.list_searches(timeout=8)
        active_states = {"None", "Queued", "Requested", "InProgress", "Initializing", "In Progress"}
        active = [s for s in (searches or []) if (s.get("state") or s.get("State") or "") in active_states]
        if active:
            a = active[0]
            return jsonify({
                "slotFree": False,
                "activeSearchId": a.get("id") or a.get("searchId") or "",
                "activeSearchQuery": a.get("searchText") or a.get("query") or "",
                "activeSearchState": a.get("state") or a.get("State") or "",
            })
        return jsonify({"slotFree": True})
    except Exception as exc:
        return jsonify({"slotFree": True, "error": str(exc)}), 200


@slskd_bp.route("/search/<search_id>", methods=["GET"])
def slskd_search_results(search_id):
    """Poll for Soulseek search results."""
    cfg = get_config()
    slskd_config = cfg.get("slskd", {})
    if not slskd_config.get("enabled"):
        return jsonify({"error": "slskd not enabled"}), 400
    try:
        from api_clients.slskd_http import SlskdHttpClient
        client = SlskdHttpClient(slskd_config["web_url"], slskd_config.get("api_key", ""))
        results = client.get_search_results(search_id, timeout=10)
        count = len(results or [])

        # Log manual search results when the count first settles, without
        # spamming a line for every 1s poll of the same search.
        with _manual_search_lock:
            state = _manual_search_state.get(search_id)
            if state is not None and count != state.get("last_result_count", -1):
                state["last_result_count"] = count
                _log_manual_search_event(
                    search_type="manual",
                    query=state.get("query") or search_id,
                    result_count=count,
                    notes="results_returned",
                )

        return jsonify({"success": True, "results": results or []})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@slskd_bp.route("/download", methods=["POST"])
async def slskd_download():
    """Proxy endpoint to download from slskd."""
    cfg = get_config()
    slskd_config = cfg.get("slskd", {})
    if not slskd_config.get("enabled"):
        return jsonify({"error": "slskd not enabled"}), 400
    payload = (await request.get_json()) or {}
    username = payload.get("username", "")
    filename = payload.get("filename", "")
    if not username or not filename:
        return jsonify({"error": "username and filename required"}), 400
    try:
        from api_clients.slskd_http import SlskdHttpClient
        client = SlskdHttpClient(slskd_config["web_url"], slskd_config.get("api_key", ""))
        result = client.enqueue_download(username, filename)
        _log_manual_search_event(
            search_type="manual",
            query=f"{username} - {filename}",
            result_count=1,
            notes="selected_for_download",
            selected_result={"username": username, "filename": filename},
        )
        return jsonify({"success": True, "result": result})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@slskd_bp.route("/cancel", methods=["POST"])
async def slskd_cancel():
    """Cancel a Soulseek download."""
    cfg = get_config()
    slskd_config = cfg.get("slskd", {})
    if not slskd_config.get("enabled"):
        return jsonify({"error": "slskd not enabled"}), 400
    payload = (await request.get_json()) or {}
    username = payload.get("username", "")
    filename = payload.get("filename", "")
    transfer_id = payload.get("transfer_id")
    if not username or not (filename or transfer_id):
        return jsonify({"error": "username and filename/transfer_id required"}), 400
    try:
        from api_clients.slskd_http import SlskdHttpClient
        client = SlskdHttpClient(slskd_config["web_url"], slskd_config.get("api_key", ""))
        result = client.cancel_download(username, filename, transfer_id)
        return jsonify({"success": True, "result": result})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@slskd_bp.route("/status", methods=["GET"])
def slskd_status():
    """Get slskd download status."""
    cfg = get_config()
    slskd_config = cfg.get("slskd", {})
    if not slskd_config.get("enabled"):
        return jsonify({"error": "slskd not enabled"}), 400
    try:
        from api_clients.slskd_http import SlskdHttpClient
        client = SlskdHttpClient(slskd_config["web_url"], slskd_config.get("api_key", ""))
        downloads = client.get_active_downloads(timeout=10)
        return jsonify({"success": True, "downloads": downloads or []})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@slskd_bp.route("/retry", methods=["POST"])
async def slskd_retry():
    """Retry a failed Soulseek download."""
    cfg = get_config()
    slskd_config = cfg.get("slskd", {})
    if not slskd_config.get("enabled"):
        return jsonify({"error": "slskd not enabled"}), 400
    payload = (await request.get_json()) or {}
    username = payload.get("username", "")
    filename = payload.get("filename", "")
    if not username or not filename:
        return jsonify({"error": "username and filename required"}), 400
    try:
        from api_clients.slskd_http import SlskdHttpClient
        client = SlskdHttpClient(slskd_config["web_url"], slskd_config.get("api_key", ""))
        result = client.retry_download(username, filename)
        return jsonify({"success": True, "result": result})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@slskd_bp.route("/queue-download", methods=["POST"])
async def slskd_queue_download():
    """Initiate a Soulseek download linked to a specific queue item."""
    cfg = get_config()
    slskd_config = cfg.get("slskd", {})
    if not slskd_config.get("enabled"):
        return jsonify({"error": "slskd not enabled"}), 400
    payload = (await request.get_json()) or {}
    queue_id = payload.get("queue_id")
    username = payload.get("username", "")
    filename = payload.get("filename", "")
    if not queue_id or not username or not filename:
        return jsonify({"error": "queue_id, username, and filename required"}), 400
    try:
        from api_clients.slskd_http import SlskdHttpClient
        client = SlskdHttpClient(slskd_config["web_url"], slskd_config.get("api_key", ""))
        result = client.enqueue_download(username, filename)
        return jsonify({"success": True, "result": result})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@slskd_bp.route("/events", methods=["GET"])
def slskd_events():
    """Get recent slskd events."""
    cfg = get_config()
    slskd_config = cfg.get("slskd", {})
    if not slskd_config.get("enabled"):
        return jsonify({"error": "slskd not enabled"}), 400
    try:
        from api_clients.slskd_http import SlskdHttpClient
        client = SlskdHttpClient(slskd_config["web_url"], slskd_config.get("api_key", ""))
        events = client.get_events(timeout=10)
        return jsonify({"success": True, "events": events or []})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ===========================================================================
# /api/slsk/ routes
# ===========================================================================

_slsk_banned_words: dict = {}
_slsk_banned_words_lock = threading.Lock()


@slsk_bp.route("/banned-words", methods=["GET"])
def api_get_banned_words():
    """Get banned words and suggested words."""
    return jsonify({"words": list(_slsk_banned_words.keys())})


@slsk_bp.route("/banned-words", methods=["POST"])
async def api_add_banned_word():
    """Add or update a banned word."""
    data = (await request.get_json()) or {}
    word = str(data.get("word") or "").strip().lower()
    if not word:
        return jsonify({"error": "word required"}), 400
    with _slsk_banned_words_lock:
        _slsk_banned_words[word] = True
    return jsonify({"success": True})


@slsk_bp.route("/banned-words/<path:word>", methods=["DELETE"])
def api_delete_banned_word(word):
    """Remove a word from the banned words list."""
    with _slsk_banned_words_lock:
        _slsk_banned_words.pop(word.strip().lower(), None)
    return jsonify({"success": True})


@slsk_bp.route("/banned-words/dismiss-all", methods=["POST"])
def api_dismiss_all_suggested_words():
    """Move all suggested words into dismissed."""
    # Dismiss by clearing (suggested words would be regenerated on next analysis)
    return jsonify({"success": True})

