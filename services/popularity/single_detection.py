"""
services/popularity/single_detection.py

Legacy compatibility wrapper.

Canonical single detection lives in
``services.enrichment.single_detection_service.detect_single_for_track``
and is called directly from ``track_stage.py``.
"""

from __future__ import annotations

from typing import Any

from services.enrichment.single_detection_service import (
    detect_single_for_track as _detect_single_for_track,
)


def detect_single(
    score_data: dict[str, Any],
    track_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Backward-compatible single detection wrapper."""
    if not track_info:
        popularity = float(score_data.get("final_score", 0) or 0)
        if popularity > 85:
            return {"is_single": True, "single_confidence": min(1.0, popularity / 100.0)}
        if popularity > 70:
            return {"is_single": True, "single_confidence": 0.6 * (popularity / 100.0)}
        return {"is_single": False, "single_confidence": 0.0}

    api_result = _detect_single_for_track(
        title=track_info.get("title", ""),
        artist=track_info.get("artist", ""),
        album=track_info.get("album", ""),
        use_advanced_detection=True,
        persist_result=False,
    )

    if api_result:
        return {
            "is_single": bool(api_result.get("is_single", False)),
            "single_confidence": float(api_result.get("confidence", 0.0)),
        }

    return {"is_single": False, "single_confidence": 0.0}