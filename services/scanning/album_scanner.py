"""Album Scanner Service

This module handles scanning operations at the album level.

Key Responsibilities:
    - Fetch all tracks for a given artist/album combination
    - Return track list for further processing
    - Serve as the delegation point from artist_scanner.py
"""

from __future__ import annotations

from typing import Any

import structlog

from db.repositories.library import get_tracks_for_album

logger = structlog.get_logger(__name__)


def scan_album(artist: str, album: str, force: bool = False) -> list[dict[str, Any]]:
    """Fetch all tracks for a given artist/album combination."""
    logger.debug("Scanning album tracks", artist=artist, album=album, force=force)
    tracks = get_tracks_for_album(artist, album)
    return tracks or []
