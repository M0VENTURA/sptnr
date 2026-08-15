"""MusicBrainz upcoming-releases fetcher.

Mirrors the legacy daily MusicBrainz collection refresh: pulls release-groups
for artists in the collection within a configurable date window and upserts
them into ``upcoming_releases`` (source = "MusicBrainz Daily Collection"),
augmenting the Wikipedia scrape with direct MusicBrainz data.

A global discovery pass (no artist constraint) also runs first so brand-new
MusicBrainz releases from artists NOT in the collection surface too.

Config (``features.*`` in config.yaml, editable on the config page):
    daily_musicbrainz_release_scan_enabled (default True)
    daily_musicbrainz_release_lookback_days (default 42)
    daily_musicbrainz_release_lookahead_days (default 120)
    daily_musicbrainz_release_max_artists (default 500)
    daily_musicbrainz_release_per_artist_limit (default 100)
    daily_musicbrainz_release_global_limit (default 50; 0 disables global discovery)
"""

from __future__ import annotations

import logging
import threading
import unicodedata
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text

from api_clients.musicbrainz_http import MusicBrainzHttpClient, escape_lucene_special_chars
from db.engine import db_session

logger = logging.getLogger(__name__)

SOURCE_NAME = "MusicBrainz Daily Collection"

_DISQUALIFYING_SECONDARY_TYPES = ("live", "remix", "compilation")

_mb_client: MusicBrainzHttpClient | None = None
_refresh_lock = threading.Lock()
_refresh_running = False

# Circuit breaker: abort the whole refresh after this many consecutive
# per-artist failures (rate limits / network errors usually repeat).  Kept
# generous — MusicBrainz search frequently returns transient 503s, and a low
# threshold meant one flaky minute aborted the entire weekly run, surfacing
# zero "MusicBrainz Daily Collection" rows.
_MAX_CONSECUTIVE_FAILURES = 20

# Live progress snapshot shared with the /scrape/status endpoint.
_status_lock = threading.Lock()
_status: dict[str, Any] = {
    "status": "idle",
    "progress": 0,
    "total": 0,
    "current_artist": None,
    "updated_at": None,
    "last_stats": None,
}


def _set_status(**changes: Any) -> None:
    with _status_lock:
        _status.update(changes)
        _status["updated_at"] = datetime.now().isoformat(timespec="seconds")


def get_refresh_status() -> dict[str, Any]:
    """Snapshot of the background MusicBrainz refresh (for the status endpoint)."""
    with _status_lock:
        return dict(_status)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_mb_client() -> MusicBrainzHttpClient:
    global _mb_client
    if _mb_client is None:
        _mb_client = MusicBrainzHttpClient(enabled=True)
    return _mb_client


def _normalize_artist(name: str) -> str:
    return unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode().strip().lower()


def _parse_release_date(raw: Any) -> str | None:
    """Accept YYYY-MM-DD / YYYY-MM / YYYY; return the raw string or None."""
    if not raw:
        return None
    raw = str(raw).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            datetime.strptime(raw, fmt)
            return raw
        except ValueError:
            continue
    return None


def _normalize_release_date(raw: Any) -> str:
    """Expand a partial MB release date to a comparable ISO date string.

    MusicBrainz stores partial dates ("2026", "2026-07").  Storing them as-is
    breaks the upcoming-releases page: its window filter compares
    ``release_date`` with ``BETWEEN :win_start AND :win_end`` as strings, so a
    year-only row ("2026") silently drops out of the feed even though it is
    within the scan window.  Expand to the earliest representable date
    (matching the legacy scraper): "2026" -> "2026-01-01", "2026-07" ->
    "2026-07-01".  Full dates pass through untouched.
    """
    raw = str(raw or "").strip()
    if len(raw) == 4 and raw[:4].isdigit():
        return f"{raw}-01-01"
    if len(raw) == 7 and raw[4] == "-" and raw[:4].isdigit() and raw[5:7].isdigit():
        return f"{raw}-01"
    return raw


def _feature_int(key: str, default: int) -> int:
    try:
        from helpers.config_helpers import get_feature
        return int(get_feature(key, default) or default)
    except Exception:
        return default


