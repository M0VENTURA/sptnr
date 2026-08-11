"""Album detail/comparison services — missing tracks, title mismatches, library tracks.

Extracted from the old monolithic app.py.
"""

from __future__ import annotations

import logging
import unicodedata
import re
from typing import Any

from sqlalchemy import text
from db.engine import db_session
from db.utils import get_db_connection, row_get  # TODO: migrate

logger = logging.getLogger(__name__)


def get_library_tracks(artist: str, album: str) -> list[dict]:
    """Get all library tracks for a specific artist/album."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, title, track_number, disc_number, file_path, duration FROM tracks "
            "WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s AND album = %s "
            "ORDER BY COALESCE(disc_number, '1'), track_number",
            (artist, album),
        )
        return [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()


def get_missing_tracks(artist: str, album: str) -> dict:
    """Check which tracks are in the MusicBrainz release but missing from the library."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        # Get MBID
        cursor.execute(
            "SELECT musicbrainz_album_mbid FROM tracks WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s AND album = %s "
            "AND musicbrainz_album_mbid IS NOT NULL AND TRIM(musicbrainz_album_mbid) != '' LIMIT 1",
            (artist, album),
        )
        row = cursor.fetchone()
        # Rows are RealDictRow (dict-like); never index by position.
        mb_mbid = row.get("musicbrainz_album_mbid") if row else None

        # Fetch library tracks (mbid is the recording MBID column — beets_mbid
        # does not exist in the current schema).
        cursor.execute(
            "SELECT id, title, track_number, disc_number, mbid FROM tracks "
            "WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s AND album = %s",
            (artist, album),
        )
        library_rows = cursor.fetchall()
        library_count = len(library_rows)
    finally:
        conn.close()

    if not mb_mbid:
        # Fallback: search MB for a release
        try:
            from api_clients.musicbrainz_http import (
                MusicBrainzHttpClient,
                escape_lucene_special_chars,
            )
            from helpers.normalization_service import normalize_title_for_lucene_query
            query = (
                f'artist:"{escape_lucene_special_chars(artist)}" '
                f'AND release:"{escape_lucene_special_chars(album)}"'
            )
            releases = MusicBrainzHttpClient(enabled=True).search_releases(query, limit=5)
            if releases:
                mb_mbid = releases[0].get("id")
        except Exception as exc:
            logger.debug("MB release search fallback failed: %s", exc)

    if not mb_mbid:
        return {"missing_tracks": [], "missing_count": 0, "mb_total": 0, "library_count": library_count}

    # Fetch MB release tracklist
    try:
        from services.enrichment.musicbrainz_service import fetch_musicbrainz_release_metadata
        mb_release = fetch_musicbrainz_release_metadata(mb_mbid)
    except Exception:
        mb_release = None

    if not mb_release:
        return {"missing_tracks": [], "missing_count": 0, "mb_total": 0, "library_count": library_count}

    mb_tracks = mb_release.get("tracks", [])
    mb_total = len(mb_tracks)

    # Build library entries: a set of normalized titles (for title-based
    # matching) AND a lookup by (disc, track_number) — position-first per the
    # alignment rules: a local file occupying the same Disc # + Track # slot
    # counts as present even when the title differs slightly (that discrepancy
    # belongs in /corrections as WRONG_TRACK_NAME, not in "missing").
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

        # Position-first: is the Disc # / Track # slot occupied locally?
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


def get_title_mismatches(artist: str, album: str) -> dict:
    """Compare library track titles against the full MusicBrainz release tracklist."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT musicbrainz_album_mbid FROM tracks WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s AND album = %s "
            "AND musicbrainz_album_mbid IS NOT NULL AND TRIM(musicbrainz_album_mbid) != '' LIMIT 1",
            (artist, album),
        )
        row = cursor.fetchone()
        # Rows are RealDictRow (dict-like); never index by position.
        mb_mbid = row.get("musicbrainz_album_mbid") if row else None

        cursor.execute(
            "SELECT id, title, track_number, disc_number, duration FROM tracks "
            "WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s AND album = %s",
            (artist, album),
        )
        library_rows = cursor.fetchall()
    finally:
        conn.close()

    if not mb_mbid:
        return {"mismatches": [], "mismatch_count": 0, "library_count": len(library_rows)}

    from services.enrichment.musicbrainz_service import fetch_musicbrainz_release_metadata
    mb_release = fetch_musicbrainz_release_metadata(mb_mbid)
    if not mb_release:
        return {"mismatches": [], "mismatch_count": 0, "library_count": len(library_rows)}

    # Build lib lookup by (disc, track_number)
    lib_by_tracknum: dict[tuple, dict] = {}
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
