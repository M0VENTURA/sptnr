"""Album tag correction and consistency service.

Detects albums whose tracks have inconsistent album-level metadata values
(e.g. different album_artist, year, label, genres, etc.) that cause
Navidrome to display the same album as multiple entries.

Built from logic migrated from the old monolithic app.py.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text
from db.engine import db_session
from db.utils import get_db_connection, row_get  # TODO: migrate

logger = logging.getLogger(__name__)


def _table_columns(cursor: Any, table_name: str) -> set[str]:
    """Return the column names of a table via a psycopg2 cursor.

    ``db.utils`` has no ``get_table_columns`` (it lives in
    ``db.schema_helpers`` and takes a SQLAlchemy session, not a cursor) —
    importing it raised ImportError and took the whole corrections page
    down with a silent "Database error" banner.
    """
    try:
        cursor.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = %s",
            (table_name,),
        )
        # psycopg2 RealDictRow rows are dict-like — ``row[0]`` raises KeyError,
        # which made this always return an empty set and silently disable the
        # whole inconsistency scan. Use ``row_get`` which handles dict-like and
        # tuple rows.
        return {
            str(row_get(row, "column_name"))
            for row in cursor.fetchall()
            if row_get(row, "column_name")
        }
    except Exception:
        return set()


# Fields whose values MUST be consistent across all tracks of the same album.
# Navidrome uses ALBUM + ALBUMARTIST as the primary album key; inconsistencies
# in these fields are the most common cause of "split album" problems.
ALBUM_CONSISTENCY_FIELDS: list[tuple[str, str]] = [
    ("album_artist", "Album Artist"),
    ("musicbrainz_album_mbid", "MusicBrainz Album ID"),
    ("musicbrainz_releasegroupid", "MusicBrainz Release Group ID"),
    ("releasetype", "Release Type"),
    ("releasestatus", "Release Status"),
    ("releasecountry", "Release Country"),
    ("compilation", "Compilation"),
    ("genres", "Genre"),
    ("mood", "Mood"),
    ("year", "Year"),
    ("label", "Label"),
    ("recordlabel", "Record Label"),
    ("tracktotal", "Track Total"),
    ("disctotal", "Disc Total"),
    ("grouping", "Grouping"),
    ("media", "Media"),
    ("albumversion", "Album Version"),
    ("discsubtitle", "Disc Subtitle"),
    ("script", "Script"),
    ("replaygain_album_gain", "ReplayGain Album Gain"),
    ("replaygain_album_peak", "ReplayGain Album Peak"),
]


def get_album_tag_inconsistencies(artist_filter: str | None = None) -> list[dict[str, Any]]:
    """Return albums that have inconsistent album-level tag values across their tracks.

    Returns a list of dicts:
        {album_artist, album, track_count,
         inconsistencies: [{field, field_label, values: [{value, count, track_ids}]}]}
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        track_columns = _table_columns(cursor, "tracks")
        has_album_artist = "album_artist" in track_columns
        artist_expr = "COALESCE(NULLIF(album_artist, ''), artist)" if has_album_artist else "artist"

        # Only check fields whose columns actually exist — on a fresh bootstrap
        # many album-level columns are missing and the whole query would fail
        # with "column does not exist" (silently swallowed -> page shows nothing).
        effective_fields = [
            (f, lbl) for f, lbl in ALBUM_CONSISTENCY_FIELDS
            if f in track_columns
        ]

        having_parts = []
        for field, _ in effective_fields:
            having_parts.append(f"COUNT(DISTINCT COALESCE(CAST({field} AS TEXT), '')) > 1")
        if not having_parts:
            return []
        having_sql = " OR ".join(having_parts)

        where_sql = "WHERE album IS NOT NULL AND TRIM(album) != ''"
        params: list[str] = []
        if artist_filter:
            where_sql += f" AND {artist_expr} = %s"
            params.append(artist_filter)

        cursor.execute(
            f"""
            SELECT {artist_expr} AS album_artist, album, COUNT(*) AS track_count
            FROM tracks {where_sql}
            GROUP BY {artist_expr}, album
            HAVING COUNT(*) >= 2 AND ({having_sql})
            ORDER BY {artist_expr}, album
            """,
            params or None,
        )
        flagged_albums = cursor.fetchall()

        results = []
        for row in flagged_albums:
            row_dict = dict(row) if hasattr(row, "keys") else {"album_artist": row[0], "album": row[1], "track_count": row[2]}
            aa = row_dict.get("album_artist") or ""
            al = row_dict.get("album") or ""
            tc = int(row_dict.get("track_count") or 0)

            try:
                cursor.execute(
                    f"SELECT id, {', '.join(f for f, _ in effective_fields)} FROM tracks WHERE {artist_expr} = %s AND album = %s",
                    (aa, al),
                )
                detail_rows = cursor.fetchall()
            except Exception:
                continue

            col_names = ["id"] + [f for f, _ in effective_fields]
            inconsistencies = []
            for field, field_label in effective_fields:
                col_idx = col_names.index(field)
                value_map: dict[str, dict] = {}
                for dr in detail_rows:
                    track_id = dr[0] if not hasattr(dr, "get") else dr.get("id")
                    val = dr[col_idx] if not hasattr(dr, "get") else dr.get(field)
                    val_str = str(val or "").strip()
                    if val_str not in value_map:
                        value_map[val_str] = {"value": val_str, "count": 0, "track_ids": []}
                    value_map[val_str]["count"] += 1
                    if track_id is not None:
                        value_map[val_str]["track_ids"].append(str(track_id))

                non_empty = {v: d for v, d in value_map.items() if v and v != ""}
                if len(non_empty) >= 2:
                    inconsistencies.append({
                        "field": field,
                        "field_label": field_label,
                        "values": sorted(non_empty.values(), key=lambda x: -x["count"]),
                    })

            if inconsistencies:
                results.append({
                    "album_artist": aa,
                    "album": al,
                    "track_count": tc,
                    "inconsistencies": inconsistencies,
                })

        return results
    except Exception as exc:
        logger.warning("Failed to get album tag inconsistencies: %s", exc, exc_info=True)
        return []
    finally:
        conn.close()


def fix_album_field(album_artist: str, album: str, field: str, value: Any) -> tuple[int, int]:
    """Apply a single field value to all tracks in an album.

    Returns (tracks_updated, files_updated).
    """
    allowed = {f for f, _ in ALBUM_CONSISTENCY_FIELDS}
    if field not in allowed:
        logger.warning("Field %s not in allowed consistency fields", field)
        return 0, 0

    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT id, file_path FROM tracks WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s AND album = %s",
            (album_artist, album),
        )
        affected = [dict(r) if hasattr(r, "keys") else {"id": r[0], "file_path": r[1]} for r in cursor.fetchall()]
        if not affected:
            return 0, 0

        cursor.execute(
            f"UPDATE tracks SET {field} = %s WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s AND album = %s",
            (value, album_artist, album),
        )
        conn.commit()
        updated_count = len(affected)

        files_updated = 0
        from services.metadata.tag_file_service import write_tags_to_file
        for track in affected:
            fp = str(track.get("file_path") or "").strip()
            if fp:
                try:
                    write_tags_to_file(fp, {field: value})
                    files_updated += 1
                except Exception:
                    pass

        return updated_count, files_updated
    except Exception as exc:
        logger.error("fix_album_field failed: %s", exc, exc_info=True)
        return 0, 0
    finally:
        conn.close()