def _collection_artists(limit: int) -> list[str]:
    """Distinct album artists from the library (sorted, capped)."""
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT artist_name
                    FROM (
                        SELECT DISTINCT COALESCE(NULLIF(album_artist, ''), artist) AS artist_name,
                               LOWER(COALESCE(NULLIF(album_artist, ''), artist)) AS artist_sort
                        FROM tracks
                        WHERE COALESCE(NULLIF(album_artist, ''), artist) IS NOT NULL
                          AND TRIM(COALESCE(NULLIF(album_artist, ''), artist)) <> ''
                    ) AS artist_rows
                    ORDER BY artist_sort
                    LIMIT :limit
                """),
                {"limit": max(1, min(limit, 5000))},
            )
            return [str(r[0]).strip() for r in result.fetchall() or [] if r[0]]
    except Exception as exc:
        logger.error("[UPCOMING_MB] Collection artist query failed: %s", exc)
        return []


def _fetch_artist_release_groups(
    client: MusicBrainzHttpClient,
    artist: str,
    limit: int,
    min_date,
    max_date,
) -> list[dict[str, Any]]:
    """Fetch one artist's release-groups inside the date window."""
    escaped = escape_lucene_special_chars(artist)
    # The date range is part of the Lucene query — keep its brackets/colons
    # unescaped (escape_lucene_special_chars would mangle them).
    # The QUERY range is deliberately wider than the requested window: MB
    # indexes partial first-release dates ("2026", "2026-10") as ranges, and
    # a day-precision query bound can silently exclude them.  The exact
    # window is re-applied client-side below (partial dates included).
    query_margin = timedelta(days=90)
    query_range = (
        f"[{(min_date - query_margin).isoformat()} TO {(max_date + query_margin).isoformat()}]"
    )
    query = (
        f'artist:"{escaped}" AND (primarytype:album OR primarytype:ep OR primarytype:single) '
        f"AND firstreleasedate:{query_range}"
    )
    raw = client.search_release_groups(query, limit=max(1, min(limit, 100)))
    requested_norm = _normalize_artist(artist)
    out: list[dict[str, Any]] = []
    for rg in raw or []:
        if not isinstance(rg, dict):
            continue
        primary = str(rg.get("primary-type") or "").lower()
        if primary not in ("album", "ep", "single"):
            continue
        secondary = [str(s).lower() for s in (rg.get("secondary-types") or []) if s]
        if any(t in secondary for t in _DISQUALIFYING_SECONDARY_TYPES):
            continue
        # Artist-credit sanity check when present (name search can overmatch).
        credits = rg.get("artist-credit") or []
        if credits:
            names = []
            for credit in credits:
                if isinstance(credit, dict):
                    art = credit.get("artist") or {}
                    names.append(art.get("name") or credit.get("name") or "")
            if names and not any(_normalize_artist(n) == requested_norm for n in names):
                continue
        parsed_date = _parse_release_date(rg.get("first-release-date"))
        if not parsed_date:
            continue
        if not _within_window(parsed_date, min_date, max_date):
            continue
        out.append({
            "id": str(rg.get("id") or "").strip(),
            "title": str(rg.get("title") or "").strip(),
            "first_release_date": parsed_date,
            "primary_type": primary,
        })
    return out


