"""Shared response helpers for internal service-to-route communication.

These helpers return ``(dict, int)`` tuples — *not* Flask ``Response`` objects.
For Flask-specific ``jsonify`` wrappers see ``services.web.api_response``.
"""

from __future__ import annotations

from typing import Any


def _ok(**payload: Any) -> tuple[dict[str, Any], int]:
    """Return a success payload with HTTP 200."""
    payload.setdefault("success", True)
    return payload, 200


def _fail(message: str, status: int = 400, **extra: Any) -> tuple[dict[str, Any], int]:
    """Return an error payload with the given HTTP status."""
    payload: dict[str, Any] = {"success": False, "error": message}
    payload.update(extra)
    return payload, status


def _safe_int(value: Any, default: int = 0) -> int:
    """Convert *value* to int, returning *default* on failure."""
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default
