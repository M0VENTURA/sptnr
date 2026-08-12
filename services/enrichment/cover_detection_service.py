"""Cover song detection service.

Identifies cover songs using the ``CoverDetector`` class, which uses
MusicBrainz relations, writer/composer analysis, ISRC matching, and
heuristic title annotations.

Key Functions:
    - detect_covers_for_artist(): Scan all tracks for an artist and mark
      covers in the database.
    - detect_covers_for_album(): Detect covers within a single album (used
      during popularity scan).
    - detect_cover_song(): Quick single-track check used during popularity
      scanning.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from services.enrichment.cover_detector_impl import CoverDetector
from db.utils import get_db_connection, row_get
from helpers.normalization_service import detect_cover_and_normalize_title

logger = logging.getLogger(__name__)


def detect_covers_for_artist(artist_name: str, conn, force: bool = False) -> int:
    """Scan all tracks for *artist_name* and mark covers in the database.

    Uses the full ``CoverDetector`` pipeline (ISRC, MB relations, writer
    analysis, heuristics) for each track individually.  Returns the number
    of tracks updated.  ``conn`` is kept for backward compatibility — DB
    access runs on SQLAlchemy sessions.
    """
    try:
        detector = CoverDetector(db_connection=None)
    except Exception as exc:
        logger.warning("CoverDetector unavailable: %s", exc)
        return 0

    from sqlalchemy import text
    from db.engine import db_session
    with db_session() as session:
        result = session.execute(
            text("SELECT id, title, artist, album, composer, writer, isrc, mbid, "
                 "musicbrainz_album_mbid, file_path, is_cover, genres, musicbrainz_genres, "
                 "original_cover_artist, cover_manual_override, cover_last_checked "
                 "FROM tracks WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)"),
            {"artist": artist_name},
        )
        rows = result.fetchall() or []

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
            "original_cover_artist": row_get(row, "original_cover_artist", 13, ""),
            "cover_manual_override": row_get(row, "cover_manual_override", 14, False),
            "cover_last_checked": row_get(row, "cover_last_checked", 15),
        }
        album_key = track["album"] or "_no_album"
        albums.setdefault(album_key, []).append(track)

    for album_name, tracks in albums.items():
        artist = tracks[0].get("artist") or artist_name
        results = detector.detect_covers_for_album(
            album=album_name, artist=artist, tracks=tracks, force=force,
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
    force: bool = False,
) -> List[Dict[str, Any]]:
    """Detect covers for a single album context (called during popularity scan).

    Args:
        album: Album title.
        artist: Album artist.
        tracks: List of track dicts with metadata fields.
        conn: Optional DB connection (used by the underlying detector).
        force: If True, re-check even already-confirmed covers.

    Returns the list of cover result dicts so the caller can integrate them
    into the track update payload.
    """
    try:
        detector = CoverDetector(db_connection=conn)
        return detector.detect_covers_for_album(
            album=album, artist=artist, tracks=tracks, force=force,
        )
    except Exception as exc:
        logger.warning("Cover detection failed for '%s' by '%s': %s", album, artist, exc)
        return []


def detect_cover_song(
    title: str,
    artist: str,
    composer: Optional[str] = None,
    writer: Optional[str] = None,
    track_data: Optional[Dict[str, Any]] = None,
    force: bool = False,
) -> tuple[bool, str]:
    """Quick single-track cover check used during popularity scanning.

    Returns ``(is_cover, reason)``.  Runs the cheap title-string check
    first and only falls through to writer/composer comparison when needed.

    Caching behaviour (avoids re-detecting on every scan):
    - If ``track_data`` contains ``cover_manual_override=True`` → skip entirely.
    - If ``track_data`` has ``is_cover`` truthy AND ``original_cover_artist``
      is populated → skip unless ``force=True``.
    - Otherwise run the standard detection pipeline.
    """
    # ── Cache check: skip if already confirmed ──────────────────────────
    if not force and track_data:
        manual = track_data.get("cover_manual_override")
        if manual:
            return False, "manual_override"

        existing_cover = track_data.get("is_cover")
        existing_orig = str(track_data.get("original_cover_artist") or "").strip()
        if existing_cover and existing_orig:
            return False, "already_confirmed"

    # ── Standard detection ──────────────────────────────────────────────
    is_cover, normalized = detect_cover_and_normalize_title(title)
    if is_cover:
        return is_cover, normalized

    if composer or writer:
        from helpers.normalization_service import names_match
        for c in [composer, writer]:
            if c and not names_match(c, artist):
                return True, normalized
    return False, normalized

