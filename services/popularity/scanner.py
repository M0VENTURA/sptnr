"""
services/popularity/scanner.py

Main orchestration layer for popularity scanning.

Responsibilities:
- iterates artists/albums/tracks
- coordinates scoring + enrichment
- handles batching + threading
- DOES NOT contain business logic (delegates to services)
"""

from services.popularity.scoring import calculate_track_score
from services.popularity.single_detection import detect_single
from services.popularity.enrichment import enrich_track_metadata
from services.popularity.filtering import should_skip_track
from db.repositories.tracks import get_tracks_for_artist, update_track


def popularity_scan(artist_filter=None, album_filter=None, force=False):
    """
    Main entrypoint for popularity scanning.

    This should:
    - fetch tracks from DB
    - process each track
    - update DB
    """

    tracks = get_tracks_for_artist(artist_filter, album_filter)

    for track in tracks:
        if should_skip_track(track, force=force):
            continue

        # 1. Enrich metadata (APIs)
        enrich_track_metadata(track)

        # 2. Calculate popularity score
        score_data = calculate_track_score(track)

        # 3. Detect single
        single_data = detect_single(score_data, track)

        # 4. Persist
        update_track(track["id"], {
            **score_data,
            **single_data
        })