def _fetch_global_upcoming_release_groups(
    client: MusicBrainzHttpClient,
    limit: int,
    min_date,
    max_date,
) -> list[dict[str, Any]]:
    """Fetch upcoming/recent release-groups from ANY artist (discovery).

    Unlike the per-artist scan, this query carries NO ``artist:`` constraint
    so it surfaces releases from artists NOT in the local catalogue — the
    "new MusicBrainz releases" the user expects alongside Wikipedia matches.
    The same date-window + type filters apply; results are capped by
    ``daily_musicbrainz_release_global_limit`` (config).

    CRITICAL: a single OR'd query ``(primarytype:album OR primarytype:ep OR
    primarytype:single)`` is NOT used here. MusicBrainz ranks OR'd
    primary-type terms by relevance and empirically returns ONLY EPs/singles
    on the first pages, burying every album — the exact "isn't finding new
    albums" symptom.  Albums are therefore queried FIRST with their own
    ``primarytype:album`` term (full limit), then EPs/singles are topped up
    with a separate query only if the album result came up short.
    """
    # Widen the query window so partial first-release dates (year / year-month)
    # indexed as ranges are not silently excluded; exact window re-applied
    # below via ``_within_window``.
    query_margin = timedelta(days=90)
    query_range = (
        f"[{(min_date - query_margin).isoformat()} TO {(max_date + query_margin).isoformat()}]"
    )

    def _fetch_type(primary_type: str, fetch_limit: int) -> list[dict[str, Any]]:
        query = f"primarytype:{primary_type} AND firstreleasedate:{query_range}"
        return client.search_release_groups(query, limit=max(1, min(fetch_limit, 100)))

    out: list[dict[str, Any]] = []
    # Pass 1 — albums first (the whole point of the discovery pass).
    for rg in _fetch_type("album", limit) or []:
        if not isinstance(rg, dict):
            continue
        secondary = [str(s).lower() for s in (rg.get("secondary-types") or []) if s]
        if any(t in secondary for t in _DISQUALIFYING_SECONDARY_TYPES):
            continue
        parsed_date = _parse_release_date(rg.get("first-release-date"))
        if not parsed_date or not _within_window(parsed_date, min_date, max_date):
            continue
        out.append({
            "id": str(rg.get("id") or "").strip(),
            "title": str(rg.get("title") or "").strip(),
            "first_release_date": parsed_date,
            "primary_type": "album",
            "artist": _release_group_artist(rg),
        })

    # Pass 2 — EPs/singles only when albums didn't fill the limit.  The same
    # relevance-ranking quirk that buries albums in the OR'd query would also
    # hide singles under an EP-heavy top page, so each type is queried with
    # its own term too.
    remaining = max(1, min(limit, 100)) - len(out)
    if remaining > 0:
        for primary_type in ("ep", "single"):
            if remaining <= 0:
                break
            for rg in _fetch_type(primary_type, remaining) or []:
                if not isinstance(rg, dict):
                    continue
                secondary = [str(s).lower() for s in (rg.get("secondary-types") or []) if s]
                if any(t in secondary for t in _DISQUALIFYING_SECONDARY_TYPES):
                    continue
                parsed_date = _parse_release_date(rg.get("first-release-date"))
                if not parsed_date or not _within_window(parsed_date, min_date, max_date):
                    continue
                out.append({
                    "id": str(rg.get("id") or "").strip(),
                    "title": str(rg.get("title") or "").strip(),
                    "first_release_date": parsed_date,
                    "primary_type": str(rg.get("primary-type") or primary_type).lower(),
                    "artist": _release_group_artist(rg),
                })
                remaining -= 1

    # Newest first so the feed leads with what is actually coming out.
    out.sort(key=lambda r: r.get("first_release_date") or "", reverse=True)
    return out[: max(1, min(limit, 100))]


def _release_group_artist(rg: dict[str, Any]) -> str:
    """Artist credit string for a release-group ('' when absent)."""
    credits = rg.get("artist-credit") or []
    parts = []
    for credit in credits:
        if isinstance(credit, dict):
            name = credit.get("name") or (credit.get("artist") or {}).get("name") or ""
            join = credit.get("joinphrase", "")
            if name:
                parts.append(name)
            if join:
                parts.append(join)
        elif isinstance(credit, str):
            parts.append(credit)
    return "".join(parts).strip()


def _within_window(raw_date: str, min_date, max_date) -> bool:
    """True when a possibly-partial release date falls inside the window.

    Handles YYYY-MM-DD / YYYY-MM / YYYY (the forms ``_parse_release_date``
    accepts).  A year-only date is accepted when its year overlaps the
    window's year span; a year-month date when the month overlaps; a full
    date is compared exactly.  Releases with no usable date component are
    rejected (the caller keeps TBA rows out of the daily scan).
    """
    try:
        if len(raw_date) >= 10:
            return min_date <= datetime.strptime(raw_date[:10], "%Y-%m-%d").date() <= max_date
        if len(raw_date) == 7:
            ym = datetime.strptime(raw_date, "%Y-%m").date()
            # Overlap test: the release month intersects the window month span.
            return ym.replace(day=1) <= max_date.replace(day=1) and ym.replace(day=28) >= min_date.replace(day=1)
        if len(raw_date) == 4:
            year = int(raw_date)
            return min_date.year <= year <= max_date.year
    except ValueError:
        pass
    return False


