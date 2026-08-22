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
    """Check which tracks are in the MusicBrainz release but missing from the library."""
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
        ).fetchall()
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
        title = r.get("title") if hasattr(r, "get") else r[1]
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
        })

    return {"missing_tracks": missing, "missing_count": len(missing), "mb_total": mb_total, "library_count": library_count}


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
        ).fetchall()

    if not mb_mbid:
        return {"mismatches": [], "mismatch_count": 0, "library_count": len(library_rows)}

    mb_release = fetch_musicbrainz_release_metadata(mb_mbid)
    if not mb_release:
        return {"mismatches": [], "mismatch_count": 0, "library_count": len(library_rows)}

    lib_by_tracknum: dict[tuple[Any, Any], dict[str, Any]] = {}
    for r in library_rows:
        if hasattr(r, "get"):
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
        else:
            tn = r[2]
            dn = r[3] or 1
            try:
                tn_int = int(str(tn).split("/")[0].strip()) if tn else None
            except (ValueError, TypeError):
                tn_int = None
            lib_by_tracknum[(int(dn or 1), tn_int)] = {
                "id": r[0],
                "title": r[1],
                "duration": r[4],
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
