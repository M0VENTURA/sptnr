"""Album detail/comparison services — missing tracks, title mismatches, library tracks.

Extracted from the old monolithic app.py.
"""

from __future__ import annotations

import unicodedata
import re
from typing import Any

import structlog
from sqlalchemy import text

from db.engine import db_session
from services.enrichment.musicbrainz_service import (
    get_shared_mb_client,
    fetch_musicbrainz_release_metadata,
)
from api_clients.musicbrainz_http import escape_lucene_special_chars
from helpers.normalization_service import normalize_title_for_lucene_query

logger = structlog.get_logger(__name__)


_ALBUM_SCOPE_WHERE = (
    "LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist) "
    "AND LOWER(COALESCE(album, '')) = LOWER(:album)"
)


def get_library_tracks(artist: str, album: str) -> list[dict[str, Any]]:
    """Get all library tracks for a specific artist/album."""
    with db_session() as session:
        result = session.execute(
            text(
                f"SELECT id, title, track_number, disc_number, file_path, duration FROM tracks "
                f"WHERE {_ALBUM_SCOPE_WHERE} "
                "ORDER BY COALESCE(disc_number, '1'), track_number"
            ),
            {"artist": artist, "album": album},
        )
        return [dict(r) for r in result.mappings().all()]


def get_missing_tracks(artist: str, album: str) -> dict[str, Any]:
    """Check which tracks are in the MusicBrainz release but missing from the library.

    Computes the missing set from the MusicBrainz release tracklist, persists
    each missing track to ``missing_album_tracks`` (so the list survives page
    refreshes until the track is downloaded or rejected), and returns only the
    tracks that are still missing AND not rejected (``ignored = FALSE``).
    """
    with db_session() as session:
        row = session.execute(
            text(
                "SELECT musicbrainz_album_mbid FROM tracks "
                f"WHERE {_ALBUM_SCOPE_WHERE} "
                "AND musicbrainz_album_mbid IS NOT NULL AND TRIM(musicbrainz_album_mbid) != '' LIMIT 1"
            ),
            {"artist": artist, "album": album},
        ).fetchone()
        mb_mbid = row[0] if row else None

        library_rows = session.execute(
            text(
                "SELECT id, title, track_number, disc_number, mbid FROM tracks "
                f"WHERE {_ALBUM_SCOPE_WHERE}"
            ),
            {"artist": artist, "album": album},
        ).mappings().all()
        library_count = len(library_rows)

    if not mb_mbid:
        # ✅ Use shared MusicBrainz client singleton instead of raw instantiation
        try:
            query = (
                f'artist:"{escape_lucene_special_chars(artist)}" '
                f'AND release:"{escape_lucene_special_chars(album)}"'
            )
            releases = get_shared_mb_client().search_releases(query, limit=5)
            if releases:
                mb_mbid = releases[0].get("id")
        except Exception as exc:
            logger.debug("MB release search fallback failed", error=str(exc))

    if not mb_mbid:
        return {"missing_tracks": [], "missing_count": 0, "mb_total": 0, "library_count": library_count}

    try:
        mb_release = fetch_musicbrainz_release_metadata(mb_mbid)
    except Exception:
        mb_release = None

    if not mb_release:
        return {"missing_tracks": [], "missing_count": 0, "mb_total": 0, "library_count": library_count}

    mb_tracks = mb_release.get("tracks", [])
    mb_total = len(mb_tracks)

    lib_norm = set()
    lib_by_position: dict[tuple[int, str], str] = {}
    for r in library_rows:
        title = r.get("title") or ""
        if title:
            norm = unicodedata.normalize("NFKD", title).lower()
            norm = re.sub(r"[^a-z0-9]+", " ", norm).strip()
            lib_norm.add(norm)
        disc = int(r.get("disc_number") or 1) if r.get("disc_number") not in (None, "", "0", 0) else 1
        tn = str(r.get("track_number") or "").strip()
        if tn:
            lib_by_position[(disc, tn)] = title or ""

    missing = []
    for mt in mb_tracks:
        mb_title = mt.get("title", "")
        if not mb_title:
            continue
        norm = unicodedata.normalize("NFKD", mb_title).lower()
        norm = re.sub(r"[^a-z0-9]+", " ", norm).strip()

        mb_disc = int(mt.get("disc_number") or 1)
        mb_num = str(mt.get("track_number") or "").strip()
        position_occupied = bool(mb_num and (mb_disc, mb_num) in lib_by_position)
        if position_occupied or norm in lib_norm:
            continue

        missing.append({
            "title": mb_title,
            "track_number": mt.get("track_number"),
            "disc_number": mt.get("disc_number", 1),
            "recording_mbid": mt.get("recording_mbid"),
            "track_artist": mt.get("artist") or artist,
            "year": mb_release.get("release_year") or mb_release.get("year"),
            "release_id": mb_mbid,
            "duration": mt.get("duration"),
        })

    # Persist missing tracks so they survive refreshes.  A track already
    # present (by title + disc position) is skipped; a track previously
    # rejected (ignored=TRUE) is deleted so it can be re-detected if the
    # user re-runs the comparison (reject is a soft dismiss, not permanent).
    try:
        _persist_missing_tracks(artist, album, missing)
    except Exception as exc:
        logger.debug("Failed to persist missing tracks", artist=artist, album=album, error=str(exc))

    # Return only tracks that are still missing AND not rejected.
    rejected_titles = _rejected_missing_titles(artist, album)
    visible = [
        m for m in missing
        if (m["track_number"], m["disc_number"], m["title"]) not in rejected_titles
    ]

    return {
        "missing_tracks": visible,
        "missing_count": len(visible),
        "mb_total": mb_total,
        "library_count": library_count,
    }


