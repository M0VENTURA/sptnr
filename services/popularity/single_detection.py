"""
services/popularity/single_detection.py

Handles:
- Single detection logic
- Z-score thresholds
- API-backed checks
"""

from services.enrichment.single_detection_service import (
    detect_single_for_track,
)


def detect_single(score_data: dict, track_info: dict | None = None) -> dict:
    """
    Determine if a track is a single using multiple signals.

    Args:
        score_data: A dict containing scoring info, including 'final_score'.
        track_info: Optional dict with track metadata (artist, title, album, etc.)

    Returns:
        A dict with 'is_single' (bool) and 'single_confidence' (float).
    """
    popularity = score_data.get("final_score", 0)
    
    # Default values
    is_single = False
    confidence = 0.0
    
    # Priority 1: Use external API detection if track info available
    if track_info:
        artist = track_info.get("artist", "")
        title = track_info.get("title", "")
        album = track_info.get("album", "")
        
        if artist and title:
            api_result = detect_single_for_track(
                title=title,
                artist=artist,
                album=album,
                use_advanced_detection=True,
                persist_result=False  # Don't persist during scan
            )
            if api_result:
                is_single = api_result.get("is_single", False)
                confidence = api_result.get("confidence", 0.0)
                return {
                    "is_single": is_single,
                    "single_confidence": float(confidence)
                }
    
    # Priority 2: Fallback to popularity-based heuristic
    # High popularity (>85) + no album context suggests single
    # Medium popularity (70-85) requires additional signals
    if popularity > 85:
        is_single = True
        confidence = min(1.0, popularity / 100.0)
    elif popularity > 70:
        # Moderate confidence - mark as potential single
        is_single = True
        confidence = 0.6 * (popularity / 100.0)
    
    return {
        "is_single": is_single,
        "single_confidence": float(confidence)
    }