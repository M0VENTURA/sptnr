"""Album Scanner Service

This module handles scanning operations at the album level.

Key Responsibilities:
    - Fetch all tracks for a given artist/album combination
    - Return track list for further processing
    - Serve as the delegation point from artist_scanner.py
    
Architecture:
    Part of the scanning hierarchy:
    
    scanner.py (main loop)
        ↓
    artist_scanner.py
        ↓
    album_scanner.py (this file)
        ↓
    [Track-level processing in popularity stages]
    
    This module is intentionally minimal - it's a data retrieval layer.

Historical Context:
    Replaces album loops that were previously embedded directly in
    popularity_scan(). This separation improves testability and maintainability.

Usage:
    >>> from services.scanning.album_scanner import scan_album
    >>> tracks = scan_album("The Beatles", "Abbey Road", force=False)
    >>> print(f"Found {len(tracks)} tracks")

Performance Notes:
    - Single database query per album
    - Returns all tracks at once (not paginated)
    - Track-level processing happens elsewhere
"""

import logging
from db.repositories.library import get_tracks_for_album

logger = logging.getLogger(__name__)

def scan_album(artist, album, force=False):
    tracks = get_tracks_for_album(artist, album)
    return tracks
