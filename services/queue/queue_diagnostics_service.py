"""Queue diagnostics service.

Provides monitoring and diagnostic utilities for the download queue:
    - Queue processor status and health checks.
    - Queue event logging and retrieval.
    - Queue event search and filtering.
    - Collection batch status checks.
    - slskd eligibility diagnostics.

Architecture:
    Maintains an in-memory event store (thread-safe) for recent queue
    events. Delegates persistent state queries to the queue repository.

    Event Store:
        Thread-safe list of dicts with type, message, and extra metadata.
        Used for real-time monitoring in the WebUI.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Mapping

from db.repositories.queue import (
    get_queue_status_counts,
)

from db.repositories.tracks import (
    find_library_track,
)
from helpers.response_helpers import _ok, _fail, _safe_int

logger = logging.getLogger(__name__)

_queue_events_lock = threading.Lock()
_queue_events: list[dict[str, Any]] = []


# =============================================================================
# EVENT STORE
# =============================================================================

def log_queue_event(
    event_type: str,
    message: str,
    **extra,
) -> None:
    event = {
        "type": event_type,
        "message": message,
        **extra,
    }

    with _queue_events_lock:
        _queue_events.append(event)

        if len(_queue_events) > 5000:
            del _queue_events[:-5000]


def clear_queue_events() -> None:
    with _queue_events_lock:
        _queue_events.clear()


def get_queue_events(
    limit: int = 50,
    event_type: str | None = None,
) -> list[dict[str, Any]]:

    with _queue_events_lock:

        events = list(
            reversed(_queue_events)
        )

        if event_type:
            events = [
                event
                for event in events
                if event.get("type") == event_type
            ]

        return events[:limit]


# =============================================================================
# PROCESSOR
# =============================================================================

def queue_processor_status():
    """
    Placeholder until queue processor state
    is fully centralized.
    """

    return _ok(
        running=False,
        migrated=False,
        message="Queue processor status not implemented",
    )


# =============================================================================
# EVENTS
# =============================================================================

def queue_events(
    args: Mapping[str, Any],
):
    try:
        limit = _safe_int(
            args.get("limit"),
            100,
        )

        event_type = (
            str(args.get("type"))
            if args.get("type")
            else None
        )

        events = get_queue_events(
            limit=limit,
            event_type=event_type,
        )

        return _ok(
            events=events,
            total=len(events),
        )

    except Exception as exc:
        logger.exception(
            "queue_events failed"
        )

        return _fail(
            str(exc),
            500,
        )


def queue_search_events(
    args: Mapping[str, Any],
):
    try:
        query = str(
            args.get("q")
            or args.get("query")
            or ""
        ).strip().lower()

        limit = _safe_int(
            args.get("limit"),
            100,
        )

        events = get_queue_events(
            limit=5000,
        )

        matches = [
            event
            for event in events
            if query in str(event).lower()
        ]

        return _ok(
            events=matches[:limit],
            total=len(matches),
            query=query,
        )

    except Exception as exc:
        logger.exception(
            "queue_search_events failed"
        )

        return _fail(
            str(exc),
            500,
        )


# =============================================================================
# COLLECTION DIAGNOSTICS
# =============================================================================

def queue_check_collection_batch(
    data: Mapping[str, Any],
):
    try:

        items = data.get("items") or []

        if not isinstance(items, list):
            return _fail(
                "items must be a list",
                400,
            )

        results = []

        for item in items:

            artist = str(
                item.get("artist") or ""
            ).strip()

            title = str(
                item.get("title") or ""
            ).strip()

            album = item.get("album")

            if not artist or not title:
                results.append(
                    {
                        "item": item,
                        "found": False,
                        "error": (
                            "artist and title are required"
                        ),
                    }
                )
                continue

            track = find_library_track(
                artist=artist,
                title=title,
                album=album,
            )

            results.append(
                {
                    "item": item,
                    "found": track is not None,
                    "collection_path": (
                        track.get("file_path")
                        if track
                        else None
                    ),
                }
            )

        return _ok(
            results=results,
            total=len(results),
        )

    except Exception as exc:
        logger.exception(
            "queue_check_collection_batch failed"
        )

        return _fail(
            str(exc),
            500,
        )


# =============================================================================
# SLSKD DIAGNOSTICS
# =============================================================================

def queue_slskd_eligibility_diagnostics():
    try:
        return _ok(
            migrated=False,
            counts=get_queue_status_counts(),
            message=(
                "slskd eligibility diagnostics "
                "not implemented"
            ),
        )

    except Exception as exc:
        logger.exception(
            "queue_slskd_eligibility_diagnostics failed"
        )

        return _fail(
            str(exc),
            500,
        )