def _persist_missing_tracks(artist: str, album: str, missing: list[dict[str, Any]]) -> None:
    """Upsert the computed missing tracks into ``missing_album_tracks``.

    Any previously-persisted row for this artist/album that is NOT in the
    current missing set (e.g. the track has since been downloaded, or the
    release changed) is removed so stale rows never linger.  Rows that were
    rejected (``ignored = TRUE``) are preserved.
    """
    with db_session() as session:
        existing = session.execute(
            text(
                "SELECT id, title, track_number, disc_number, ignored "
                "FROM missing_album_tracks "
                "WHERE LOWER(artist_name) = LOWER(:artist) "
                "  AND LOWER(album_name) = LOWER(:album)"
            ),
            {"artist": artist, "album": album},
        ).mappings().all()

        current_keys = {
            (str(m.get("track_number") or ""), int(m.get("disc_number") or 1), str(m.get("title") or ""))
            for m in missing
        }

        for row in existing:
            row_key = (
                str(row.get("track_number") or ""),
                int(row.get("disc_number") or 1),
                str(row.get("title") or ""),
            )
            if row_key not in current_keys and not row.get("ignored"):
                session.execute(
                    text("DELETE FROM missing_album_tracks WHERE id = :id"),
                    {"id": row.get("id")},
                )

        for m in missing:
            tn = str(m.get("track_number") or "").strip()
            disc = int(m.get("disc_number") or 1)
            title = str(m.get("title") or "").strip()
            if not title:
                continue
            session.execute(
                text("""
                    INSERT INTO missing_album_tracks
                        (artist_name, album_name, title, track_number, disc_number,
                         track_artist, year, release_id, recording_mbid, duration)
                    VALUES
                        (:artist, :album, :title, :track_number, :disc_number,
                         :track_artist, :year, :release_id, :recording_mbid, :duration)
                    ON CONFLICT DO NOTHING
                """),
                {
                    "artist": artist,
                    "album": album,
                    "title": title,
                    "track_number": tn or None,
                    "disc_number": disc,
                    "track_artist": m.get("track_artist"),
                    "year": str(m.get("year") or "") or None,
                    "release_id": m.get("release_id"),
                    "recording_mbid": m.get("recording_mbid"),
                    "duration": m.get("duration"),
                },
            )
        session.commit()


