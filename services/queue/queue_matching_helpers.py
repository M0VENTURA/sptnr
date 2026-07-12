"""Queue matching helpers.

Utility functions for queue item matching:
    - filename_matches_queue_item(): Check if a downloaded filename
      corresponds to a queue item via fuzzy matching.
    - _strip_track_prefix(): Remove track number prefixes from filenames.

Architecture:
    Uses ``helpers.normalization_service`` exclusively for all text
    normalization. No duplicated logic. Conservative fallback matcher
    for filename-based matching when metadata is unavailable.
"""

from __future__ import annotations

import os
import re

from helpers.normalization_service import (
    normalize_string,
    normalize_artist,
    normalize_core_title,
    strip_brackets,
)

from helpers.types_queue import QueueItem

# =============================================================================
# ✅ FILE HELPERS (stay local)
# =============================================================================

def _strip_track_prefix(filename: str) -> str:
    """
    Removes prefixes like:
    01 -
    1-03 -
    07.
    """
    return re.sub(r"^(?:\d+-\d+|\d+)[\s\.\-_]+", "", filename)


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

    basename = os.path.basename(filename)

    # ✅ Clean filename structure
    basename = _strip_track_prefix(basename)
    basename = strip_brackets(basename)

    # ✅ Normalize filename (core-safe)
    filename_norm = normalize_string(basename)

    # ✅ Normalize queue fields (correctly!)
    artist_norm = normalize_artist(queue_item.get("artist") or "")
    title_norm  = normalize_core_title(queue_item.get("title") or "")


    if not artist_norm or not title_norm:
        return False

    # ✅ Conservative matching rule
    return (
        artist_norm in filename_norm and
        title_norm in filename_norm
    )