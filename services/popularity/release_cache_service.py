"""Artist release cache service.

Prefetches an artist's releases (albums / EPs / singles) from MusicBrainz and
Discogs into ``artist_release_cache`` so singles detection can match local
tracks against known single releases WITHOUT per-track API searches.

- MusicBrainz: two release-group searches (singles+EPs, then albums).
- Discogs: one artist-releases call (first 100 releases, role=Main), with
  format-based classification (Single / EP / Album).
- Persisted with a 7-day freshness TTL; re-prefetch skips fresh artists.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Set

from sqlalchemy import text

from api_clients.musicbrainz_http import MusicBrainzHttpClient, escape_lucene_special_chars
from db.engine import db_session

logger = logging.getLogger(__name__)

CACHE_FRESH_HOURS = 24 * 7


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cache_has_source(artist: str, source: str) -> bool:
    """True when the artist's release cache holds rows from ``source``."""
    try:
        with db_session() as session:
            result = session.execute(
                text(
                    "SELECT 1 FROM artist_release_cache "
                    "WHERE LOWER(artist) = LOWER(:artist) AND source = :source LIMIT 1"
                ),
                {"artist": artist, "source": source},
            )
            return result.fetchone() is not None
    except Exception:
        return False


def _artist_cache_fresh(artist: str) -> bool:
    """True when the artist was cached within the TTL."""
    try:
        with db_session() as session:
            result = session.execute(
                text("SELECT MAX(updated_at) AS latest FROM artist_release_cache WHERE LOWER(artist) = LOWER(:artist)"),
                {"artist": artist},
            )
            row = result.fetchone()
        if not row or not row[0]:
            return False
        latest = row[0]
        if isinstance(latest, datetime):
            if latest.tzinfo is None:
                latest = latest.replace(tzinfo=timezone.utc)
            else:
                latest = latest.astimezone(timezone.utc)
            return (datetime.now(timezone.utc) - latest).total_seconds() < CACHE_FRESH_HOURS * 3600
        return False
    except Exception:
        return False


