"""Cover song detection service.

Identifies cover songs (tracks that are not original compositions) by
comparing track titles against known composers/writers. Delegates to
``helpers.normalization_service.detect_cover_and_normalize_title`` for
the core detection logic.

Key Functions:
    - detect_covers_for_artist(): Scan all tracks for an artist and mark
      covers in the database.

Architecture:
    Uses an external ``CoverDetector`` class (from the optional
    ``cover_detector`` package) for the actual detection algorithm.
    Falls back to no-op if the dependency is not available.
"""
from __future__ import annotations
from helpers.normalization_service import detect_cover_and_normalize_title


def detect_covers_for_artist(artist_name: str, conn) -> int:
    try:
        from cover_detector import CoverDetector
    except Exception:
        return 0
    detector = CoverDetector()
    cur = conn.cursor()
    cur.execute("SELECT id, title, artist, composer, writer FROM tracks WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s", (artist_name,))
    updated = 0
    for row in cur.fetchall() or []:
        try:
            track_id = row[0]
            title = row[1]
            is_cover = detector.is_cover(title=title, artist=artist_name, composer=row[3] if len(row) > 3 else None, writer=row[4] if len(row) > 4 else None)
            if is_cover:
                cur.execute("UPDATE tracks SET is_cover = TRUE WHERE id = %s", (track_id,))
                updated += 1
        except Exception:
            continue
    conn.commit()
    return updated

