"""Artist Scanner Service

This module handles scanning operations at the artist level.

Key Responsibilities:
    - Fetch all albums for a given artist from the database
    - Iterate through albums sequentially
    - Delegate individual album scanning to album_scanner.py
"""

from __future__ import annotations

from typing import Any

import structlog

from db.repositories.library import get_albums_for_artist
from helpers.logging_config import log_unified
from services.scanning.album_scanner import scan_album

logger = structlog.get_logger(__name__)


def scan_artist(artist: str, force: bool = False) -> None:
    """Scan all albums for a single artist."""
    log_unified(f"[ARTIST_SCANNER] Starting scan for artist: {artist} (force={force})")
    logger.info("Starting scan for artist", artist=artist, force=force)

    albums = get_albums_for_artist(artist) or []

    for album in albums:
        logger.debug("Scanning album for artist", artist=artist, album=album)
        try:
            scan_album(artist, album, force=force)
        except Exception as exc:
            logger.error("Failed scanning album for artist", artist=artist, album=album, error=str(exc))
