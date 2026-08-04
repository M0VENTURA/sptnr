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
                            (artist, title, release_type, source, release_id, year, updated_at)
                        VALUES (:artist, :title, :rtype, :source, :release_id, :year, CURRENT_TIMESTAMP)
                        ON CONFLICT (artist, title, source) DO UPDATE SET
                            release_type = EXCLUDED.release_type,
                            release_id = EXCLUDED.release_id,
                            year = EXCLUDED.year,
                            updated_at = CURRENT_TIMESTAMP
                    """),
                    {**row, "artist": artist},
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
    if _artist_cache_fresh(artist):
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
    except Exception:\n        return set()\n\n\n# ---------------------------------------------------------------------------\n# Missing-releases gap detection + tracklists (cache-driven)\n# ---------------------------------------------------------------------------\n\ndef _norm_release_title(title: str) -> str:\n    \"\"\"Normalize a release title for library-vs-cache comparison.\"\"\"\n    import re as _re\n    return _re.sub(r\"\\s+\", \" \", (title or \"\").strip().lower())\n\n\ndef _library_albums(artist: str) -> Set[str]:\n    try:\n        with db_session() as session:\n            result = session.execute(\n                text(\"\"\"\n                    SELECT DISTINCT album FROM tracks\n                    WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist\n                      AND album IS NOT NULL AND TRIM(album) <> ''\n                \"\"\"),\n                {\"artist\": artist},\n            )\n            return {_norm_release_title(r[0]) for r in result.fetchall() or [] if r[0]}\n    except Exception:\n        return set()\n\n\ndef refresh_missing_releases_for_artist(artist: str) -> Dict[str, Any]:\n    \"\"\"Compare the cached artist releases against the library and persist gaps.\n\n    Mirrors the legacy gap detection: a cached release (album/EP/single) whose\n    normalized title is not in the library is stored in ``missing_releases``\n    with a category (Album / EP / Single — current-year singles only).  Pure\n    DB work — no API calls — so it is safe to run during every artist prefetch.\n    \"\"\"\n    if not artist:\n        return {\"missing\": 0}\n    library = _library_albums(artist)\n    try:\n        with db_session() as session:\n            result = session.execute(\n                text(\"\"\"\n                    SELECT DISTINCT title, release_type, year, release_id, source\n                    FROM artist_release_cache\n                    WHERE LOWER(artist) = LOWER(:artist)\n                \"\"\"),\n                {\"artist\": artist},\n            )\n            cached = [dict(r._mapping) for r in result.fetchall() or []]\n    except Exception as exc:\n        logger.debug(\"[RELEASE_CACHE] Missing-releases read failed for %s: %s\", artist, exc)\n        return {\"missing\": 0}\n\n    current_year = datetime.now().year\n    seen: Set[str] = set()\n    missing_rows: list[dict[str, Any]] = []\n    for row in cached:\n        title = str(row.get(\"title\") or \"\").strip()\n        if not title:\n            continue\n        norm = _norm_release_title(title)\n        if norm in library or norm in seen:\n            continue\n        seen.add(norm)\n        rtype = str(row.get(\"release_type\") or \"album\").lower()\n        year = row.get(\"year\")\n        if rtype == \"single\":\n            if not (isinstance(year, int) and year == current_year):\n                continue  # legacy parity: singles only when current-year\n            category = \"Single\"\n        elif rtype == \"ep\":\n            category = \"EP\"\n        else:\n            category = \"Album\"\n        missing_rows.append({\n            \"release_id\": str(row.get(\"release_id\") or \"\").strip() or f\"{artist}-{norm}\",\n            \"title\": title,\n            \"primary_type\": rtype,\n            \"first_release_date\": str(year) if isinstance(year, int) else None,\n            \"category\": category,\n            \"source\": str(row.get(\"source\") or \"musicbrainz\"),\n        })\n\n    try:\n        with db_session() as session:\n            session.execute(\n                text(\"DELETE FROM missing_releases WHERE LOWER(artist) = LOWER(:artist)\"),\n                {\"artist\": artist},\n            )\n            for item in missing_rows:\n                session.execute(\n                    text(\"\"\"\n                        INSERT INTO missing_releases\n                            (artist, release_id, title, primary_type, first_release_date,\n                             category, last_checked)\n                        VALUES (:artist, :release_id, :title, :primary_type, :fdate,\n                                :category, CURRENT_TIMESTAMP)\n                    \"\"\"),\n                    {**item, \"artist\": artist, \"fdate\": item.get(\"first_release_date\")},\n                )\n    except Exception as exc:\n        logger.debug(\"[RELEASE_CACHE] Missing-releases persist failed for %s: %s\", artist, exc)\n        return {\"missing\": 0}\n\n    if missing_rows:\n        logger.info(\"[RELEASE_CACHE] Artist '%s': %s missing release(s) detected from cache\", artist, len(missing_rows))\n    return {\"missing\": len(missing_rows), \"rows\": missing_rows}\n\n\ndef populate_missing_release_tracklists(artist: str, limit: int = 5) -> Dict[str, Any]:\n    \"\"\"Fetch tracklists for missing releases and store them for download.\n\n    For each missing release without a cached tracklist: resolve a concrete\n    release from the release-group, fetch its recordings, and persist into\n    ``musicbrainz_releases`` + ``musicbrainz_release_tracks`` (keyed by the\n    release-group id, matching ``missing_releases.release_id``) so the\n    download flows can use them like the legacy system.\n\n    Throttled (two MusicBrainz calls per release); capped by ``limit``.\n    \"\"\"\n    if not artist or limit <= 0:\n        return {\"populated\": 0}\n    try:\n        with db_session() as session:\n            result = session.execute(\n                text(\"\"\"\n                    SELECT release_id, title FROM missing_releases\n                    WHERE LOWER(artist) = LOWER(:artist)\n                      AND release_id IS NOT NULL AND TRIM(release_id) <> ''\n                      AND release_id NOT IN (SELECT DISTINCT release_id FROM musicbrainz_release_tracks)\n                    LIMIT :limit\n                \"\"\"),\n                {\"artist\": artist, \"limit\": max(1, min(limit, 25))},\n            )\n            targets = [dict(r._mapping) for r in result.fetchall() or []]\n    except Exception as exc:\n        logger.debug(\"[RELEASE_CACHE] Tracklist targets failed for %s: %s\", artist, exc)\n        return {\"populated\": 0}\n\n    if not targets:\n        return {\"populated\": 0}\n\n    try:\n        from api_clients.musicbrainz_http import MusicBrainzHttpClient\n        client = MusicBrainzHttpClient(enabled=True)\n    except Exception as exc:\n        logger.debug(\"[RELEASE_CACHE] MB client failed: %s\", exc)\n        return {\"populated\": 0}\n\n    populated = 0\n    for target in targets:\n        rg_id = str(target.get(\"release_id\") or \"\").strip()\n        try:\n            rg = client.get_release_group(rg_id) or {}\n            releases = rg.get(\"releases\") or []\n            release_mbid = \"\"\n            if isinstance(releases, list) and releases:\n                for rel in releases:\n                    if isinstance(rel, dict) and rel.get(\"id\"):\n                        release_mbid = str(rel.get(\"id\") or \"\").strip()\n                        break\n            if not release_mbid:\n                # Fallback: search releases by release-group id.\n                hits = client.search_releases(f\"rgid:{rg_id}\", limit=1) or []\n                if hits and isinstance(hits[0], dict) and hits[0].get(\"id\"):\n                    release_mbid = str(hits[0][\"id\"]).strip()\n            if not release_mbid:\n                continue\n            rel = client.get_release(release_mbid, inc=\"recordings\") or {}\n            tracks: list[dict[str, Any]] = []\n            for medium in rel.get(\"media\") or []:\n                if not isinstance(medium, dict):\n                    continue\n                disc = int(medium.get(\"position\") or 1)\n                for trk in medium.get(\"tracks\") or []:\n                    if not isinstance(trk, dict):\n                        continue\n                    rec = trk.get(\"recording\") or {}\n                    tracks.append({\n                        \"disc_number\": disc,\n                        \"track_number\": trk.get(\"position\"),\n                        \"track_title\": trk.get(\"title\") or rec.get(\"title\") or \"\",\n                        \"duration\": trk.get(\"length\"),\n                        \"isrc\": trk.get(\"isrc\"),\n                        \"recording_mbid\": rec.get(\"id\") or None,\n                        \"recording_title\": rec.get(\"title\") or None,\n                    })\n            if not tracks:\n                continue\n            with db_session() as session:\n                session.execute(\n                    text(\"\"\"\n                        INSERT INTO musicbrainz_releases\n                            (release_id, release_title, artist, release_year, total_tracks,\n                             status, method, album_artist, release_source, updated_at)\n                        VALUES (:release_id, :title, :artist, :year, :total,\n                                'active', 'musicbrainz', :artist, 'missing_releases', CURRENT_TIMESTAMP)\n                        ON CONFLICT (release_id) DO UPDATE SET\n                            release_title = EXCLUDED.release_title,\n                            total_tracks = EXCLUDED.total_tracks,\n                            updated_at = CURRENT_TIMESTAMP\n                    \"\"\"),\n                    {\n                        \"release_id\": rg_id,\n                        \"title\": target.get(\"title\") or rel.get(\"title\") or \"\",\n                        \"artist\": artist,\n                        \"year\": int(str(rel.get(\"date\") or \"\")[:4]) if str(rel.get(\"date\") or \"\")[:4].isdigit() else None,\n                        \"total\": len(tracks),\n                    },\n                )\n                for t in tracks:\n                    session.execute(\n                        text(\"\"\"\n                            INSERT INTO musicbrainz_release_tracks\n                                (release_id, disc_number, track_number, track_title, duration,\n                                 isrc, recording_title, recording_mbid, album_artist, year,\n                                 status, created_at, updated_at)\n                            VALUES (:release_id, :disc, :num, :title, :duration,\n                                    :isrc, :rec_title, :rec_mbid, :album_artist, :year,\n                                    'queued', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)\n                        \"\"\"),\n                        {\n                            \"release_id\": rg_id,\n                            \"disc\": t[\"disc_number\"],\n                            \"num\": t[\"track_number\"],\n                            \"title\": t[\"track_title\"],\n                            \"duration\": t[\"duration\"],\n                            \"isrc\": t[\"isrc\"],\n                            \"rec_title\": t[\"recording_title\"],\n                            \"rec_mbid\": t[\"recording_mbid\"],\n                            \"album_artist\": artist,\n                            \"year\": str(rel.get(\"date\") or \"\")[:4] or None,\n                        },\n                    )\n            populated += 1\n            logger.info(\"[RELEASE_CACHE] Tracklist cached for missing release %s (%s): %s tracks\", rg_id, artist, len(tracks))\n        except Exception as exc:\n            logger.debug(\"[RELEASE_CACHE] Tracklist fetch failed for %s: %s\", rg_id, exc)\n\n    return {\"populated\": populated}"
