"""Download Match Engine

This module provides sophisticated matching algorithms for comparing downloaded
file folders against known music releases from sources like MusicBrainz.

Key Responsibilities:
    - Token-based text normalization and comparison
    - Multi-factor scoring (track count, title similarity, order consistency)
    - Folder-to-release match quality assessment
    
Scoring Algorithm:
    The engine uses a weighted combination of three factors:
    
    1. Track Count Match (30% weight)
       - Exact count = 1.0
       - Close count (≥80% ratio) = 0.5
       - Otherwise = 0.0
    
    2. Title Similarity (50% weight)
       - Uses SequenceMatcher for fuzzy string comparison
       - Averages similarity across all track pairs
    
    3. Track Order Consistency (20% weight)
       - Rewards matches that preserve original track ordering
       - Penalizes shuffled or reordered tracks
    
Usage:
    >>> from services.downloads.match_engine import score_folder_match
    >>> score, details = score_folder_match(folder_tracks, release_tracks)
    >>> if score >= 0.75:
    ...     print("High confidence match!")

Architecture:
    This module is purely algorithmic - no database access or API calls.
    It receives normalized track data and returns match scores.
    
    Called by: services/downloads/download_matching_service.py
    Calls: helpers.normalization_service (for text normalization)
"""

from difflib import SequenceMatcher
from typing import List, Dict, Any, Tuple, Optional
import os
import re
from api_clients import logger
from helpers.normalization_service import normalize_match_text, edition_annotations_compatible
from services.downloads.download_matching_service import get_release_tracks


def _seq_ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0

    return SequenceMatcher(None, a, b).ratio()


def _tokenize_meaningful(text: str) -> List[str]:
    """
    Tokenize normalized text into useful comparison tokens.
    """

    if not text:
        return []

    tokens = re.findall(r"[a-z0-9]+", text.lower())

    ignored = {
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "for",
        "with",
        "feat",
        "ft",
        "featuring",
        "remaster",
        "remastered",
        "explicit",
        "clean",
        "version",
        "edit",
        "mix",
    }

    return [token for token in tokens if token not in ignored]


def _token_overlap_score(a: str, b: str) -> float:
    """
    Return token overlap score between two normalized strings.
    """

    a_tokens = set(_tokenize_meaningful(a))
    b_tokens = set(_tokenize_meaningful(b))

    if not a_tokens or not b_tokens:
        return 0.0

    return len(a_tokens & b_tokens) / len(a_tokens | b_tokens)


def score_folder_match(
    folder_tracks: List[Dict[str, Any]],
    release_tracks: List[Dict[str, Any]],
) -> Tuple[float, Dict[str, Any]]:
    """
    Score how well a folder's tracks match a release.

    Scoring:
        - Track count match: 30%
        - Title similarity: 50%
        - Track order consistency: 20%
    """

    if not release_tracks:
        return 0.0, {}

    folder_tracks = folder_tracks or []

    scores: List[Tuple[str, float, float]] = []
    details: Dict[str, Any] = {}

    # -------------------------------------------------------------------------
    # Score 1: Track count match
    # -------------------------------------------------------------------------

    folder_count = len(folder_tracks)
    release_count = len(release_tracks)

    if folder_count == release_count:
        count_score = 1.0
    elif folder_count > 0 and release_count > 0:
        ratio = min(folder_count, release_count) / max(folder_count, release_count)
        count_score = 0.5 if ratio >= 0.8 else 0.0
    else:
        count_score = 0.0

    scores.append(("count_match", count_score, 0.3))

    details["track_count_match"] = {
        "folder": folder_count,
        "release": release_count,
        "score": count_score,
    }

    # -------------------------------------------------------------------------
    # Score 2: Title similarity
    # -------------------------------------------------------------------------

    title_matches: List[Dict[str, Any]] = []

    if folder_tracks and release_tracks:
        for i, folder_track in enumerate(folder_tracks):
            folder_title = folder_track.get("title") or folder_track.get("file") or ""

            best_sim = 0.0
            best_match_idx = -1

            for j, release_track in enumerate(release_tracks):
                release_title = release_track.get("title") or ""

                sim = _best_title_similarity(folder_title, release_title)

                if sim > best_sim:
                    best_sim = sim
                    best_match_idx = j

            title_matches.append(
                {
                    "folder_idx": i,
                    "folder_title": folder_title or "Unknown",
                    "best_match_idx": best_match_idx,
                    "release_title": (
                        release_tracks[best_match_idx].get("title", "")
                        if best_match_idx >= 0
                        else ""
                    ),
                    "similarity": best_sim,
                }
            )

        avg_title_score = (
            sum(match["similarity"] for match in title_matches) / len(title_matches)
            if title_matches
            else 0.0
        )

        title_score = avg_title_score if avg_title_score >= 0.6 else 0.0
    else:
        title_score = 0.0

    scores.append(("title_similarity", title_score, 0.5))
    details["title_matches"] = title_matches

    # -------------------------------------------------------------------------
    # Score 3: Track order consistency
    # -------------------------------------------------------------------------

    order_score = 1.0
    mismatches = 0

    has_track_numbers = any(
        track.get("track_number") or track.get("track") or track.get("number")
        for track in folder_tracks
    )

    if has_track_numbers and release_tracks:
        for folder_track in folder_tracks:
            track_num = (
                folder_track.get("track_number")
                or folder_track.get("track")
                or folder_track.get("number")
            )

            if not track_num:
                continue

            try:
                expected_idx = int(str(track_num).split("/")[0]) - 1

                if expected_idx < 0 or expected_idx >= len(release_tracks):
                    continue

                expected_title = release_tracks[expected_idx].get("title", "")
                actual_title = folder_track.get("title") or folder_track.get("file") or ""

                if expected_title and actual_title:
                    sim = _best_title_similarity(actual_title, expected_title)

                    if sim < 0.6:
                        mismatches += 1

            except (ValueError, TypeError, IndexError):
                continue

        if mismatches > 0 and folder_tracks:
            order_score = max(0.3, 1.0 - (mismatches / len(folder_tracks)))

    scores.append(("track_order", order_score, 0.2))

    details["track_order"] = {
        "mismatches": mismatches,
        "score": order_score,
    }

    total_score = sum(score * weight for _, score, weight in scores)

    details["weighted_scores"] = {
        name: {
            "score": score,
            "weight": weight,
        }
        for name, score, weight in scores
    }

    return total_score, details