def _rejected_missing_titles(artist: str, album: str) -> set[tuple[str, int, str]]:
    """Return the set of ``(track_number, disc_number, title)`` rejected rows."""
    try:
        with db_session() as session:
            rows = session.execute(
                text("""
                    SELECT title, track_number, disc_number
                    FROM missing_album_tracks
                    WHERE LOWER(artist_name) = LOWER(:artist)
                      AND LOWER(album_name) = LOWER(:album)
                      AND ignored = TRUE
                """),
                {"artist": artist, "album": album},
            ).mappings().all()
        return {
            (str(r.get("track_number") or ""), int(r.get("disc_number") or 1), str(r.get("title") or ""))
            for r in rows
        }
    except Exception:
        return set()


def get_title_mismatches(artist: str, album: str) -> dict[str, Any]:
    """Compare library track titles against the full MusicBrainz release tracklist."""
    with db_session() as session:
        row = session.execute(
            text(
                "SELECT musicbrainz_album_mbid FROM tracks "
                f"WHERE {_ALBUM_SCOPE_WHERE} "
                "AND musicbrainz_album_mbid IS NOT NULL AND TRIM(musicbrainz_album_mbid) != '' LIMIT 1"
            ),
            {"artist": artist, "album": album},
        ).fetchone()
        mb_mbid = row[0] if row else None

        library_rows = session.execute(
            text(
                "SELECT id, title, track_number, disc_number, duration FROM tracks "
                f"WHERE {_ALBUM_SCOPE_WHERE}"
            ),
            {"artist": artist, "album": album},
        ).mappings().all()

    if not mb_mbid:
        return {"mismatches": [], "mismatch_count": 0, "library_count": len(library_rows)}

    mb_release = fetch_musicbrainz_release_metadata(mb_mbid)
    if not mb_release:
        return {"mismatches": [], "mismatch_count": 0, "library_count": len(library_rows)}

    lib_by_tracknum: dict[tuple[Any, Any], dict[str, Any]] = {}
    for r in library_rows:
        tn = r.get("track_number")
        dn = r.get("disc_number") or 1
        try:
            tn_int = int(str(tn).split("/")[0].strip()) if tn else None
        except (ValueError, TypeError):
            tn_int = None
        lib_by_tracknum[(int(dn or 1), tn_int)] = {
            "id": r.get("id"),
            "title": r.get("title"),
            "duration": r.get("duration"),
        }

    mismatches = []
    for mt in mb_release.get("tracks", []):
        mb_title = mt.get("title", "")
        mb_disc = mt.get("disc_number", 1)
        mb_num = mt.get("track_number")
        if not mb_title or mb_num is None:
            continue
        try:
            mb_tn = int(str(mb_num).split("/")[0].strip())
        except (ValueError, TypeError):
            continue

        lib_entry = lib_by_tracknum.get((mb_disc, mb_tn))
        if lib_entry:
            lib_title = lib_entry.get("title", "")
            if lib_title:
                lib_norm = re.sub(r"[^a-z0-9]+", " ", lib_title.lower()).strip()
                mb_norm = re.sub(r"[^a-z0-9]+", " ", mb_title.lower()).strip()
                if lib_norm != mb_norm:
                    dur_tolerance = 5
                    lib_dur = lib_entry.get("duration")
                    mb_dur = mt.get("duration")
                    if lib_dur and mb_dur and abs(float(lib_dur) - float(mb_dur)) > dur_tolerance:
                        mismatch_type = "title_and_length"
                    else:
                        mismatch_type = "title"
                    mismatches.append({
                        "track_id": lib_entry["id"],
                        "library_title": lib_title,
                        "mb_title": mb_title,
                        "track_number": mb_num,
                        "disc_number": mb_disc,
                        "mismatch_type": mismatch_type,
                    })

    return {"mismatches": mismatches, "mismatch_count": len(mismatches), "library_count": len(library_rows)}
