"""Album enrichment/statistics stage."""

from __future__ import annotations

from typing import Any


def enrich_album(
    *,
    album_row: dict[str, Any],
    album_context: dict[str, Any],
    stat_eligible_tracks: list[dict[str, Any]],
    options: dict[str, Any],
) -> dict[str, Any]:
    """Run album-level enrichment and stat preparation.

    Move old album-level logic here:
    - album type decisions
    - album stats population
    - album art lookup/save
    - compilation/greatest-hits decisions
    """
    return {
        "album_row": album_row,
        "album_context": album_context,
        "stat_eligible_tracks": stat_eligible_tracks,
    }