def _persist_artist_releases(artist: str, releases: list[dict[str, Any]]) -> tuple[int, int]:
    """Upsert one artist's releases; returns (inserted, updated).

    Precedence rule (per-album identity): MusicBrainz metadata is
    authoritative, so an MB row overwrites an existing Wikipedia row — but it
    keeps the earlier valid release date.  A Wikipedia row never clobbers an
    existing MB row (handled in the Wikipedia scraper's upsert).
    """
    inserted = 0
    updated = 0
    if not releases:
        return 0, 0
    try:
        from services.upcoming_releases.matching_service import sanitize_wiki_entry
        with db_session() as session:
            for rel in releases:
                _artist, album = sanitize_wiki_entry(artist, rel.get("title") or "")
                if not album:
                    continue
                rel_date = _normalize_release_date(rel.get("first_release_date") or "")
                release_year = int(rel_date[:4]) if len(rel_date) >= 4 and rel_date[:4].isdigit() else None
                # Case/punctuation-insensitive dedupe: a Wikipedia-scraped row
                # with different casing ("Tanzneid" vs "TANZNEID") must merge
                # into this row instead of creating a duplicate.
                dup = session.execute(
                    text("""
                        SELECT id FROM upcoming_releases
                        WHERE LOWER(REGEXP_REPLACE(artist_name, '[^a-zA-Z0-9]', '', 'g'))
                              = LOWER(REGEXP_REPLACE(:artist, '[^a-zA-Z0-9]', '', 'g'))
                          AND LOWER(REGEXP_REPLACE(album_name, '[^a-zA-Z0-9]', '', 'g'))
                              = LOWER(REGEXP_REPLACE(:album, '[^a-zA-Z0-9]', '', 'g'))
                        LIMIT 1
                    """),
                    {"artist": _artist, "album": album},
                ).fetchone()

                _mbid = rel.get("id") or None
                _ptype = str(rel.get("primary_type") or "").strip()
                if dup:
                    # Same precedence as the ON CONFLICT branch below.
                    session.execute(
                        text("""
                            UPDATE upcoming_releases SET
                                last_seen_at = CURRENT_TIMESTAMP,
                                source = :source,
                                primary_type = COALESCE(:ptype, upcoming_releases.primary_type),
                                release_date = CASE
                                    WHEN upcoming_releases.release_date IS NULL
                                         OR :date IS NULL
                                        THEN COALESCE(:date, upcoming_releases.release_date)
                                    WHEN :date < upcoming_releases.release_date
                                        THEN :date
                                    ELSE upcoming_releases.release_date
                                END,
                                release_year = COALESCE(:year, upcoming_releases.release_year),
                                artist_in_collection = TRUE,
                                release_group_mbid = COALESCE(:mbid, upcoming_releases.release_group_mbid),
                                mbid_match_status = CASE
                                    WHEN COALESCE(upcoming_releases.mbid_manual_override, FALSE) THEN upcoming_releases.mbid_match_status
                                    ELSE 'matched'
                                END,
                                mbid_confidence = CASE
                                    WHEN COALESCE(upcoming_releases.mbid_manual_override, FALSE) THEN upcoming_releases.mbid_confidence
                                    ELSE 'high'
                                END,
                                mbid_source = CASE
                                    WHEN COALESCE(upcoming_releases.mbid_manual_override, FALSE) THEN upcoming_releases.mbid_source
                                    ELSE :source
                                END,
                                mbid_match_score = CASE
                                    WHEN COALESCE(upcoming_releases.mbid_manual_override, FALSE) THEN upcoming_releases.mbid_match_score
                                    ELSE 1.0
                                END,
                                mbid_last_checked_at = :checked,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE id = :id
                        """),
                        {"id": dup[0], "source": SOURCE_NAME, "date": rel_date,
                         "year": release_year, "mbid": _mbid, "ptype": _ptype,
                         "checked": datetime.now().isoformat()},
                    )
                    updated += 1
                    continue

                result = session.execute(
                    text("""
                        INSERT INTO upcoming_releases (
                            artist_name, album_name, release_date, release_year, source,
                            primary_type, artist_in_collection, release_group_mbid,
                            mbid_match_status, mbid_source, mbid_confidence,
                            mbid_match_score, mbid_last_checked_at, status,
                            last_seen_at, updated_at
                        ) VALUES (
                            :artist, :album, :date, :year, :source,
                            :ptype, TRUE, :mbid,
                            'matched', 'musicbrainz_daily_scan', 'high',
                            1.0, :checked, 'discovered',
                            CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                        )
                        ON CONFLICT (artist_name, album_name) DO UPDATE SET
                            last_seen_at = CURRENT_TIMESTAMP,
                            source = EXCLUDED.source,
                            primary_type = COALESCE(EXCLUDED.primary_type, upcoming_releases.primary_type),
                            release_date = CASE
                                WHEN upcoming_releases.release_date IS NULL
                                     OR EXCLUDED.release_date IS NULL
                                    THEN COALESCE(EXCLUDED.release_date, upcoming_releases.release_date)
                                WHEN EXCLUDED.release_date < upcoming_releases.release_date
                                    THEN EXCLUDED.release_date
                                ELSE upcoming_releases.release_date
                            END,
                            release_year = COALESCE(EXCLUDED.release_year, upcoming_releases.release_year),
                            artist_in_collection = TRUE,
                            release_group_mbid = COALESCE(EXCLUDED.release_group_mbid, upcoming_releases.release_group_mbid),
                            mbid_match_status = CASE
                                WHEN COALESCE(upcoming_releases.mbid_manual_override, FALSE) THEN upcoming_releases.mbid_match_status
                                ELSE EXCLUDED.mbid_match_status
                            END,
                            mbid_confidence = CASE
                                WHEN COALESCE(upcoming_releases.mbid_manual_override, FALSE) THEN upcoming_releases.mbid_confidence
                                ELSE EXCLUDED.mbid_confidence
                            END,
                            mbid_source = CASE
                                WHEN COALESCE(upcoming_releases.mbid_manual_override, FALSE) THEN upcoming_releases.mbid_source
                                ELSE EXCLUDED.mbid_source
                            END,
                            mbid_match_score = CASE
                                WHEN COALESCE(upcoming_releases.mbid_manual_override, FALSE) THEN upcoming_releases.mbid_match_score
                                ELSE EXCLUDED.mbid_match_score
                            END,
                            mbid_last_checked_at = EXCLUDED.mbid_last_checked_at,
                            updated_at = CURRENT_TIMESTAMP
                    """),
                    {
                        "artist": _artist,
                        "album": album,
                        "date": rel_date,
                        "year": release_year,
                        "source": SOURCE_NAME,
                        "mbid": _mbid,
                        "ptype": _ptype,
                        "checked": datetime.now().isoformat(),
                    },
                )
                if result.rowcount == 1:
                    inserted += 1
                else:
                    updated += 1
        return inserted, updated
    except Exception as exc:
        logger.debug("[UPCOMING_MB] Persist failed for %s: %s", artist, exc)
        return 0, 0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _artist_in_collection(artist: str) -> bool:
    """True when the artist exists anywhere in the local library."""
    if not artist:
        return False
    try:
        with db_session() as session:
            result = session.execute(
                text("""
                    SELECT 1 FROM tracks
                    WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)
                    LIMIT 1
                """),
                {"artist": artist},
            )
            return result.fetchone() is not None
    except Exception as exc:
        logger.debug("[UPCOMING_MB] Collection check failed for %s: %s", artist, exc)
        return False


