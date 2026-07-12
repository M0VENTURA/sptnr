"""MusicBrainz release metadata cache.

Stores and retrieves MusicBrainz release metadata (release info + track list)
from ``musicbrainz_releases`` and ``musicbrainz_release_tracks`` tables.

Used by the download matching pipeline to avoid repeated MusicBrainz
API calls for the same release.
"""

from typing import Optional, Dict, Any, List
import logging

from db.context import db_cursor
from db.utils import row_get

logger = logging.getLogger(__name__)

def get_cached_release_metadata(release_id: str) -> Optional[Dict[str, Any]]:
    try:
        with db_cursor() as (_, cursor):

            cursor.execute("""
                SELECT release_title, artist, release_year
                FROM musicbrainz_releases
                WHERE release_id = %s
                LIMIT 1
            """, (release_id,))

            release_row = cursor.fetchone()

            cursor.execute("""
                SELECT disc_number, track_number, track_title, track_artist,
                       duration, isrc, recording_title, recording_mbid
                FROM musicbrainz_release_tracks
                WHERE release_id = %s
                ORDER BY COALESCE(disc_number, 1),
                         COALESCE(track_number, 999999)
            """, (release_id,))

            track_rows = cursor.fetchall()

            if not release_row and not track_rows:
                return None

            tracks = []
            for r in track_rows:
                tracks.append({
                    "disc_number": row_get(r, "disc_number", 0, 1),
                    "track_number": row_get(r, "track_number", 1),
                    "title": row_get(r, "track_title", 2, ""),
                    "artist": row_get(r, "track_artist", 3, ""),
                    "duration": row_get(r, "duration", 4),
                    "isrc": row_get(r, "isrc", 5),
                    "recording_title": row_get(r, "recording_title", 6),
                    "recording_mbid": row_get(r, "recording_mbid", 7),
                })

            return {
                "release_title": row_get(release_row, "release_title", 0, ""),
                "artist": row_get(release_row, "artist", 1, ""),
                "release_year": row_get(release_row, "release_year", 2),
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

        with db_cursor(commit=True) as (_, cursor):

            cursor.execute("""
                INSERT INTO musicbrainz_releases
                (release_id, release_title, artist, release_year)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (release_id) DO UPDATE SET
                    release_title = EXCLUDED.release_title,
                    artist = EXCLUDED.artist,
                    release_year = EXCLUDED.release_year
            """, (
                release_id,
                metadata.get("release_title"),
                metadata.get("artist"),
                metadata.get("release_year"),
            ))

            cursor.execute("""
                DELETE FROM musicbrainz_release_tracks
                WHERE release_id = %s
            """, (release_id,))

            for t in tracks:
                cursor.execute("""
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
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    release_id,
                    t.get("disc_number"),
                    t.get("track_number"),
                    t.get("title"),
                    t.get("artist"),
                    t.get("duration"),
                    t.get("isrc"),
                    t.get("recording_title"),
                    t.get("recording_mbid"),
                ))

    except Exception as e:
        logger.error(f"[cache_release_metadata] {e}")

def get_active_musicbrainz_releases() -> List[Dict[str, Any]]:
    """Get all active releases with progress information."""
    try:
        # Use your context manager to handle connection/cursor
        with db_cursor() as (_, cursor):
            cursor.execute("""
                SELECT id, release_id, release_title, artist, release_year, 
                       total_tracks, discovered_count, organized_count, 
                       finalized_count, status, monitoring_folder_path, 
                       created_at, updated_at
                FROM musicbrainz_releases
                WHERE status IN ('active', 'finalizing')
                ORDER BY created_at DESC
            """)
            
            # Use fetchall and map to a list of dicts here so the service is clean
            rows = cursor.fetchall()
            return [dict(zip([d[0] for d in cursor.description], row)) for row in rows]
            
    except Exception as e:
        logger.error(f"[GET_ACTIVE] Error fetching active releases: {e}")
        return []
