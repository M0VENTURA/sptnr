"""Track repository queries."""

from __future__ import annotations
import os
import logging
logger = logging.getLogger(__name__)


from datetime import datetime
from typing import Any, Iterable, Set

from helpers.normalization_service import (
    normalize_artist,
    normalize_title_for_lookup,
    normalize_album,
)

from sqlalchemy import text

from db.engine import db_session

from services.metadata.tag_file_service import ( 
    update_file_tags
)


def upsert_track_payload(
    track_payload: dict[str, Any],
) -> bool:
    """Persist a Navidrome track payload via the popularity repository.

    Delegates to ``popularity_repository.save_to_db`` which handles
    schema-aware dynamic upserts and protects popularity/scoring columns
    when ``_navidrome_sync`` is set in the payload.
    """
    from db.repositories.popularity_repository import save_to_db
    return save_to_db(track_payload)

def insert_or_update_track(track_id: str, track_data: dict[str, Any]) -> None:
    """Insert or update a track's popularity/enrichment data.

    Delegates to ``popularity_repository.save_to_db`` which handles
    schema-aware dynamic upserts for all known column names.
    """
    from db.repositories.popularity_repository import save_to_db
    track_data["id"] = track_id
    save_to_db(track_data)


def get_tracks_by_artist(artist_id: str) -> list[Any]:
    """Return all tracks for an artist_id."""
    with db_session() as session:
        result = session.execute(
            text("SELECT * FROM tracks WHERE artist_id = :artist_id"),
            {"artist_id": artist_id},
        )
        return result.fetchall() or []


def get_top_tracks(limit: int = 10) -> list[Any]:
    """Return top tracks ordered by final_score descending."""
    with db_session() as session:
        result = session.execute(
            text("""
                SELECT title, final_score, stars
                FROM tracks
                ORDER BY final_score DESC NULLS LAST
                LIMIT :limit
            """),
            {"limit": limit},
        )
        return result.fetchall() or []


def get_all_ratings() -> list[dict[str, Any]]:
    """Return all tracks with a non-null star rating > 0.

    Returns:
        List of dicts with ``id`` and ``stars`` keys.
    """
    try:
        with db_session() as session:
            result = session.execute(
                text("SELECT id, stars FROM tracks WHERE stars IS NOT NULL AND stars > 0")
            )
            return [dict(row._mapping) for row in result.fetchall() or []]
    except Exception as exc:
        logger.error("Failed to fetch rated tracks: %s", exc, exc_info=True)
        return []


def get_current_track_rating(track_id: str) -> int:
    """Return current track star rating, or 0 if unavailable."""
    try:
        with db_session() as session:
            result = session.execute(
                text("SELECT stars FROM tracks WHERE id = :id"),
                {"id": track_id},
            )
            row = result.fetchone()
            return int(row[0] or 0) if row else 0
    except Exception as exc:
        logger.debug(
            "Failed to get current rating for track %s: %s",
            track_id,
            exc,
        )
    return 0


def delete_tracks_by_id(track_ids: Set[str], *, context: str, session: Any | None = None) -> int:
    """Delete tracks by ID.
    
    Args:
        track_ids: Set of track IDs to delete.
        context: Description for logging (e.g. artist name).
        session: Optional SQLAlchemy session. If None, creates one.
        
    Returns:
        Number of tracks deleted.
    """
    if not track_ids:
        return 0
    
    def _do_delete(sess):
        placeholders = ", ".join([f":id_{i}" for i in range(len(track_ids))])
        params = {f"id_{i}": tid for i, tid in enumerate(track_ids)}
        sess.execute(text(f"DELETE FROM tracks WHERE id IN ({placeholders})"), params)
        return len(track_ids)
    
    try:
        if session is not None:
            return _do_delete(session)
        else:
            with db_session() as sess:
                return _do_delete(sess)
    except Exception as err:
        logger.error(
            "Failed to remove stale tracks for %s: %s",
            context,
            err,
        )
        return 0

# db/repositories/tracks.py
def update_track_single_status(
    track_id: str,
    is_single: bool,
    confidence: float,
) -> None:
    """[COMPLIANT] Persist single-detection results for a track."""
    if not track_id:
        return
    with db_session() as session:
        session.execute(
            text("""
                UPDATE tracks
                SET is_single = :is_single, single_confidence = :confidence
                WHERE id = :id
            """),
            {"is_single": is_single, "confidence": confidence, "id": track_id},
        )

