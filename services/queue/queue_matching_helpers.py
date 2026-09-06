"""Queue matching helpers.

Utility functions for queue item matching:
    - filename_matches_queue_item(): Check if a downloaded filename
      corresponds to a queue item via fuzzy matching.

Architecture:
    Uses ``helpers.normalization_service`` exclusively for all text
    normalization. No duplicated logic. Conservative fallback matcher
    for filename-based matching when metadata is unavailable.
"""

from __future__ import annotations

import os

from helpers.normalization_service import (
    normalize_string,
    normalize_core_title,
    strip_brackets,
    edition_annotations_compatible,
)

from helpers.types_queue import QueueItem
from services.queue.queue_matching_config import TRACK_NUMBER_PREFIX_RE

# =============================================================================
# ✅ FILENAME MATCHER
# =============================================================================

def filename_matches_queue_item(filename: str, queue_item: QueueItem) -> bool:
    """
    ✅ Conservative filename matcher

    Used when metadata is unavailable or inconclusive.
    """

    if not filename:
        return False

    # An edition-annotated track ("Valhalla (Epic Edition)") must never match
    # the plain "Valhalla" queue item — strip_brackets below would otherwise
    # make the two indistinguishable.
    queue_title = queue_item.get("title") or ""
    if not edition_annotations_compatible(
        queue_title, os.path.splitext(os.path.basename(filename))[0]
    ):
        return False

    basename = os.path.basename(filename)

    # ✅ Clean filename structure using consolidated regex
    basename = TRACK_NUMBER_PREFIX_RE.sub("", basename)
    basename = strip_brackets(basename)

    # ✅ Normalize filename (core-safe)
    filename_norm = normalize_string(basename)

    # ✅ Normalize queue fields
    title_norm  = normalize_core_title(queue_item.get("title") or "")

    if not title_norm:
        return False

    # ✅ Lenient fallback
    # Artist name is frequently omitted from album track filenames, so we 
    # strictly verify the title is present within the normalized string
    return title_norm in filename_norm