def _persist_global_releases(releases: list[dict[str, Any]]) -> tuple[int, int]:
    """Upsert globally-discovered release-groups (ANY artist).

    Unlike the per-artist path (which hard-codes ``artist_in_collection =
    TRUE``), the global path resolves each release's artist credit and sets
    ``artist_in_collection`` accordingly so the UI can distinguish genuinely
    new artists from collection artists.  Returns ``(inserted, updated)``.
    """
    inserted = 0
    updated = 0
    if not releases:
        return 0, 0
    try:
        from services.upcoming_releases.matching_service import sanitize_wiki_entry
        with db_session() as session:
            for rel in releases:
                artist_name = str(rel.get("artist") or "").strip()
                _artist, album = sanitize_wiki_entry(artist_name, rel.get("title") or "")
                if not album:
                    continue
                rel_date = _normalize_release_date(rel.get("first_release_date") or "")
                release_year = int(rel_date[:4]) if len(rel_date) >= 4 and rel_date[:4].isdigit() else None
                _mbid = rel.get("id") or None
                _ptype = str(rel.get("primary_type") or "").strip()
                in_collection = _artist_in_collection(_artist)

                # Case/punctuation-insensitive dedupe (same as per-artist path).
                dup = session.execute(
                    text("""
                        SELECT id FROM upcoming_releases
                        WHERE LOWER(REGEXP_REPLACE(artist_name, '[^a-zA-Z0-9]', '', 'g'))
                              = LOWER(REGEXP_REPLACE(:artist, '[^a-zA-Z0-9]', '', 'g'))
                          AND LOWER(REGEXP_REPLACE(album_name, '[^a-zA-Z0-9]', '', 'g'))
                              = LOWER(REGEXP_REPLACE(:album, '[^a-zA-Z0-9]', '', 'g'))
                        LIMIT 1
                    """),
                    {"artist": _artist, "album": album},
                ).fetchone()

                if dup:
                    session.execute(
                        text("""
                            UPDATE upcoming_releases SET
                                last_seen_at = CURRENT_TIMESTAMP,
                                source = :source,
                                primary_type = COALESCE(:ptype, upcoming_releases.primary_type),
                                release_date = CASE
                                    WHEN upcoming_releases.release_date IS NULL
                                         OR :date IS NULL
                                        THEN COALESCE(:date, upcoming_releases.release_date)
                                    WHEN :date < upcoming_releases.release_date
                                        THEN :date
                                    ELSE upcoming_releases.release_date
                                END,
                                release_year = COALESCE(:year, upcoming_releases.release_year),
                                artist_in_collection = :in_collection,
                                release_group_mbid = COALESCE(:mbid, upcoming_releases.release_group_mbid),
                                mbid_match_status = CASE
                                    WHEN COALESCE(upcoming_releases.mbid_manual_override, FALSE) THEN upcoming_releases.mbid_match_status
                                    ELSE 'matched'
                                END,
                                mbid_confidence = CASE
                                    WHEN COALESCE(upcoming_releases.mbid_manual_override, FALSE) THEN upcoming_releases.mbid_confidence
                                    ELSE 'high'
                                END,
                                mbid_source = CASE
                                    WHEN COALESCE(upcoming_releases.mbid_manual_override, FALSE) THEN upcoming_releases.mbid_source
                                    ELSE :source
                                END,
                                mbid_match_score = CASE
                                    WHEN COALESCE(upcoming_releases.mbid_manual_override, FALSE) THEN upcoming_releases.mbid_match_score
                                    ELSE 1.0
                                END,
                                mbid_last_checked_at = :checked,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE id = :id
                        """),
                        {"id": dup[0], "source": SOURCE_NAME, "date": rel_date,
                         "year": release_year, "mbid": _mbid, "ptype": _ptype,
                         "in_collection": in_collection,
                         "checked": datetime.now().isoformat()},
                    )
                    updated += 1
                    continue

                result = session.execute(
                    text("""
                        INSERT INTO upcoming_releases (
                            artist_name, album_name, release_date, release_year, source,
                            primary_type, artist_in_collection, release_group_mbid,
                            mbid_match_status, mbid_source, mbid_confidence,
                            mbid_match_score, mbid_last_checked_at, status,
                            last_seen_at, updated_at
                        ) VALUES (
                            :artist, :album, :date, :year, :source,
                            :ptype, :in_collection, :mbid,
                            'matched', 'musicbrainz_daily_scan', 'high',
                            1.0, :checked, 'discovered',
                            CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                        )
                        ON CONFLICT (artist_name, album_name) DO UPDATE SET
                            last_seen_at = CURRENT_TIMESTAMP,
                            source = EXCLUDED.source,
                            primary_type = COALESCE(EXCLUDED.primary_type, upcoming_releases.primary_type),
                            release_date = CASE
                                WHEN upcoming_releases.release_date IS NULL
                                     OR EXCLUDED.release_date IS NULL
                                    THEN COALESCE(EXCLUDED.release_date, upcoming_releases.release_date)
                                WHEN EXCLUDED.release_date < upcoming_releases.release_date
                                    THEN EXCLUDED.release_date
                                ELSE upcoming_releases.release_date
                            END,
                            release_year = COALESCE(EXCLUDED.release_year, upcoming_releases.release_year),
                            artist_in_collection = EXCLUDED.artist_in_collection,
                            release_group_mbid = COALESCE(EXCLUDED.release_group_mbid, upcoming_releases.release_group_mbid),
                            mbid_match_status = CASE
                                WHEN COALESCE(upcoming_releases.mbid_manual_override, FALSE) THEN upcoming_releases.mbid_match_status
                                ELSE EXCLUDED.mbid_match_status
                            END,
                            mbid_confidence = CASE
                                WHEN COALESCE(upcoming_releases.mbid_manual_override, FALSE) THEN upcoming_releases.mbid_confidence
                                ELSE EXCLUDED.mbid_confidence
                            END,
                            mbid_source = CASE
                                WHEN COALESCE(upcoming_releases.mbid_manual_override, FALSE) THEN upcoming_releases.mbid_source
                                ELSE EXCLUDED.mbid_source
                            END,
                            mbid_match_score = CASE
                                WHEN COALESCE(upcoming_releases.mbid_manual_override, FALSE) THEN upcoming_releases.mbid_match_score
                                ELSE EXCLUDED.mbid_match_score
                            END,
                            mbid_last_checked_at = EXCLUDED.mbid_last_checked_at,
                            updated_at = CURRENT_TIMESTAMP
                    """),
                    {
                        "artist": _artist,
                        "album": album,
                        "date": rel_date,
                        "year": release_year,
                        "source": SOURCE_NAME,
                        "mbid": _mbid,
                        "ptype": _ptype,
                        "in_collection": in_collection,
                        "checked": datetime.now().isoformat(),
                    },
                )
                if result.rowcount == 1:
                    inserted += 1
                else:
                    updated += 1
        return inserted, updated
    except Exception as exc:
        logger.debug("[UPCOMING_MB] Global persist failed: %s", exc)
        return 0, 0