def filename_matches_queue_item(
    file_path: str,
    queue_item: Dict[str, Any],
    threshold: float = 0.65,
) -> bool:
    """
    Determine whether a downloaded filename/path appears to match a queue item.

    This replaces the old queue_processor._filename_matches_queue_item-style
    dependency with a clean service function.

    Used by:
        - cleanup_engine_service
        - duplicate sibling cleanup
        - mismatched download handling
    """

    if not file_path or not queue_item:
        return False

    artist = queue_item.get("artist") or ""
    album_artist = queue_item.get("album_artist") or ""
    title = queue_item.get("title") or ""

    # An edition-annotated track ("Valhalla (Epic Edition)") must never match
    # the plain "Valhalla" queue item — normalize_match_text strips brackets on
    # both sides, so the edition suffix would otherwise be invisible.  Strip the
    # file extension so the trailing "(Epic Edition)" annotation is still
    # extractable from the path.
    if not edition_annotations_compatible(title, os.path.splitext(file_path)[0]):
        return False

    artist_norm = normalize_match_text(artist)
    album_artist_norm = normalize_match_text(album_artist)
    title_norm = normalize_match_text(title)
    path_norm = normalize_match_text(file_path)

    if not title_norm or not path_norm:
        return False

    artist_candidates = [
        value for value in (artist_norm, album_artist_norm)
        if value
    ]

    title_in_path = title_norm in path_norm

    title_score = max(
        _seq_ratio(title_norm, path_norm),
        _token_overlap_score(title_norm, path_norm),
        1.0 if title_in_path else 0.0,
    )

    artist_score = 0.0

    for candidate in artist_candidates:
        candidate_score = max(
            _seq_ratio(candidate, path_norm),
            _token_overlap_score(candidate, path_norm),
            1.0 if candidate in path_norm else 0.0,
        )
        artist_score = max(artist_score, candidate_score)

    if title_in_path and any(candidate in path_norm for candidate in artist_candidates):
        return True

    if title_score >= 0.80 and artist_score >= 0.35:
        return True

    combined = (title_score * 0.70) + (artist_score * 0.30)

    return combined >= threshold



def _best_title_similarity(folder_title: str, release_title: str) -> float:
    folder_norm = normalize_match_text(folder_title or "")
    release_norm = normalize_match_text(release_title or "")

    if not folder_norm or not release_norm:
        return 0.0

    if folder_norm == release_norm:
        return 1.0

    if folder_norm in release_norm or release_norm in folder_norm:
        return 0.9

    return max(
        _seq_ratio(folder_norm, release_norm),
        _token_overlap_score(folder_norm, release_norm),
    )


def suggest_auto_match(
    folder_artist: str,
    folder_album: str,
    candidates: List[Dict[str, Any]],
    folder_tracks: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """
    Automatically suggest the best release candidate if confidence is high enough.
    """

    if not candidates or not folder_tracks:
        return None

    best_candidate = None
    best_score = 0.0
    best_details: Dict[str, Any] = {}

    folder_artist_norm = normalize_match_text(folder_artist or "")
    folder_album_norm = normalize_match_text(folder_album or "")

    for candidate in candidates:
        release_id = candidate.get("id") or candidate.get("release_id")

        if not release_id:
            continue

        release_tracks = get_release_tracks(
            release_id,
            candidate.get("source", "musicbrainz"),
        )

        if not release_tracks:
            continue

        score, details = score_folder_match(folder_tracks, release_tracks)

        candidate_artist_norm = normalize_match_text(
            candidate.get("artist")
            or candidate.get("artist_name")
            or ""
        )

        candidate_album_norm = normalize_match_text(
            candidate.get("title")
            or candidate.get("album")
            or ""
        )

        # Small bonus if candidate artist text aligns with folder labels.
        if folder_artist_norm and candidate_artist_norm:
            artist_score = max(
                _seq_ratio(folder_artist_norm, candidate_artist_norm),
                _token_overlap_score(folder_artist_norm, candidate_artist_norm),
                1.0 if folder_artist_norm == candidate_artist_norm else 0.0,
            )

            if artist_score >= 0.8:
                score += 0.05

        # Small bonus if candidate album text aligns with folder labels.
        if folder_album_norm and candidate_album_norm:
            album_score = max(
                _seq_ratio(folder_album_norm, candidate_album_norm),
                _token_overlap_score(folder_album_norm, candidate_album_norm),
                1.0 if folder_album_norm == candidate_album_norm else 0.0,
            )

            if album_score >= 0.8:
                score += 0.05

        score = min(score, 1.0)

        if score > best_score:
            best_score = score
            best_candidate = dict(candidate)
            best_details = details

    if best_score >= 0.75 and best_candidate:
        best_candidate["auto_match_confidence"] = best_score
        best_candidate["match_details"] = best_details

        logger.info(
            "Auto-match suggested: %s confidence=%.2f",
            best_candidate.get("title") or best_candidate.get("album") or "Unknown",
            best_score,
        )

        return best_candidate

    logger.info("No high-confidence auto-match found. best=%.2f", best_score)

    return None