def _upsert_releases(artist: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    try:
        with db_session() as session:
            for row in rows:
                session.execute(
                    text("""
                        INSERT INTO artist_release_cache
                            (artist, title, release_type, source, release_id, year, is_promo, updated_at)
                        VALUES (:artist, :title, :rtype, :source, :release_id, :year, :is_promo, CURRENT_TIMESTAMP)
                        ON CONFLICT (artist, title, source) DO UPDATE SET
                            release_type = EXCLUDED.release_type,
                            release_id = EXCLUDED.release_id,
                            year = EXCLUDED.year,
                            is_promo = EXCLUDED.is_promo,
                            updated_at = CURRENT_TIMESTAMP
                    """),
                    {**row, "artist": artist, "is_promo": bool(row.get("is_promo", False))},
                )
    except Exception as exc:
        logger.debug("[RELEASE_CACHE] Upsert failed for %s: %s", artist, exc)


def _fetch_musicbrainz_releases(artist: str) -> list[dict[str, Any]]:
    """Release-groups for singles/EPs and albums (two throttled calls)."""
    try:
        client = MusicBrainzHttpClient(enabled=True)
        escaped = escape_lucene_special_chars(artist)
        out: list[dict[str, Any]] = []
        queries = {
            "single": f'artist:"{escaped}" AND (primarytype:single OR primarytype:ep)',
            "album": f'artist:"{escaped}" AND primarytype:album',
        }
        for rtype, query in queries.items():
            for rg in client.search_release_groups(query, limit=25) or []:
                if not isinstance(rg, dict):
                    continue
                title = str(rg.get("title") or "").strip()
                primary = str(rg.get("primary-type") or "").lower()
                if not title or primary not in ("album", "ep", "single"):
                    continue
                year = None
                fdate = str(rg.get("first-release-date") or "")
                if len(fdate) >= 4 and fdate[:4].isdigit():
                    year = int(fdate[:4])
                out.append({
                    "title": title,
                    "release_type": primary,
                    "source": "musicbrainz",
                    "release_id": str(rg.get("id") or "").strip() or None,
                    "year": year,
                })
        return out
    except Exception as exc:
        logger.debug("[RELEASE_CACHE] MB fetch failed for %s: %s", artist, exc)
        return []


def _fetch_discogs_releases(artist: str, discogs_artist_id: str) -> list[dict[str, Any]]:
    """One Discogs artist-releases call; classify by format (role=Main only)."""
    try:
        from api_clients.discogs_http import DiscogsHttpClient
        from helpers.config_helpers import get_config
        token = ""
        try:
            token = (get_config().get("api_integrations", {}).get("discogs", {}) or {}).get("token", "") or ""
        except Exception:
            token = ""
        if not token or token.lower() in ("your_discogs_token", "your_token", "placeholder"):
            return []
        client = DiscogsHttpClient(token=token)
        releases = client.get_artist_releases(discogs_artist_id, per_page=100) or []
        out: list[dict[str, Any]] = []
        for rel in releases:
            if not isinstance(rel, dict):
                continue
            if str(rel.get("role") or "Main").strip().lower() != "main":
                continue
            title = str(rel.get("title") or "").strip()
            if not title:
                continue
            low = [str(f).lower() for f in (rel.get("format") or []) if f]
            if "single" in low:
                rtype = "single"
            elif "ep" in low:
                rtype = "ep"
            else:
                rtype = "album"
            year = None
            raw_year = rel.get("year")
            if isinstance(raw_year, int) and raw_year > 0:
                year = raw_year
            out.append({
                "title": title,
                "release_type": rtype,
                "source": "discogs",
                "release_id": str(rel.get("id") or "").strip() or None,
                "year": year,
                "is_promo": "promo" in " ".join(low),
            })
        return out
    except Exception as exc:
        logger.debug("[RELEASE_CACHE] Discogs fetch failed for %s: %s", artist, exc)
        return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def prefetch_artist_releases(artist: str, discogs_artist_id: str = "") -> Dict[str, Any]:
    """Pull an artist's releases into ``artist_release_cache``.

    Skips artists refreshed within the TTL.  Returns per-source counts.
    """
    if not artist:
        return {"musicbrainz": 0, "discogs": 0}
    # A "fresh" cache is only trusted when it contains the Discogs source — a
    # cache written before the artist's Discogs id was resolved (first-ever
    # scan) would otherwise stay Discogs-free until the 7-day TTL expires,
    # silently disabling Discogs single confirmation and gap detection.
    if _artist_cache_fresh(artist) and (_cache_has_source(artist, "discogs") or not discogs_artist_id):
        return {"musicbrainz": 0, "discogs": 0, "skipped": "fresh"}

    mb_rows = _fetch_musicbrainz_releases(artist)
    _upsert_releases(artist, mb_rows)

    discogs_rows: list[dict[str, Any]] = []
    if discogs_artist_id:
        discogs_rows = _fetch_discogs_releases(artist, discogs_artist_id)
        _upsert_releases(artist, discogs_rows)

    if mb_rows or discogs_rows:
        logger.info(
            "[RELEASE_CACHE] Artist '%s': %s MB + %s Discogs releases cached",
            artist, len(mb_rows), len(discogs_rows),
        )
    return {"musicbrainz": len(mb_rows), "discogs": len(discogs_rows)}


def get_artist_single_titles(artist: str, source: str | None = None) -> Set[str]:
    """Single/EP titles known for the artist, lowercased.

    ``source``: ``"musicbrainz"``, ``"discogs"``, or None for both.
    """
    try:
        source_clause = ""
        params: dict[str, Any] = {"artist": artist}
        if source:
            source_clause = " AND source = :source"
            params["source"] = source
        with db_session() as session:
            result = session.execute(
                text(f"""
                    SELECT DISTINCT title FROM artist_release_cache
                    WHERE LOWER(artist) = LOWER(:artist)
                      AND release_type IN ('single', 'ep')
                      {source_clause}
                """),
                params,
            )
            return {str(r[0]).strip().lower() for r in result.fetchall() or [] if r[0]}
    except Exception:
        return set()


def get_artist_promo_titles(artist: str, source: str = "discogs") -> Set[str]:
    """Promotional single/EP titles known for the artist, lowercased.

    A promo-only release is real Discogs confirmation that the track was
    issued as a (promotional) single, but it is weaker evidence than a
    commercial single — the caller should treat it as a medium-confidence
    source rather than a high-confidence one.
    """
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT DISTINCT title FROM artist_release_cache
                    WHERE LOWER(artist) = LOWER(:artist)
                      AND source = :source
                      AND release_type IN ('single', 'ep')
                      AND COALESCE(is_promo, FALSE) = TRUE
                """),
                {"artist": artist, "source": source},
            )
            return {str(r[0]).strip().lower() for r in result.fetchall() or [] if r[0]}
    except Exception:
        return set()


# ---------------------------------------------------------------------------
# Missing-releases gap detection + tracklists (cache-driven)
# ---------------------------------------------------------------------------

def _norm_release_title(title: str) -> str:
    """Normalize a release title for library-vs-cache comparison."""
    import re as _re
    return _re.sub(r"\s+", " ", (title or "").strip().lower())


def _library_albums(artist: str) -> Set[str]:
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT DISTINCT album FROM tracks
                    WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist
                      AND album IS NOT NULL AND TRIM(album) <> ''
                """),
                {"artist": artist},
            )
            return {_norm_release_title(r[0]) for r in result.fetchall() or [] if r[0]}
    except Exception:
        return set()


