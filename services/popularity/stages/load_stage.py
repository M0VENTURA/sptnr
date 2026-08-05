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
    resume_from = options.get("resume_from")

    candidates: List[Dict[str, Any]] = []

    # -------------------------------------------------------------------------
    # 1. Get artists
    # -------------------------------------------------------------------------

    artists = get_all_artists()

    # Resume support (legacy parity): skip artists before the resume point.
    # A fuzzy match (resume artist contained in the current artist) is also
    # accepted, matching the legacy popularity scanner.
    resume_hit = False if resume_from else True

    for artist in artists:
        # Case-insensitive match — the artist/album pages render with LOWER()
        # queries, so URL casing can differ from the stored name; a
        # case-sensitive filter here silently produces zero candidates
        # ("No tracks found") for such scans.
        if artist_filter and artist.lower().strip() != artist_filter.lower().strip():
            continue

        if not resume_hit:
            if resume_from and (
                artist.lower() == resume_from.lower()
                or resume_from.lower() in artist.lower()
            ):
                resume_hit = True
                # Do NOT skip the matched artist — rescan from this point.
            else:
                continue

        # ---------------------------------------------------------------------
        # 2. Get albums
        # ---------------------------------------------------------------------

        albums = get_albums_for_artist(artist)

        for album in albums:
            if album_filter and album.lower().strip() != album_filter.lower().strip():
                continue

            # -----------------------------------------------------------------
            # 3. Get tracks (YOU JUST FIXED THIS ✅)
            # -----------------------------------------------------------------

            tracks = get_tracks_for_album(artist, album)

            if not tracks:
                continue

            # Skip albums with fewer tracks than the configured minimum
            # (features.album_skip_min_tracks, default 1 = no-op).
            try:
                from helpers.config_helpers import get_feature
                min_tracks = int(get_feature("album_skip_min_tracks", 1) or 1)
            except Exception:
                min_tracks = 1
            if len(tracks) < max(1, min_tracks):
                logger.debug("[LOAD_STAGE] Skipping '%s - %s': only %s track(s), min %s", artist, album, len(tracks), min_tracks)
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