"""
Legacy compatibility wrapper for single detection.

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
    
    # Fallback to pure score-based detection if no track metadata is provided
    if not track_info:
        popularity = float(score_data.get("final_score", 0) or 0.0)
        if popularity > 85:
            return {"is_single": True, "single_confidence": "high", "confidence": "high"}
        if popularity > 70:
            return {"is_single": True, "single_confidence": "medium", "confidence": "medium"}
        return {"is_single": False, "single_confidence": "low", "confidence": "low"}

    # Route to the modern enrichment service
    api_result = _detect_single_for_track(
        title=str(track_info.get("title", "")),
        artist=str(track_info.get("artist", "")),
        album=str(track_info.get("album", "")),
        use_advanced_detection=True,
        persist_result=False,
    )

    if api_result:
        confidence = str(api_result.get("confidence", "low"))
        return {
            "is_single": bool(api_result.get("is_single", False)),
            "single_confidence": confidence,
            "confidence": confidence,
        }

    return {"is_single": False, "single_confidence": "low", "confidence": "low"}
