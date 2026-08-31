"""MusicBrainz release metadata service.

Provides release-level queries from the cached MusicBrainz tables:
- ``get_release_details`` – Full release metadata including track list.
- ``get_active_releases_with_progress`` – Releases with download status.
- ``get_cached_missing_releases`` – Releases identified as missing.

All data comes from ``musicbrainz_releases`` / ``musicbrainz_release_tracks`` tables.
"""

from __future__ import annotations

import re
from typing import Any

import structlog
from sqlalchemy import text

from db.engine import db_session
from db.repositories.musicbrainz_cache import get_active_musicbrainz_releases

logger = structlog.get_logger(__name__)


def get_release_details(release_id: str) -> dict[str, Any] | None:
    """Get full release metadata including track list from cache."""
    try:
        with db_session() as session:
            result = session.execute(
                text("SELECT * FROM musicbrainz_releases WHERE release_id = :id"),
                {"id": release_id}
            )
            release_row = result.fetchone()
            if not release_row:
                return None
            release = dict(release_row._mapping)

            result = session.execute(
                text("SELECT * FROM musicbrainz_release_tracks WHERE release_id = :id"),
                {"id": release_id}
            )
            tracks = [dict(r._mapping) for r in result.fetchall()]

        return {"release": release, "tracks": tracks}
    except Exception as exc:
        logger.error("Failed to get release details", release_id=release_id, error=str(exc))
        return None


def get_cached_missing_releases(artist: str) -> tuple[dict[str, Any], int]:
    """Return cached missing releases for an artist.

    Rows whose release title now EXISTS in the library are filtered out (and
    deleted) so a single/album added since the last missing-releases scan
    disappears from the "Missing" list immediately — the reported "some
    singles missing releases aren't disappearing when the release is added
    and it will show multiple copies".
    """
    if not artist:
        return {"success": False, "error": "Artist is required"}, 400

    try:
        # Normalisation shared with the missing-release builder: strip
        # edition markers / punctuation so "Queen Dies (Single)" ≈ "Queen
        # Dies" and a library album named slightly differently still matches.
        def _norm(value: str) -> str:
            return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()

        with db_session() as session:
            # Library album titles for this artist (normalised).
            lib_albums = {
                _norm(str(r[0] or ""))
                for r in session.execute(
                    text("""
                        SELECT DISTINCT album FROM tracks
                        WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
                    """),
                    {"artist": artist},
                ).fetchall() or []
                if r[0]
            }

            # Library TRACK titles — a SINGLE whose track is already on an
            # owned album is not missing (e.g. "Queen Dies" when the album
            # containing it is in the library).  This mirrors the insert-time
            # rule in the missing-releases builders, and also cleans up stale
            # rows persisted before that rule existed.
            lib_track_titles = {
                _norm(str(r[0] or ""))
                for r in session.execute(
                    text("""
                        SELECT DISTINCT title FROM tracks
                        WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
                          AND title IS NOT NULL AND TRIM(title) <> ''
                    """),
                    {"artist": artist},
                ).fetchall() or []
                if r[0]
            }

            result = session.execute(
                text("""
                    SELECT release_id, title, primary_type, first_release_date,
                           cover_art_url, category, last_checked
                    FROM missing_releases
                    WHERE artist = :artist
                    ORDER BY first_release_date DESC
                """),
                {"artist": artist}
            )
            rows = [dict(r._mapping) for r in result.fetchall()]

            # Drop + delete cached rows already in the library.
            still_missing = []
            for r in rows:
                title_norm = _norm(str(r.get("title") or ""))
                category = str(r.get("category") or "")
                covered_by_library_track = (
                    category.lower() == "single"
                    and title_norm
                    and title_norm in lib_track_titles
                )
                if (title_norm and title_norm in lib_albums) or covered_by_library_track:
                    try:
                        session.execute(
                            text("DELETE FROM missing_releases WHERE release_id = :rid AND artist = :artist"),
                            {"rid": r.get("release_id", ""), "artist": artist},
                        )
                    except Exception:
                        pass
                    continue
                still_missing.append(r)

        return {
            "artist": artist,
            "missing": [
                {
                    "id": r.get("release_id", ""),
                    "title": r.get("title", ""),
                    "primary_type": r.get("primary_type", "Album"),
                    "first_release_date": str(r.get("first_release_date", "")),
                    "cover_art_url": r.get("cover_art_url", ""),
                    "category": r.get("category", "Album"),
                    "last_checked": str(r.get("last_checked", "")),
                } for r in still_missing
            ],
            "from_cache": True
        }, 200

    except Exception as exc:
        logger.error("Failed to get cached missing releases", artist=artist, error=str(exc))
        return {"success": False, "error": str(exc)}, 500


def get_active_releases_with_progress() -> list[dict[str, Any]]:
    """Return active releases with calculated download progress."""
    releases = get_active_musicbrainz_releases() or []
    
    for r in releases:
        total = r.get("total_tracks", 0) or 0
        discovered = r.get("discovered_count", 0) or 0
        r["progress_percent"] = int((discovered / total * 100) if total > 0 else 0)
        
    return releases
