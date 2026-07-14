"""Cover detection repository — ALL database read/write for cover detection.

Extracted from the legacy ``CoverDetector`` class to enforce the
repository-only-DB-access rule.

Every function accepts a raw DB connection (``conn``) and uses positional
``row_get`` access so it works with both ``psycopg2`` and ``psycopg2.extras``
RealDict cursors.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

from db.utils import row_get

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def track_has_original_by_artist(
    conn,
    artist: str,
    title: str,
) -> bool:
    """Return True when *artist* already has a non-cover recording of *title*."""
    if not conn or not artist or not title:
        return False
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT 1 FROM tracks
            WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(%s)
              AND LOWER(title) = LOWER(%s)
              AND COALESCE(is_cover, 0) = 0
            LIMIT 1
            """,
            (artist, title),
        )
        return cursor.fetchone() is not None
    except Exception:
        return False


def is_common_writer_for_artist(
    conn,
    writer: str,
    artist: str,
    track_artist: Optional[str] = None,
    min_count: int = 2,
) -> bool:
    """Return True when *writer* appears on at least *min_count* tracks by *artist*."""
    if not conn or not writer or not artist:
        return False

    lookup = track_artist if track_artist and not _loose_match(track_artist, artist) else artist
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT writer FROM tracks
            WHERE (LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(%s)
                   OR LOWER(artist) = LOWER(%s))
              AND writer IS NOT NULL AND writer != '' AND writer != '[]'
            """,
            (lookup, lookup),
        )
        rows = cursor.fetchall() or []
    except Exception:
        return False

    writer_norm = _loose_normalize(writer)
    count = 0
    for row in rows:
        raw = row_get(row, "writer", 0, "")
        if not raw:
            continue
        try:
            names = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(names, list):
                names = [str(names)]
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if any(_loose_normalize(str(n)) == writer_norm for n in names):
            count += 1
            if count >= min_count:
                return True
    return False


def writer_coverage_for_artist(conn, artist: str) -> float:
    """Return fraction (0.0–1.0) of tracks by *artist* that have a non-null writer field."""
    if not conn or not artist:
        return 1.0
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN writer IS NOT NULL
                         AND TRIM(CAST(writer AS TEXT)) NOT IN ('', '[]', 'null', 'None')
                    THEN 1 ELSE 0 END) AS with_writer
            FROM tracks
            WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(%s)
            """,
            (artist,),
        )
        row = cursor.fetchone()
        if row:
            total = int(row_get(row, "total", 0, 0) or 0)
            with_writer = int(row_get(row, "with_writer", 1, 0) or 0)
            return float(with_writer) / float(total) if total else 1.0
    except Exception:
        pass
    return 1.0


def get_track_writers_from_db(conn, track_id: str) -> List[str]:
    """Fetch writer JSON from the tracks table for a single track."""
    if not conn:
        return []
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT writer FROM tracks WHERE id = %s",
            (track_id,),
        )
        row = cursor.fetchone()
        if row:
            raw = row_get(row, "writer", 0, "")
            if raw:
                try:
                    w = json.loads(raw) if isinstance(raw, str) else raw
                    return w if isinstance(w, list) else [str(w)]
                except (json.JSONDecodeError, TypeError):
                    pass
    except Exception:
        pass
    return []


def is_cover_fully_confirmed(conn, track_id: str) -> bool:
    """Return True when the track already has all three cover confirmation signals:
    is_cover=1, original_cover_artist set, and cover_manual_override is not True.
    
    Once confirmed, the track should not be re-checked on subsequent scans
    unless explicitly forced.
    """
    if not conn:
        return False
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT is_cover, original_cover_artist, cover_manual_override FROM tracks WHERE id = %s",
            (track_id,),
        )
        row = cursor.fetchone()
        if not row:
            return False
        is_cover = row_get(row, "is_cover", 0, 0)
        original_artist = row_get(row, "original_cover_artist", 1, "") or ""
        manual_override = row_get(row, "cover_manual_override", 2, False)
        if manual_override:
            return True  # user-override means skip detection entirely
        if is_cover and original_artist.strip():
            return True
        return False
    except Exception:
        return False


