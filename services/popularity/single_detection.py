"""Legacy compatibility wrapper for single detection.

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
    track_info = track_info or {}
    
    # Fallback to pure score-based detection if no core metadata is provided
    if not track_info.get("title") and not track_info.get("artist"):
        popularity = float(score_data.get("final_score", 0) or score_data.get("popularity", 0) or 0.0)
        if popularity > 85:
            return {"is_single": True, "single_confidence": "high", "confidence": "high"}
        if popularity > 70:
            return {"is_single": True, "single_confidence": "medium", "confidence": "medium"}
        return {"is_single": False, "single_confidence": "low", "confidence": "low"}

    # Route to the modern enrichment service with full context forwarded
    api_result = _detect_single_for_track(
        title=str(track_info.get("title", "")),
        artist=str(track_info.get("artist", "")),
        album=str(track_info.get("album", "")),
        isrc=track_info.get("isrc") or score_data.get("isrc"),
        recording_mbid=track_info.get("recording_mbid") or track_info.get("mbid") or track_info.get("musicbrainz_trackid"),
        listenbrainz_listens=int(track_info.get("listenbrainz_listens") or score_data.get("listenbrainz_listens") or 0),
        lastfm_listeners=int(track_info.get("lastfm_listeners") or score_data.get("lastfm_listeners") or 0),
        duration=float(track_info.get("duration") or 0) or None,
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
