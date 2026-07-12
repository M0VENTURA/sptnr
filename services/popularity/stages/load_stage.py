"""Popularity scan: data loading stage.

Loads candidate artists/albums/tracks from the database for
processing by the popularity scan pipeline. Builds the
artist→album→track structure used by subsequent stages.
"""

from __future__ import annotations

from typing import Any, List, Dict

from db.repositories.library import (
    get_all_artists,
    get_albums_for_artist,
    get_tracks_for_album,
)


import logging

logger = logging.getLogger(__name__)


def load_candidates(options: dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Build artist → album → track structure for scan pipeline.
    """

    injected = options.get("albums")
    if injected is None:
        logger.debug("[LOAD_STAGE] Loading candidates from database")
    else:
        logger.debug("[LOAD_STAGE] Using injected album data")
    if injected is not None:
        return injected

    artist_filter = options.get("artist_filter")
    album_filter = options.get("album_filter")

    candidates: List[Dict[str, Any]] = []

    # -------------------------------------------------------------------------
    # 1. Get artists
    # -------------------------------------------------------------------------

    artists = get_all_artists()

    for artist in artists:
        if artist_filter and artist != artist_filter:
            continue

        # ---------------------------------------------------------------------
        # 2. Get albums
        # ---------------------------------------------------------------------

        albums = get_albums_for_artist(artist)

        for album in albums:
            if album_filter and album != album_filter:
                continue

            # -----------------------------------------------------------------
            # 3. Get tracks (YOU JUST FIXED THIS ✅)
            # -----------------------------------------------------------------

            tracks = get_tracks_for_album(artist, album)

            if not tracks:
                continue

            candidates.append({
                "artist": artist,
                "album": album,
                "album_artist": artist,
                "spotify_album_type": None,
                "musicbrainz_album_type": None,
                "tracks": tracks,
            })

    return candidates