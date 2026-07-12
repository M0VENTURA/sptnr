"""Popularity scan finalisation stage."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def finalise_scan(*, results: list[dict[str, Any]], options: dict[str, Any]) -> None:
    """Finalise scan.

    Move old finalisation logic here:
    - final DB commits if buffered
    - playlist refresh
    - rating sync
    - summary logging
    - clearing progress files
    """
    track_count = len(results) if results else 0
    logger.info("[FINALISE_STAGE] Finalising scan — %s tracks processed", track_count)
    return None

