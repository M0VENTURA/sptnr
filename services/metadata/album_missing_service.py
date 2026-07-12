"""Album detail/comparison services — missing tracks, title mismatches, library tracks.

Extracted from the old monolithic app.py.
"""

from __future__ import annotations

import logging
import unicodedata
import re
import time
from typing import Any

from db.utils import get_db_connection, row_get
from helpers.config_helpers import get_musicbrainz_user_agent

logger = logging.getLogger(__name__)

MUSICBRAINZ_USER_AGENT = get_musicbrainz_user_agent()


def get_library_tracks(artist: str, album: str) -> list[dict]:
    """Get all library tracks for a specific artist/album."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, title, track_number, disc_number, file_path, duration FROM tracks "
            "WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s AND album = %s "
            "ORDER BY COALESCE(disc_number, 1), track_number",
            (artist, album),
        )
        return [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()


def get_missing_tracks(artist: str, album: str) -> dict:
    """Check which tracks are in the MusicBrainz release but missing from the library."""
    import requests
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
        mb_mbid = row[0] if row else None
        if not hasattr(row, "get"):
            pass
        elif row:
            mb_mbid = row.get("musicbrainz_album_mbid")

        # Fetch library tracks
        cursor.execute(
            "SELECT id, title, track_number, disc_number, mbid, beets_mbid FROM tracks "
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
            headers = {"User-Agent": MUSICBRAINZ_USER_AGENT}
            search_url = "https://musicbrainz.org/ws/2/release/"
            from urllib.parse import quote
            resp = requests.get(
                search_url,
                params={"query": f'artist:"{quote(artist)}" AND release:"{quote(album)}"', "fmt": "json", "limit": 5},
                headers=headers, timeout=10,
            )
            releases = resp.json().get("releases", [])
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

    # Build library entries set
    lib_norm = set()
    for r in library_rows:
        title = r.get("title") if hasattr(r, "get") else r[1]
        if title:
            norm = unicodedata.normalize("NFKD", title).lower()
            norm = re.sub(r"[^a-z0-9]+", " ", norm).strip()
            lib_norm.add(norm)

    missing = []
    for mt in mb_tracks:
        mb_title = mt.get("title", "")
        if not mb_title:
            continue
        norm = unicodedata.normalize("NFKD", mb_title).lower()
        norm = re.sub(r"[^a-z0-9]+", " ", norm).strip()
        if norm not in lib_norm:
            missing.append({
                "title": mb_title,
                "track_number": mt.get("track_number"),
                "disc_number": mt.get("disc_number", 1),
                "recording_mbid": mt.get("recording_mbid"),
            })

    return {"missing_tracks": missing, "missing_count": len(missing), "mb_total": mb_total, "library_count": library_count}


def get_title_mismatches(artist: str, album: str) -> dict:
    """Compare library track titles against the full MusicBrainz release tracklist."""
    import requests
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT musicbrainz_album_mbid FROM tracks WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s AND album = %s "
            "AND musicbrainz_album_mbid IS NOT NULL AND TRIM(musicbrainz_album_mbid) != '' LIMIT 1",
            (artist, album),
        )
        row = cursor.fetchone()
        mb_mbid = row[0] if row else None
        if hasattr(row, "get") and row:
            mb_mbid = row.get("musicbrainz_album_mbid")

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
