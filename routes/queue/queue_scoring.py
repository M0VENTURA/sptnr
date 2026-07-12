"""
QUEUE SCORING

✅ Contains Soulseek candidate scoring logic
✅ Uses canonical normalization_service
✅ No duplicated normalization functions
"""

from __future__ import annotations

import os
import re
import logging

from helpers.types_queue import QueueItem
from helpers.normalization_service import (
    normalize_string,
    normalize_artist,
    normalize_album,
    strip_brackets,
    strip_parentheses,
)

logger = logging.getLogger(__name__)


# =============================================================================
# ✅ TOKEN HELPERS
# =============================================================================

def _tokenize_meaningful(value: str):
    stop_words = {"the", "and", "of", "a", "an", "to", "in", "on", "for", "with"}

    value = normalize_string(value)

    return [
        t for t in value.split()
        if len(t) >= 3 and t not in stop_words
    ]


# =============================================================================
# ✅ INTERNAL NORMALIZATION PHASE
# =============================================================================

def _normalize_inputs(filename: str, queue_item: QueueItem):
    norm_filename = filename.replace("\\", "/")

    basename = os.path.basename(norm_filename)

    return {
        "norm_filename": norm_filename,
        "filename_norm": normalize_string(norm_filename),
        "basename": basename,
        "basename_norm": normalize_string(basename),
        "artist_norm": normalize_artist(queue_item.get("artist") or ""),
        "album_artist_norm": normalize_artist(queue_item.get("album_artist") or ""),
        "title_norm": normalize_string(queue_item.get("title") or ""),
        "album_norm": normalize_album(queue_item.get("album") or ""),
    }


# =============================================================================
# ✅ MAIN SCORER
# =============================================================================

def _score_soulseek_candidate(filename: str, queue_item: QueueItem, candidate_duration=None):
    """
    Core scoring engine.
    Returns float score in [0, 1]
    """

    norm = _normalize_inputs(filename, queue_item)

    norm_filename = norm["norm_filename"]
    filename_norm = norm["filename_norm"]
    basename = norm["basename"]
    basename_norm = norm["basename_norm"]

    artist_norm = norm["artist_norm"]
    album_artist_norm = norm["album_artist_norm"]
    title_norm = norm["title_norm"]
    album_norm = norm["album_norm"]

    if not artist_norm or not title_norm or not basename_norm:
        return 0.0

    # ------------------------------------------------------------------
    # Title normalisation (core)
    # ------------------------------------------------------------------

    raw_basename = _strip_prefix_and_uid(basename)

    core_basename = strip_brackets(raw_basename)
    core_title = strip_brackets(queue_item.get("title") or "")

    core_basename_norm = normalize_string(core_basename)
    core_title_norm = normalize_string(core_title)

    # ------------------------------------------------------------------
    # Token scoring
    # ------------------------------------------------------------------

    title_tokens = _tokenize_meaningful(core_title_norm)
    basename_tokens = set(_tokenize_meaningful(core_basename_norm))

    if not title_tokens:
        return 0.0

    shared = sum(1 for t in title_tokens if t in basename_tokens)
    token_ratio = shared / len(title_tokens)

    if token_ratio < 0.5:
        return 0.0

    # ------------------------------------------------------------------
    # Basic similarity
    # ------------------------------------------------------------------

    artist_sim = 1.0 if artist_norm in basename_norm else 0.0
    title_sim = 1.0 if title_norm in basename_norm else token_ratio

    score = (artist_sim * 0.4) + (title_sim * 0.6)

    # ------------------------------------------------------------------
    # Album boost
    # ------------------------------------------------------------------

    if album_norm and album_norm in filename_norm:
        score += 0.2

    # ------------------------------------------------------------------
    # Orphan token penalty
    # ------------------------------------------------------------------

    explained = (
        set(_tokenize_meaningful(artist_norm))
        | set(_tokenize_meaningful(core_title_norm))
        | set(_tokenize_meaningful(album_norm or ""))
    )

    full_tokens = set(_tokenize_meaningful(basename_norm))

    orphans = [
        t for t in full_tokens
        if t not in explained
    ]

    if len(orphans) >= 2:
        score -= 0.3

    # ------------------------------------------------------------------
    # Clamp score
    # ------------------------------------------------------------------

    score = max(0.0, min(1.0, score))

    logger.debug(
        "Score: %s -> %s",
        os.path.basename(filename),
        score
    )

    return score


# =============================================================================
# ✅ UTILS (remain here, NOT normalization_service)
# =============================================================================

def _strip_prefix_and_uid(basename: str):
    return _strip_soulseek_uid_suffix(
        _strip_track_number_prefix(basename)
    )


def _strip_track_number_prefix(name: str):
    return re.sub(r"^(?:\d+-\d+|\d+)[\s.\-_]+", "", name)


def _strip_soulseek_uid_suffix(name: str):
    return re.sub(r"_\d{12,}$", "", name)