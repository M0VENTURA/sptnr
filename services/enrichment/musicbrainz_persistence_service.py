"""MusicBrainz persistence helpers.

These functions intentionally sit outside ``api_clients`` because they mutate
the local database. They bridge MusicBrainz enrichment and repository writes.
"""

from __future__ import annotations


import re
import time
import logging

from typing import Optional, Dict



from api_clients.musicbrainz_http import MUSICBRAINZ_UUID_RE, escape_lucene_special_chars

from services.enrichment.musicbrainz_service import MusicBrainzService
from services.enrichment.musicbrainz_service import fetch_release_metadata
from helpers.normalization_service import strip_featured_artist

logger = logging.getLogger(__name__)


def _build_artist_credit_string(artist_credit):
    parts = []
    for entry in artist_credit:
        if isinstance(entry, dict):
            name = entry.get("name", "")
            parts.append(name + entry.get("joinphrase", ""))
    return "".join(parts).strip()



def _has_valid_artist_mbid(value) -> bool:
    """True when *value* is a non-empty MusicBrainz UUID."""
    raw = str(value or "").strip()
    return bool(raw) and bool(MUSICBRAINZ_UUID_RE.match(raw))


def lookup_and_save_artist_mbid(artist: str, db_connection) -> str:
    """Lookup an artist MBID and update matching track rows missing artist MBIDs."""
    if not artist or db_connection is None:
        return ""

    lookup_artist = strip_featured_artist(artist)
    service = MusicBrainzService(enabled=True)

    try:
        candidates = service.http.search_artists(f'artist:"{escape_lucene_special_chars(lookup_artist)}"', limit=10)
        if not candidates:
            return ""

        best_candidate, best_score = None, -1
        for candidate in candidates:
            score = 0
            name = candidate.get("name", "")
            if name.lower() == lookup_artist.lower():
                score += 100
            elif lookup_artist.lower() in name.lower():
                score += 50
            else:
                continue

            artist_type = (candidate.get("type") or "").lower()
            if artist_type == "group":
                score += 25
            elif artist_type != "person":
                score += 10

            if candidate.get("disambiguation"):
                score -= 10
            if candidate.get("life-span", {}) and not candidate.get("life-span", {}).get("ended"):
                score += 5

            if score > best_score:
                best_score = score
                best_candidate = candidate

        if not best_candidate or best_score < 0:
            return ""

        mbid = best_candidate.get("id", "")
        if not mbid:
            return ""

        cursor = db_connection.cursor()
        placeholder = "%s"
        # Fill BOTH artist-MBID columns (musicbrainz_artist_id and
        # musicbrainz_artistid) — different consumers read different ones
        # (artist page, similar artists, missing releases, single detection).
        cursor.execute(f"SELECT id, musicbrainz_artist_id, musicbrainz_artistid FROM tracks WHERE artist = {placeholder}", (artist,))
        # Rows are RealDictRow (dict-like); never index by position.
        to_fix = [
            row.get("id") for row in cursor.fetchall()
            if not _has_valid_artist_mbid(row.get("musicbrainz_artist_id"))
            or not _has_valid_artist_mbid(row.get("musicbrainz_artistid"))
        ]

        cursor.execute(f"SELECT id, artist, musicbrainz_artist_id, musicbrainz_artistid FROM tracks WHERE artist LIKE {placeholder}", (f"{artist} %",))
        feat_re = re.compile(r"\s+(?:feat\.?|featuring|ft\.?)\s+", re.IGNORECASE)
        for row in cursor.fetchall():
            if feat_re.search(str(row.get("artist") or "")):
                if (not _has_valid_artist_mbid(row.get("musicbrainz_artist_id"))
                        or not _has_valid_artist_mbid(row.get("musicbrainz_artistid"))):
                    to_fix.append(row.get("id"))

        to_fix = list(dict.fromkeys(to_fix))
        if to_fix:
            for index in range(0, len(to_fix), 500):
                chunk = to_fix[index:index + 500]
                placeholders = ','.join([placeholder] * len(chunk))
                # Only fill columns that are empty — a user-edited ID wins.
                cursor.execute(
                    f"""UPDATE tracks SET
                        musicbrainz_artist_id = CASE
                            WHEN musicbrainz_artist_id IS NULL OR musicbrainz_artist_id = '' THEN {placeholder}
                            ELSE musicbrainz_artist_id END,
                        musicbrainz_artistid = CASE
                            WHEN musicbrainz_artistid IS NULL OR musicbrainz_artistid = '' THEN {placeholder}
                            ELSE musicbrainz_artistid END
                        WHERE id IN ({placeholders})""",
                    (mbid, mbid, *chunk),
                )
        db_connection.commit()
        return mbid
    except Exception:
        return ""
