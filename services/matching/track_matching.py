"""
Shared track matching utilities.

✅ PURE MATCHING LAYER
✅ All normalization delegated to title_normalization_service
"""

from __future__ import annotations

from typing import Any

import structlog

from helpers.config_helpers import get_matching_thresholds
from helpers.normalization_service import (
    normalize_artist,
    normalize_album,
    normalize_string,
    normalize_title_for_lookup,
)

try:
    from rapidfuzz import fuzz as _fuzz  # type: ignore[import-untyped]
    _HAVE_RAPIDFUZZ = True
except ImportError:
    _fuzz: Any = None  # type: ignore[no-redef]
    _HAVE_RAPIDFUZZ = False

logger = structlog.get_logger(__name__)

_matching_cfg = get_matching_thresholds()
FUZZY_THRESHOLD = _matching_cfg["fuzzy_threshold"]
MAX_FUZZY_SCORE = 0.95


# =============================================================================
# ✅ SIMILARITY
# =============================================================================

def calculate_similarity(s1: str, s2: str) -> float:
    if not s1 or not s2:
        return 0.0
    if s1 == s2:
        return 1.0
    if _HAVE_RAPIDFUZZ:
        return _fuzz.ratio(s1, s2) / 100.0
    return 0.0


# =============================================================================
# ✅ TRACK MATCHING
# =============================================================================

def calculate_track_similarity(
    track1: dict[str, Any],
    track2: dict[str, Any],
) -> tuple[float, dict[str, float]]:

    raw_title1 = track1.get("title", "")
    raw_title2 = track2.get("title", "")

    norm_title1 = normalize_title_for_lookup(raw_title1)
    norm_title2 = normalize_title_for_lookup(raw_title2)

    title_sim = calculate_similarity(norm_title1, norm_title2)

    if title_sim < 1.0:
        title_sim = max(
            title_sim,
            calculate_similarity(
                normalize_title_for_lookup(raw_title1),
                normalize_title_for_lookup(raw_title2),
            ),
        )

    artist_sim = calculate_similarity(
        normalize_artist(track1.get("artist", "")),
        normalize_artist(track2.get("artist", "")),
    )

    album_sim = calculate_similarity(
        normalize_album(track1.get("album", "")),
        normalize_album(track2.get("album", "")),
    )

    duration_sim = 1.0  # unchanged logic placeholder

    score = (
        title_sim * 0.35 +
        artist_sim * 0.25 +
        duration_sim * 0.25 +
        album_sim * 0.15
    )

    components = {
        "title": round(title_sim, 3),
        "artist": round(artist_sim, 3),
        "album": round(album_sim, 3),
        "duration": round(duration_sim, 3),
    }

    return round(score, 3), components


def is_strict_match(track1: dict[str, Any], track2: dict[str, Any]) -> bool:
    return (
        normalize_title_for_lookup(track1.get("title", "")) ==
        normalize_title_for_lookup(track2.get("title", ""))
        and
        normalize_artist(track1.get("artist", "")) ==
        normalize_artist(track2.get("artist", ""))
    )


def is_fuzzy_match(track1: dict[str, Any], track2: dict[str, Any]) -> tuple[bool, float]:
    score, _ = calculate_track_similarity(track1, track2)
    return score >= FUZZY_THRESHOLD, score
