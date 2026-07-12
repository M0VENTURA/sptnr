"""Queue matching configuration.

Single source of truth for queue matching constants and configuration.

Defines:
    - MATCH_TARGET_STATUSES: Queue statuses eligible for matching.
    - Matching category groups for status filtering.

Do NOT place user-configurable values here — those belong in
``config.yaml`` accessed via ``helpers.config_helpers``.
"""

from __future__ import annotations

import re

# =============================================================================
# MATCH TARGET STATUSES
# =============================================================================

MATCH_TARGET_STATUSES: tuple[str, ...] = (
    "queued",
    "searching",
    "downloading",
    "matched",
    "completed",
    "unmatched",
    "queried",
    "discovered",
    "pending_match",
    "possible_duplicate",
    "duplicate",
)

# =============================================================================
# TITLE VARIANTS
# =============================================================================

TITLE_VARIANT_TOKENS: frozenset[str] = frozenset({
    "acoustic",
    "demo",
    "edit",
    "instrumental",
    "intro",
    "live",
    "mix",
    "orchestral",
    "radio",
    "remaster",
    "remastered",
    "remix",
    "version",
})

SOFT_VARIANT_TOKENS: frozenset[str] = frozenset({
    "version",
    "edit",
    "radio",
})

# =============================================================================
# COMPILATION SUPPORT
# =============================================================================

GENERIC_COMPILATION_ARTISTS: frozenset[str] = frozenset({
    "various artists",
    "various artist",
    "various",
    "va",
    "v/a",
    "unknown artist",
    "unknown",
    "soundtrack",
    "ost",
})

# =============================================================================
# REGEX HELPERS
# =============================================================================

FEAT_SUFFIX_RE = re.compile(
    r"\s+(?:feat\.?|ft\.?|featuring)\s+.*$",
    re.IGNORECASE,
)

TRACK_NUMBER_PREFIX_RE = re.compile(
    r"^(?:\d+-\d+|\d+)\s*[\.\s\-]*\s*",
)

SOULSEEK_UID_SUFFIX_RE = re.compile(
    r"_\d{12,}$",
)

ORPHAN_NUM_RE = re.compile(
    r"^\d{1,4}$"
)

ORPHAN_AUDIO_EXT_TOKENS: frozenset[str] = frozenset({
    "mp3",
    "flac",
    "wav",
    "ogg",
    "aac",
    "m4a",
    "wma",
    "opus",
    "aiff",
})

# =============================================================================
# GENRE HELPERS
# =============================================================================

def is_live_track_from_genre(
    genre_value: str | None,
) -> bool:
    """
    Detect 'live' recordings from genre tags.
    """

    if not genre_value:
        return False

    parts = (
        str(genre_value)
        .replace("/", "\\")
        .split("\\")
    )

    return any(
        p.strip().lower() == "live"
        for p in parts
    )


def is_remix_track_from_genre(
    genre_value: str | None,
) -> bool:
    """
    Detect remix recordings from genre tags.
    """

    if not genre_value:
        return False

    parts = (
        str(genre_value)
        .replace("/", "\\")
        .split("\\")
    )

    return any(
        p.strip().lower() == "remix"
        for p in parts
    )


__all__ = [
    "MATCH_TARGET_STATUSES",
    "TITLE_VARIANT_TOKENS",
    "SOFT_VARIANT_TOKENS",
    "GENERIC_COMPILATION_ARTISTS",
    "FEAT_SUFFIX_RE",
    "TRACK_NUMBER_PREFIX_RE",
    "SOULSEEK_UID_SUFFIX_RE",
    "ORPHAN_AUDIO_EXT_TOKENS",
    "ORPHAN_NUM_RE",
    "is_live_track_from_genre",
    "is_remix_track_from_genre",
]