def get_track_genres(conn, track_id: str) -> dict:
    """Return (genres, musicbrainz_genres) for a track."""
    if not conn:
        return {"genres": "", "musicbrainz_genres": ""}
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT genres, musicbrainz_genres FROM tracks WHERE id = %s",
            (track_id,),
        )
        row = cursor.fetchone()
        if row:
            return {
                "genres": row_get(row, "genres", 0, "") or "",
                "musicbrainz_genres": row_get(row, "musicbrainz_genres", 1, "") or "",
            }
    except Exception:
        pass
    return {"genres": "", "musicbrainz_genres": ""}


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def apply_cover_metadata_batch(conn, updates: List[Dict], max_retries: int = 5) -> List[str]:
    """Persist cover metadata for a batch of tracks.

    Returns the list of successfully-updated track IDs.
    Uses deterministic retry with exponential back-off.
    """
    if not conn or not updates:
        return []

    rows = sorted(
        [dict(u) for u in updates if u.get("track_id")],
        key=lambda x: str(x["track_id"]),
    )
    if not rows:
        return []

    delay = 0.15
    for attempt in range(max_retries):
        try:
            cursor = conn.cursor()
            successful: List[str] = []
            for update in rows:
                tid = update["track_id"]
                title = update.get("title", "")
                orig = update.get("original_artist", "")
                reason = update.get("is_cover_reason") or (f"Originally by {orig}" if orig else "Cover detection")
                new_title = _build_cover_title(title, orig)

                # Add "Cover" to musicbrainz_genres.
                try:
                    cursor.execute("SELECT musicbrainz_genres FROM tracks WHERE id = %s", (tid,))
                    row = cursor.fetchone()
                except Exception:
                    row = None
                mb_raw = row_get(row, "musicbrainz_genres", 0, "") if row else ""
                try:
                    mb_list = json.loads(mb_raw) if mb_raw and mb_raw != "null" else []
                    if not isinstance(mb_list, list):
                        mb_list = []
                except (json.JSONDecodeError, TypeError):
                    mb_list = []
                if "Cover" not in [str(g).strip() for g in mb_list]:
                    mb_list.insert(0, "Cover")
                    cursor.execute(
                        "UPDATE tracks SET musicbrainz_genres = %s WHERE id = %s",
                        (json.dumps(mb_list), tid),
                    )

                if new_title != title:
                    cursor.execute("UPDATE tracks SET title = %s WHERE id = %s", (new_title, tid))
                cursor.execute(
                    "UPDATE tracks SET is_cover = 1, is_cover_reason = %s, "
                    "original_cover_artist = %s WHERE id = %s",
                    (reason, orig, tid),
                )
                successful.append(tid)

            conn.commit()
            return successful
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            if attempt < max_retries - 1:
                sleep_for = min(delay * (2 ** attempt), 2.0)
                logger.warning(
                    "Cover batch transient DB error, retry %d/%d in %.2fs: %s",
                    attempt + 1, max_retries, sleep_for, exc,
                )
                time.sleep(sleep_for)
            else:
                raise
    return []


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_COVER_SUFFIX_RE_INTERNAL = __import__("re").compile(
    r'\s*\([^)]+\s+cover\)\s*$', __import__("re").IGNORECASE,
)


def _build_cover_title(title: str, original_artist: Optional[str]) -> str:
    if _COVER_SUFFIX_RE_INTERNAL.search(title or ""):
        return title
    if original_artist:
        return f"{title} ({original_artist} Cover)"
    return title


def _loose_normalize(value: str) -> str:
    """Minimal normalisation for writer-name comparison."""
    if not value:
        return ""
    return value.lower().strip()


def _loose_match(left: str, right: str) -> bool:
    """Minimal case-insensitive match for artist names."""
    return _loose_normalize(left) == _loose_normalize(right)
