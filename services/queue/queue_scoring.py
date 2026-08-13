"""Queue scoring module.

Core Soulseek candidate scoring algorithms for matching queue items
against search results from the Soulseek network.

Key Functions:
    - _score_soulseek_candidate(): Compute a match score (0.0-1.0) between
      a queue item and a potential Soulseek search result.
    - _tokenize_meaningful(): Tokenize text for scoring, removing
      stopwords and short tokens.

Architecture:
    Uses ``helpers.normalization_service`` for all text normalization.
    No duplicated logic. Pure scoring functions with no side effects.
"""

from __future__ import annotations

import os
import re
import logging

from helpers.normalization_service import (
    normalize_string,
    normalize_artist,
    normalize_album,
    normalize_core_title,
    normalize_core_filename,
    edition_annotations_compatible,
    queue_duration_seconds,
)

logger = logging.getLogger(__name__)


# =============================================================================
# ✅ TOKENIZATION
# =============================================================================

def _tokenize_meaningful(value: str):
    """
    Tokenizer used for scoring.

    Removes:
    - punctuation (via normalize_string)
    - short tokens
    - common stopwords
    """
    stop_words = {"the", "and", "of", "a", "an", "to", "in", "on", "for", "with"}

    normalized = normalize_string(value)

    return [
        t for t in normalized.split()
        if len(t) >= 3 and t not in stop_words
    ]


# =============================================================================
# ✅ NORMALIZATION PHASE
# =============================================================================

def _normalize_inputs(filename: str, queue_item: dict):
    """
    Normalize all reusable inputs ONCE.
    """
    norm_filename = filename.replace("\\", "/")
    basename = os.path.basename(norm_filename)

    return {
        "norm_filename": norm_filename,
        "filename_norm": normalize_string(norm_filename),
        "basename": basename,
        "basename_norm": normalize_string(basename),
        "artist_norm": normalize_artist(queue_item.get("artist")),
        "album_artist_norm": normalize_artist(queue_item.get("album_artist")),
        "title_norm": normalize_string(queue_item.get("title")),
        "album_norm": normalize_album(queue_item.get("album")),
    }


# =============================================================================
# ✅ MAIN SCORER
# =============================================================================

def _score_soulseek_candidate(filename, queue_item, candidate_duration=None):

    norm = _normalize_inputs(filename, queue_item)

    # An edition-annotated track ("Valhalla (Epic Edition)") must never score
    # against the plain "Valhalla" queue item — normalize_core_title strips
    # brackets, so without this gate the wrong variant would be downloaded.
    # Strip the extension so the trailing "(Epic Edition)" annotation is still
    # extractable from the candidate path.
    queue_title = queue_item.get("title") or ""
    if queue_title and not edition_annotations_compatible(
        queue_title, os.path.splitext(filename)[0]
    ):
        return 0.0

    basename = norm["basename"]
    basename_norm = norm["basename_norm"]
    filename_norm = norm["filename_norm"]

    artist_norm = norm["artist_norm"]
    album_artist_norm = norm["album_artist_norm"]
    title_norm = norm["title_norm"]
    album_norm = norm["album_norm"]

    if not artist_norm or not title_norm:
        return 0.0

    # ------------------------------------------------------------------
    # Core normalization (strict comparison strings)
    # ------------------------------------------------------------------

    raw_basename = _strip_prefix_and_uid(basename)

    core_basename_norm = normalize_core_filename(raw_basename)
    core_title_norm = normalize_core_title(queue_item.get("title"))

    title_tokens = _tokenize_meaningful(core_title_norm)
    basename_tokens = set(_tokenize_meaningful(core_basename_norm))

    if not title_tokens:
        return 0.0

    shared = sum(1 for t in title_tokens if t in basename_tokens)
    token_ratio = shared / len(title_tokens)

    if token_ratio < 0.5:
        return 0.0

    # ------------------------------------------------------------------
    # Similarity scoring
    # ------------------------------------------------------------------

    artist_match = (
        artist_norm in basename_norm
        or (album_artist_norm and album_artist_norm in basename_norm)
    )

    title_match = title_norm in basename_norm

    artist_sim = 1.0 if artist_match else 0.0
    title_sim = 1.0 if title_match else token_ratio

    score = (artist_sim * 0.45) + (title_sim * 0.55)

    # ------------------------------------------------------------------
    # Boosts
    # ------------------------------------------------------------------

    if artist_match and title_match:
        score += 0.15

    if album_norm and album_norm in filename_norm:
        score += 0.25

    # ------------------------------------------------------------------
    # Orphan-token penalty
    # ------------------------------------------------------------------

    explained = (
        set(_tokenize_meaningful(artist_norm))
        | set(_tokenize_meaningful(core_title_norm))
        | set(_tokenize_meaningful(album_norm or ""))
    )

    full_tokens = set(_tokenize_meaningful(basename_norm))

    orphan_tokens = [t for t in full_tokens if t not in explained]

    if len(orphan_tokens) >= 2:
        score -= 0.3

    # ------------------------------------------------------------------
    # Duration scoring (legacy parity: graduated boosts/penalties).
    # ------------------------------------------------------------------
    # ``candidate_duration`` (seconds) is supplied by callers that can read a
    # real duration off the file (e.g. the download-completion fuzzy match).
    # Duration is strong independent evidence of track identity — an exact
    # match confirms the right version, while a large mismatch rules out a
    # differently-long track that happens to share a name.
    expected_duration = queue_duration_seconds(queue_item.get("duration"))
    candidate_duration = queue_duration_seconds(candidate_duration)
    if expected_duration and candidate_duration:
        duration_diff = abs(expected_duration - candidate_duration)
        if duration_diff <= 2:
            score += 0.22
        elif duration_diff <= 5:
            score += 0.12
        elif duration_diff <= 10:
            score -= 0.05
        elif duration_diff <= 30:
            score -= 0.15
        else:
            score -= 0.25
    elif expected_duration and not candidate_duration:
        # Missing/zero candidate length → moderate negative penalty so a
        # same-named different-length track is not accepted without evidence.
        score -= 0.15

    # ------------------------------------------------------------------
    # Final clamp
    # ------------------------------------------------------------------

    score = max(0.0, min(1.0, score))

    logger.debug(
        "Queue scorer: '%s' → %.2f",
        os.path.basename(filename),
        score,
    )

    return score


# =============================================================================
# ✅ FILESYSTEM HELPERS (NOT normalization_service)
# =============================================================================

def _strip_prefix_and_uid(basename: str):
    return _strip_soulseek_uid_suffix(
        _strip_track_number_prefix(basename)
    )


def _strip_track_number_prefix(name: str):
    return re.sub(r"^(?:\d+-\d+|\d+)[\s.\-_]+", "", name)


def _strip_soulseek_uid_suffix(name: str):
    return re.sub(r"_\d{12,}$", "", name)