def refresh_missing_releases_for_artist(artist: str) -> Dict[str, Any]:
    """Compare the cached artist releases against the library and persist gaps.

    Mirrors the legacy gap detection: a cached release (album/EP/single) whose
    normalized title is not in the library is stored in ``missing_releases``
    with a category (Album / EP / Single — current-year singles only).  Pure
    DB work — no API calls — so it is safe to run during every artist prefetch.
    """
    if not artist:
        return {"missing": 0}
    library = _library_albums(artist)
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT DISTINCT title, release_type, year, release_id, source
                    FROM artist_release_cache
                    WHERE LOWER(artist) = LOWER(:artist)
                """),
                {"artist": artist},
            )
            cached = [dict(r._mapping) for r in result.fetchall() or []]
    except Exception as exc:
        logger.debug("[RELEASE_CACHE] Missing-releases read failed for %s: %s", artist, exc)
        return {"missing": 0}

    current_year = datetime.now().year
    seen: Set[str] = set()
    missing_rows: list[dict[str, Any]] = []
    for row in cached:
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        norm = _norm_release_title(title)
        if norm in library or norm in seen:
            continue
        seen.add(norm)
        rtype = str(row.get("release_type") or "album").lower()
        year = row.get("year")
        if rtype == "single":
            if not (isinstance(year, int) and year == current_year):
                continue  # legacy parity: singles only when current-year
            category = "Single"
        elif rtype == "ep":
            category = "EP"
        else:
            category = "Album"
        missing_rows.append({
            "release_id": str(row.get("release_id") or "").strip() or f"{artist}-{norm}",
            "title": title,
            "primary_type": rtype,
            "first_release_date": str(year) if isinstance(year, int) else None,
            "category": category,
            "source": str(row.get("source") or "musicbrainz"),
        })

    try:
        with db_session() as session:
            session.execute(
                text("DELETE FROM missing_releases WHERE LOWER(artist) = LOWER(:artist)"),
                {"artist": artist},
            )
            for item in missing_rows:
                session.execute(
                    text("""
                        INSERT INTO missing_releases
                            (artist, release_id, title, primary_type, first_release_date,
                             category, last_checked)
                        VALUES (:artist, :release_id, :title, :primary_type, :fdate,
                                :category, CURRENT_TIMESTAMP)
                    """),
                    {**item, "artist": artist, "fdate": item.get("first_release_date")},
                )
    except Exception as exc:
        logger.debug("[RELEASE_CACHE] Missing-releases persist failed for %s: %s", artist, exc)

    return {"missing": len(missing_rows)}


def populate_missing_release_tracklists(artist: str, limit: int = 5) -> Dict[str, Any]:
    """Fetch tracklists for missing releases and store them for queue matching.

    For the first *limit* missing releases, fetch the tracklist from MusicBrainz
    and persist them so the download queue can match against them.

    Returns per-release counts of tracks stored.
    """
    if not artist:
        return {"fetched": 0}

    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT release_id, title, primary_type, category
                    FROM missing_releases
                    WHERE LOWER(artist) = LOWER(:artist)
                      AND (tracklist IS NULL OR tracklist = '[]')
                    ORDER BY last_checked ASC NULLS FIRST
                    LIMIT :limit
                """),
                {"artist": artist, "limit": limit},
            )
            missing = [dict(r._mapping) for r in result.fetchall() or []]
    except Exception as exc:
        logger.debug("[RELEASE_CACHE] Missing-releases query failed for %s: %s", artist, exc)
        return {"fetched": 0}

    if not missing:
        return {"fetched": 0}

    fetched = 0
    for row in missing:
        release_id = str(row.get("release_id") or "").strip()
        if not release_id:
            continue
        try:
            from api_clients.musicbrainz_http import MusicBrainzHttpClient
            client = MusicBrainzHttpClient(enabled=True)
            recordings = client.get_release_recordings(release_id) or []
            tracklist = []
            for rec in recordings:
                if not isinstance(rec, dict):
                    continue
                title = str(rec.get("title") or "").strip()
                if title:
                    tracklist.append(title)
            if tracklist:
                import json
                tracklist_json = json.dumps(tracklist)
                with db_session() as session:
                    session.execute(
                        text("""
                            UPDATE missing_releases
                            SET tracklist = :tracklist, last_checked = CURRENT_TIMESTAMP
                            WHERE release_id = :release_id AND LOWER(artist) = LOWER(:artist)
                        """),
                        {"tracklist": tracklist_json, "release_id": release_id, "artist": artist},
                    )
                fetched += 1
        except Exception as exc:
            logger.debug("[RELEASE_CACHE] Tracklist fetch failed for %s (%s): %s", artist, release_id, exc)

    return {"fetched": fetched}
