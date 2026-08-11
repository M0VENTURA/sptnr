"""Artist Scanner Service

This module handles scanning operations at the artist level.

Key Responsibilities:
    - Fetch all albums for a given artist from the database
    - Iterate through albums sequentially
    - Delegate individual album scanning to album_scanner.py
    
Architecture:
    This is an orchestration layer that coordinates album-level scanning:
    
    scanner.py (main loop)
        ↓
    artist_scanner.py (this file)
        ↓
    album_scanner.py
    
    The service follows the single responsibility principle:
    - No scoring logic here
    - No metadata lookups here  
    - Pure orchestration only

Usage:
    >>> from services.scanning.artist_scanner import scan_artist
    >>> scan_artist("The Beatles", force=False)  # Scan all Beatles albums

Performance Notes:
    - Albums are processed sequentially (not in parallel)
    - Progress is saved after each album completes
    - Safe to interrupt and resume via progress tracking
"""

import logging
from services.scanning.album_scanner import scan_album
from db.repositories.library import get_albums_for_artist
from helpers.logging_config import log_unified

logger = logging.getLogger(__name__)


def scan_artist(artist, force=False):
    """
    Scan all albums for a single artist.

    Args:
        artist (str):
            Name of the artist.
        force (bool):
            If True, bypass skip logic for all tracks.

    Flow:
        1. Retrieve album list from DB
        2. Iterate albums
        3. Delegate each album to scan_album()

    Notes:
        - No scoring logic here
        - No metadata logic here
        - Pure orchestration layer
    """
    log_unified(f"[ARTIST_SCANNER] Starting scan for artist: {artist} (force={force})")

    albums = get_albums_for_artist(artist)

    for album in albums:
        logger.debug("Scanning album: %s - %s", artist, album)
        scan_album(artist, album, force=force)