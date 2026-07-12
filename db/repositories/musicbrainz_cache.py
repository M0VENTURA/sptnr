"""MusicBrainz release metadata cache.

Stores and retrieves MusicBrainz release metadata (release info + track list)
from ``musicbrainz_releases`` and ``musicbrainz_release_tracks`` tables.

Used by the download matching pipeline to avoid repeated MusicBrainz
API calls for the same release.
"""

from typing import Optional, Dict, Any, List
import logging

from sqlalchemy import text

from db.engine import db_session

logger = logging.getLogger(__name__)

def get_cached_release_metadata(release_id: str) -> Optional[Dict[str, Any]]:
    try:
        with db_session() as session:

            result = session.execute(
                text("""
                    SELECT release_title, artist, release_year
                    FROM musicbrainz_releases
                    WHERE release_id = :release_id
                    LIMIT 1
                """),
                {"release_id": release_id},
            )
            release_row = result.fetchone()

            result = session.execute(
                text("""
                    SELECT disc_number, track_number, track_title, track_artist,
                           duration, isrc, recording_title, recording_mbid
                    FROM musicbrainz_release_tracks
                    WHERE release_id = :release_id
                    ORDER BY COALESCE(disc_number, 1),
                             COALESCE(track_number, 999999)
                """),
                {"release_id": release_id},
            )
            track_rows = result.fetchall()

            if not release_row and not track_rows:
                return None

            tracks = []
            for r in track_rows:
                tracks.append({
                    "disc_number": r[0] if r[0] is not None else 1,
                    "track_number": r[1],
                    "title": r[2] or "",
                    "artist": r[3] or "",
                    "duration": r[4],
                    "isrc": r[5],
                    "recording_title": r[6],
                    "recording_mbid": r[7],
                })

            return {
                "release_title": release_row[0] or "" if release_row else "",
                "artist": release_row[1] or "" if release_row else "",
                "release_year": release_row[2] if release_row else None,
                "tracks": tracks,
            }

    except Exception as e:
        logger.error(f"[get_cached_release_metadata] {e}")
        return None


# ============================================================
# CACHE WRITE
# ============================================================

def cache_release_metadata(release_id: str, metadata: Dict[str, Any]) -> None:
    try:
        tracks = metadata.get("tracks") or []

        with db_session() as session:

            session.execute(
                text("""
                    INSERT INTO musicbrainz_releases
                    (release_id, release_title, artist, release_year)
                    VALUES (:release_id, :title, :artist, :year)
                    ON CONFLICT (release_id) DO UPDATE SET
                        release_title = EXCLUDED.release_title,
                        artist = EXCLUDED.artist,
                        release_year = EXCLUDED.release_year
                """),
                {
                    "release_id": release_id,
                    "title": metadata.get("release_title"),
                    "artist": metadata.get("artist"),
                    "year": metadata.get("release_year"),
                },
            )

            session.execute(
                text("DELETE FROM musicbrainz_release_tracks WHERE release_id = :release_id"),
                {"release_id": release_id},
            )

            for t in tracks:
                session.execute(
                    text("""
                        INSERT INTO musicbrainz_release_tracks (
                            release_id,
                            disc_number,
                            track_number,
                            track_title,
                            track_artist,
                            duration,
                            isrc,
                            recording_title,
                            recording_mbid
                        )
                        VALUES (:release_id, :disc_number, :track_number, :title,
                                :artist, :duration, :isrc, :recording_title, :recording_mbid)
                    """),
                    {
                        "release_id": release_id,
                        "disc_number": t.get("disc_number"),
                        "track_number": t.get("track_number"),
                        "title": t.get("title"),
                        "artist": t.get("artist"),
                        "duration": t.get("duration"),
                        "isrc": t.get("isrc"),
                        "recording_title": t.get("recording_title"),
                        "recording_mbid": t.get("recording_mbid"),
                    },
                )

    except Exception as e:
        logger.error(f"[cache_release_metadata] {e}")

def get_active_musicbrainz_releases() -> List[Dict[str, Any]]:
    """Get all active releases with progress information."""
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT id, release_id, release_title, artist, release_year,
                           total_tracks, discovered_count, organized_count,
                           finalized_count, status, monitoring_folder_path,
                           created_at, updated_at
                    FROM musicbrainz_releases
                    WHERE status IN ('active', 'finalizing')
                    ORDER BY created_at DESC
                """)
            )

            rows = result.fetchall()
            return [dict(row._mapping) for row in rows]

    except Exception as e:
        logger.error(f"[GET_ACTIVE] Error fetching active releases: {e}")
        return []