def fetch_musicbrainz_upcoming_releases(
    artists_limit: int | None = None,
    per_artist_limit: int | None = None,
    lookback_days: int | None = None,
    lookahead_days: int | None = None,
) -> dict[str, Any]:
    """Fetch upcoming/recent release-groups from MusicBrainz for the collection.

    Returns stats: ``{"artists_scanned", "inserted", "updated"}`` (or
    ``{"skipped": True, "reason": ...}`` when disabled / nothing to scan).
    """
    try:
        from helpers.config_helpers import get_feature
        enabled = get_feature("upcoming_releases_scan_enabled", None)
        if enabled is None:
            # Legacy alias — the old per-scanner toggle predates the unified flag.
            enabled = get_feature("daily_musicbrainz_release_scan_enabled", True)
        enabled = bool(enabled)
    except Exception:
        enabled = True
    if not enabled:
        return {"skipped": True, "reason": "upcoming_releases_scan_enabled is false"}

    artists_limit = artists_limit or _feature_int("daily_musicbrainz_release_max_artists", 500)
    per_artist_limit = per_artist_limit or _feature_int("daily_musicbrainz_release_per_artist_limit", 100)
    if lookback_days is None:
        lookback_days = _feature_int("daily_musicbrainz_release_lookback_days", 42)
    if lookahead_days is None:
        # ~4 months: albums are usually announced well ahead of release
        # (singles only days/weeks out), so a short lookahead starves the
        # upcoming list of albums.  Tunable on the config page.
        lookahead_days = _feature_int("daily_musicbrainz_release_lookahead_days", 120)

    today = datetime.now().date()
    min_date = today - timedelta(days=max(1, min(lookback_days, 365)))
    max_date = today + timedelta(days=max(1, min(lookahead_days, 365)))

    client = _get_mb_client()
    stats: dict[str, Any] = {"artists_scanned": 0, "inserted": 0, "updated": 0, "global_inserted": 0, "global_updated": 0}

    # ── 1. Global discovery (releases from ANY artist) ───────────────────
    # The collection-artist scan below only surfaces releases for artists
    # already in the library.  Run a global upcoming-window query first so
    # brand-new MusicBrainz releases (from artists NOT in the catalogue) also
    # land in ``upcoming_releases`` — this is the "find new releases" the
    # user expects alongside Wikipedia + catalogue-artist matching.
    try:
        global_limit = _feature_int("daily_musicbrainz_release_global_limit", 50)
        if global_limit > 0:
            global_groups = _fetch_global_upcoming_release_groups(client, global_limit, min_date, max_date)
            if global_groups:
                # Attach the artist credit so persistence can flag
                # artist_in_collection correctly.
                for rg in global_groups:
                    rg["artist"] = _release_group_artist(rg)
                g_ins, g_upd = _persist_global_releases(global_groups)
                stats["global_inserted"] = g_ins
                stats["global_updated"] = g_upd
                stats["inserted"] += g_ins
                stats["updated"] += g_upd
                logger.info(
                    "[UPCOMING_MB] Global discovery: %d inserted, %d updated (of %d candidates)",
                    g_ins, g_upd, len(global_groups),
                )
    except Exception as exc:
        logger.warning("[UPCOMING_MB] Global discovery failed: %s", exc)

    artists = _collection_artists(artists_limit)
    if not artists:
        _set_status(status="idle", current_artist=None, last_stats=stats)
        return {**stats, "skipped": True, "reason": "no collection artists"}

    failed_artists: list[str] = []
    total_artists = len(artists)
    _set_status(status="running", progress=0, total=total_artists, current_artist=None)
    consecutive_failures = 0
    try:
        for artist in artists:
            stats["artists_scanned"] += 1
            try:
                rgs = _fetch_artist_release_groups(client, artist, per_artist_limit, min_date, max_date)
                new_count, upd_count = _persist_artist_releases(artist, rgs)
                stats["inserted"] += new_count
                stats["updated"] += upd_count
                consecutive_failures = 0
            except Exception as exc:
                consecutive_failures += 1
                failed_artists.append(artist)
                # The FIRST failure of a run surfaces at warning level so a
                # silent zero-insert run is diagnosable without debug logging;
                # repeats stay at debug (rate-limit errors usually repeat for
                # every artist).
                if len(failed_artists) == 1:
                    logger.warning("[UPCOMING_MB] Artist %s failed: %s", artist, exc)
                else:
                    logger.debug("[UPCOMING_MB] Artist %s failed: %s", artist, exc)
                if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                    logger.warning(
                        "[UPCOMING_MB] Aborting refresh after %s consecutive failures (last artist: %s)",
                        consecutive_failures, artist,
                    )
                    stats["aborted"] = True
                    stats["abort_reason"] = f"{consecutive_failures} consecutive failures"
                    break
            finally:
                _set_status(
                    progress=stats["artists_scanned"],
                    current_artist=artist,
                    stats=stats,
                )
    finally:
        _set_status(status="idle", current_artist=None, last_stats=stats)

    if failed_artists:
        stats["artists_failed"] = len(failed_artists)
        logger.warning(
            "[UPCOMING_MB] Refresh complete with %d/%d artists failed (inserted=%d, updated=%d)",
            len(failed_artists), total_artists, stats["inserted"], stats["updated"],
        )
    else:
        logger.info("[UPCOMING_MB] Refresh complete: %s", stats)
    return stats


def start_musicbrainz_refresh() -> bool:
    """Kick off the MusicBrainz collection refresh in a background thread.

    Returns False when a refresh is already running.  Runs in the background
    because a large library takes minutes (one throttled MusicBrainz request
    per artist) and the caller's HTTP request must return promptly.
    """
    global _refresh_running
    with _refresh_lock:
        if _refresh_running:
            return False
        _refresh_running = True

    def _run() -> None:
        global _refresh_running
        try:
            stats = fetch_musicbrainz_upcoming_releases()
            _set_status(status="idle", current_artist=None, last_stats=stats if isinstance(stats, dict) else None)
        except Exception as exc:
            logger.error("[UPCOMING_MB] Background refresh failed: %s", exc)
            _set_status(status="error", current_artist=None, last_error=str(exc))
        finally:
            with _refresh_lock:
                _refresh_running = False

    threading.Thread(target=_run, daemon=True, name="upcoming-mb-refresh").start()
    return True
