"""MusicBrainz persistence helpers.

These functions intentionally sit outside ``api_clients`` because they mutate
the local database. They bridge MusicBrainz enrichment and repository writes.
"""
"""MusicBrainz persistence helpers.

These functions intentionally sit outside ``api_clients`` because they mutate
the local database. They bridge MusicBrainz enrichment and repository writes.
"""

from __future__ import annotations

import re
from typing import Any

import structlog
from sqlalchemy import text

from api_clients.musicbrainz_http import MUSICBRAINZ_UUID_RE, escape_lucene_special_chars
from services.enrichment.musicbrainz_service import get_shared_mb_client
from helpers.normalization_service import strip_featured_artist
from db.engine import db_session

logger = structlog.get_logger(__name__)


def _build_artist_credit_string(artist_credit: list[Any]) -> str:
    parts = []
    for entry in artist_credit:
        if isinstance(entry, dict):
            name = entry.get("name", "")
            parts.append(name + entry.get("joinphrase", ""))
    return "".join(parts).strip()


def _has_valid_artist_mbid(value: Any) -> bool:
    """True when *value* is a non-empty MusicBrainz UUID."""
    raw = str(value or "").strip()
    return bool(raw) and bool(MUSICBRAINZ_UUID_RE.match(raw))


def lookup_and_save_artist_mbid(artist: str, db_connection: Any = None) -> str:
    """Lookup an artist MBID and update matching track rows missing artist MBIDs.

    ``db_connection`` is kept for backward compatibility — the writes run on
    their own SQLAlchemy session via QueuePool.
    """
    if not artist:
        return ""

    lookup_artist = strip_featured_artist(artist)
    
    # ✅ USE THE SHARED CLIENT: Ensures we respect global rate limits and reuse connections
    mb_client = get_shared_mb_client()

    try:
        candidates = mb_client.search_artists(f'artist:"{escape_lucene_special_chars(lookup_artist)}"', limit=10)
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

        with db_session() as session:
            to_fix = [
                row.get("id")
                for row in session.execute(
                    text("SELECT id, musicbrainz_artistid FROM tracks WHERE artist = :artist"),
                    {"artist": artist},
                ).mappings().all()
                if not _has_valid_artist_mbid(row.get("musicbrainz_artistid"))
            ]

            feat_re = re.compile(r"\s+(?:feat\.?|featuring|ft\.?)\s+", re.IGNORECASE)
            for row in session.execute(
                text("SELECT id, artist, musicbrainz_artistid FROM tracks WHERE artist LIKE :pattern"),
                {"pattern": f"{artist} %"},
            ).mappings().all():
                if feat_re.search(str(row.get("artist") or "")):
                    if not _has_valid_artist_mbid(row.get("musicbrainz_artistid")):
                        to_fix.append(row.get("id"))

            to_fix = list(dict.fromkeys(to_fix))
            if to_fix:
                for index in range(0, len(to_fix), 500):
                    # ✅ NATIVE SQL TUPLE BINDING: Faster and avoids messy string formatting
                    chunk = tuple(to_fix[index:index + 500])
                    
                    session.execute(
                        text("""
                            UPDATE tracks SET
                                musicbrainz_artistid = CASE
                                    WHEN musicbrainz_artistid IS NULL OR musicbrainz_artistid = '' THEN :mbid
                                    ELSE musicbrainz_artistid 
                                END
                            WHERE id IN :ids
                        """),
                        {"mbid": mbid, "ids": chunk},
                    )
                    
        return mbid
        
    except Exception as exc:
        logger.debug("Artist MBID lookup and save failed", artist=artist, error=str(exc))
        return ""

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


def lookup_and_save_artist_mbid(artist: str, db_connection=None) -> str:
    """Lookup an artist MBID and update matching track rows missing artist MBIDs.

    ``db_connection`` is kept for backward compatibility — the writes run on
    their own SQLAlchemy session.
    """
    if not artist:
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

        from sqlalchemy import text
        from db.engine import db_session
        with db_session() as session:
            # Fill the artist-MBID column (musicbrainz_artistid) — consumers
            # (artist page, similar artists, missing releases, single detection)
            # all read this one; the legacy ``musicbrainz_artist_id`` column does
            # not exist in the current schema.
            to_fix = [
                row.get("id")
                for row in session.execute(
                    text("SELECT id, musicbrainz_artistid FROM tracks WHERE artist = :artist"),
                    {"artist": artist},
                ).mappings().all()
                if not _has_valid_artist_mbid(row.get("musicbrainz_artistid"))
            ]

            feat_re = re.compile(r"\s+(?:feat\.?|featuring|ft\.?)\s+", re.IGNORECASE)
            for row in session.execute(
                text("SELECT id, artist, musicbrainz_artistid FROM tracks WHERE artist LIKE :pattern"),
                {"pattern": f"{artist} %"},
            ).mappings().all():
                if feat_re.search(str(row.get("artist") or "")):
                    if not _has_valid_artist_mbid(row.get("musicbrainz_artistid")):
                        to_fix.append(row.get("id"))

            to_fix = list(dict.fromkeys(to_fix))
            if to_fix:
                for index in range(0, len(to_fix), 500):
                    chunk = to_fix[index:index + 500]
                    placeholders = ", ".join(f":tid{i}" for i in range(len(chunk)))
                    # Only fill the column when empty — a user-edited ID wins.
                    session.execute(
                        text(f"""UPDATE tracks SET
                            musicbrainz_artistid = CASE
                                WHEN musicbrainz_artistid IS NULL OR musicbrainz_artistid = '' THEN :mbid
                                ELSE musicbrainz_artistid END
                            WHERE id IN ({placeholders})"""),
                        {"mbid": mbid, **{f"tid{i}": tid for i, tid in enumerate(chunk)}},
                    )
        return mbid
    except Exception:
        return ""