def clear_disc_number(artist, album, force_clear=False):
        with db_session() as session:

            # ------------------------------------------------------------------
            # Safety check (multi-disc detection)
            # ------------------------------------------------------------------
            result = session.execute(
                text("""
                    SELECT
                        COUNT(DISTINCT CASE
                            WHEN disc_number IS NOT NULL
                            AND TRIM(CAST(disc_number AS TEXT)) != ''
                            AND CAST(disc_number AS TEXT) != '0'
                            THEN CAST(disc_number AS TEXT)
                        END),
                        MAX(CASE
                            WHEN disc_number IS NOT NULL
                            AND TRIM(CAST(disc_number AS TEXT)) != ''
                            AND CAST(disc_number AS TEXT) != '0'
                            THEN CAST(disc_number AS TEXT)
                        END)
                    FROM tracks
                    WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
                    AND album = :album
                """),
                {"artist": artist, "album": album},
            )
            row = result.fetchone() or (0, None)

            distinct_disc_values = int(row[0] or 0)
            max_disc_value = str(row[1] or "").strip()

            likely_multi_disc = (
                distinct_disc_values > 1 or
                (max_disc_value.isdigit() and int(max_disc_value) > 1)
            )

            if likely_multi_disc and not force_clear:
                return {
                    "success": False,
                    "needs_manual_review": True,
                    "distinct_disc_values": distinct_disc_values,
                    "max_disc_value": max_disc_value or None,
                }, 409

            # ------------------------------------------------------------------
            # Fetch affected rows BEFORE update
            # ------------------------------------------------------------------
            result = session.execute(
                text("""
                    SELECT id, file_path
                    FROM tracks
                    WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
                    AND album = :album
                    AND disc_number IS NOT NULL
                    AND TRIM(CAST(disc_number AS TEXT)) != ''
                    AND CAST(disc_number AS TEXT) != '0'
                """),
                {"artist": artist, "album": album},
            )
            rows = result.fetchall() or []

            if not rows:
                return {
                    "success": True,
                    "cleared": 0,
                    "message": "No tracks found with disc_number"
                }, 200

            affected = [
                {"id": r[0], "file_path": r[1]} for r in rows
            ]

            # ------------------------------------------------------------------
            # Clear DB values
            # ------------------------------------------------------------------
            result = session.execute(
                text("""
                    UPDATE tracks
                    SET disc_number = NULL
                    WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
                    AND album = :album
                    AND disc_number IS NOT NULL
                    AND TRIM(CAST(disc_number AS TEXT)) != ''
                    AND CAST(disc_number AS TEXT) != '0'
                """),
                {"artist": artist, "album": album},
            )
            cleared_count = result.rowcount

        # ✅ Outside DB context → handle file updates
        files_updated = 0

        try:
            for row in affected:
                path = row["file_path"]
                if path and os.path.exists(path):
                    try:
                        update_file_tags(path, {"disc_number": None})
                        files_updated += 1
                    
                    except Exception:
                        logger.debug(
                            "Failed updating tags for %s",
                            path,
                        )
        except Exception:
            pass

        return {
            "success": True,
            "cleared": cleared_count,
            "files_updated": files_updated,
        }, 200
    
# ---------------------------------------------------------------------------
# Merged:
#   find_track_in_collection()
#   find_library_track()
# ---------------------------------------------------------------------------


def find_library_track(
    *,
    artist: str,
    title: str,
    album: str | None = None,
    strict_album: bool = True,
) -> dict[str, Any] | None:
    """
    Find an existing track in the local collection.

    Matching strategy:

    1. Artist + Title + Album (if strict_album=True)
    2. Artist + Title (fallback)

    Returns:
        Track row dictionary if found.
        None if not found.
    """

    if not artist or not title:
        return None

    artist_norm = normalize_artist(
        artist
    )

    title_norm = normalize_title_for_lookup(
        title
    )

    album_norm = (
        normalize_album(album)
        if album
        else None
    )

    try:
        with db_session() as session:

            # ---------------------------------------------------------
            # PASS 1
            # Artist + Title + Album
            # ---------------------------------------------------------

            if strict_album and album_norm:

                result = session.execute(
                    text("""
                        SELECT *
                        FROM tracks
                        WHERE LOWER(
                            COALESCE(
                                NULLIF(album_artist, ''),
                                artist
                            )
                        ) = :artist
                          AND LOWER(title) = :title
                          AND LOWER(
                                COALESCE(album, '')
                          ) = :album
                          AND file_path IS NOT NULL
                          AND file_path NOT LIKE '__queued_for_download__%%'
                        LIMIT 1
                    """),
                    {
                        "artist": artist_norm,
                        "title": title_norm,
                        "album": album_norm,
                    },
                )

                row = result.fetchone()

                if row:
                    return dict(row._mapping)

            # ---------------------------------------------------------
            # PASS 2
            # Artist + Title fallback
            # ---------------------------------------------------------

            result = session.execute(
                text("""
                    SELECT *
                    FROM tracks
                    WHERE LOWER(
                        COALESCE(
                            NULLIF(album_artist, ''),
                            artist
                        )
                    ) = :artist
                      AND LOWER(title) = :title
                      AND file_path IS NOT NULL
                      AND file_path NOT LIKE '__queued_for_download__%%'
                    LIMIT 1
                """),
                {
                    "artist": artist_norm,
                    "title": title_norm,
                },
            )

            row = result.fetchone()

            if not row:
                return None

            return dict(row._mapping)

    except Exception as exc:
        logger.error(
            "[find_library_track] %s",
            exc,
        )

    return None
