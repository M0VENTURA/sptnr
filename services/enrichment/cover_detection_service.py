"""Cover song detection service.

Identifies cover songs using the ``CoverDetector`` class, which uses
MusicBrainz relations, writer/composer analysis, ISRC matching, and
heuristic title annotations.

Key Functions:
    - detect_covers_for_artist(): Scan all tracks for an artist and mark
      covers in the database.
    - detect_covers_for_album(): Detect covers within a single album (used
      during popularity scan).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from cover_detector import CoverDetector
from db.utils import get_db_connection, row_get
from helpers.normalization_service import detect_cover_and_normalize_title

logger = logging.getLogger(__name__)


def detect_covers_for_artist(artist_name: str, conn) -> int:
    """Scan all tracks for *artist_name* and mark covers in the database.

    Uses the full ``CoverDetector`` pipeline (ISRC, MB relations, writer
    analysis, heuristics) for each track individually.  Returns the number
    of tracks updated.
    """
    try:
        detector = CoverDetector(db_connection=conn)
    except Exception as exc:
        logger.warning("CoverDetector unavailable: %s", exc)
        return 0

    cur = conn.cursor()
    cur.execute(
        "SELECT id, title, artist, album, composer, writer, isrc, mbid, "
        "musicbrainz_album_mbid, file_path, is_cover, genres, musicbrainz_genres "
        "FROM tracks WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(%s)",
        (artist_name,),
    )
    rows = cur.fetchall() or []

    # Build a pseudo-album context so per-album caching works.
    albums: Dict[str, List[Dict]] = {}
    updated = 0
    for row in rows:
        track = {
            "id": str(row_get(row, "id", 0, "")),
            "title": str(row_get(row, "title", 1, "")),
            "artist": str(row_get(row, "artist", 2, "")),
            "album": str(row_get(row, "album", 3, "")),
            "writer": row_get(row, "writer", 4),
            "isrc": row_get(row, "isrc", 6),
            "mbid": row_get(row, "mbid", 7),
            "musicbrainz_album_mbid": row_get(row, "musicbrainz_album_mbid", 8),
            "file_path": row_get(row, "file_path", 9),
            "is_cover": row_get(row, "is_cover", 10),
            "genres": row_get(row, "genres", 11),
            "musicbrainz_genres": row_get(row, "musicbrainz_genres", 12),
        }
        album_key = track["album"] or "_no_album"
        albums.setdefault(album_key, []).append(track)

    for album_name, tracks in albums.items():
        artist = tracks[0].get("artist") or artist_name
        results = detector.detect_covers_for_album(
            album=album_name, artist=artist, tracks=tracks
        )
        updated += len(results)

    logger.info("Cover detection for '%s': %d covers found across %d album(s)",
                artist_name, updated, len(albums))
    return updated


def detect_covers_for_album(
    album: str,
    artist: str,
    tracks: List[Dict[str, Any]],
    conn=None,
) -> List[Dict[str, Any]]:
    """Detect covers for a single album context (called during popularity scan).

    Returns the list of cover result dicts so the caller can integrate them
    into the track update payload.
    """
    try:
        detector = CoverDetector(db_connection=conn)
        return detector.detect_covers_for_album(album=album, artist=artist, tracks=tracks)
    except Exception as exc:
        logger.warning("Cover detection failed for '%s' by '%s': %s", album, artist, exc)
        return []


def detect_cover_song(title: str, artist: str, composer: Optional[str] = None,
                      writer: Optional[str] = None) -> tuple[bool, str]:
    """Quick single-track cover check used during popularity scanning.

    Returns (is_cover, normalized_title).  Runs the cheap title-string check
    first and only falls through to writer/composer comparison when needed.
    """
    is_cover, normalized = detect_cover_and_normalize_title(title)
    if is_cover:
        return is_cover, normalized

    if composer or writer:
        from cover_detector import _names_match as names_match
        for c in [composer, writer]:
            if c and not names_match(c, artist):
                return True, normalized
    return False, normalized

