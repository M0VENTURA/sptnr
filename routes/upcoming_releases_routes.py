"""Upcoming releases routes — migrated from old app.py.

Uses ``services.enrichment.musicbrainz_service`` for all MusicBrainz lookups
to ensure proper rate limiting, User-Agent, and Lucene-escaping.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any

from quart import Blueprint, jsonify, request

from sqlalchemy import text
from db.engine import db_session
from helpers.response_helpers import _ok, _fail
from api_clients.musicbrainz_http import MusicBrainzHttpClient, escape_lucene_special_chars

logger = logging.getLogger(__name__)

upcoming_bp = Blueprint("upcoming_releases", __name__, url_prefix="/api/upcoming-releases")

# Shared MusicBrainz client (lazy-init, respects 1 req/sec rate limit)
_mb_client: MusicBrainzHttpClient | None = None


def _get_mb_client() -> MusicBrainzHttpClient:
    global _mb_client
    if _mb_client is None:
        _mb_client = MusicBrainzHttpClient(enabled=True)
    return _mb_client


def _search_musicbrainz_release_group(artist: str, album: str, track: str = "") -> list[dict[str, Any]]:
    """Search MusicBrainz release-groups with proper rate limiting and escaping."""
    client = _get_mb_client()
    parts = []
    if artist:
        parts.append(f'artist:"{escape_lucene_special_chars(artist)}"')
    if album:
        parts.append(f'release:"{escape_lucene_special_chars(album)}"')
    if track:
        parts.append(f'recording:"{escape_lucene_special_chars(track)}"')
    query = " AND ".join(parts) if parts else (escape_lucene_special_chars(artist or album or track))
    return client.search_release_groups(query, limit=20)


def _persist_match_result(release_id: int, result: dict[str, Any]) -> None:
    """Persist a pipeline match outcome (matched / candidate / unmatched).

    - matched:   release_group_mbid set, high confidence, linked.
    - candidate: candidate_mbid stored WITHOUT linking (UI offers one-click
      confirm) so queue flows never act on an unconfirmed match.
    - unmatched: cleared back to text-only.
    """
    status = result.get("status", "unmatched")
    score = float(result.get("score") or 0.0)
    now = datetime.now().isoformat()
    with db_session() as session:
        if status == "matched":
            best = result["candidates"][0]
            release_date = (best.get("first_release_date") or "")[:10]
            primary_type = str(best.get("primary_type") or "")
            session.execute(
                text("""UPDATE upcoming_releases SET
                        release_group_mbid = :mbid,
                        match_source = 'auto_match',
                        mbid_match_status = 'matched',
                        mbid_match_score = :score,
                        mbid_confidence = 'high',
                        mbid_source = 'auto_match',
                        mbid_last_checked_at = :checked,
                        candidate_release_group_mbid = NULL,
                        release_date = CASE
                            WHEN release_date IS NULL OR (:date IS NOT NULL AND :date < release_date)
                                THEN :date
                            ELSE release_date
                        END,
                        primary_type = :ptype
                    WHERE id = :id"""),
                {"mbid": best["mbid"], "score": score, "checked": now,
                 "date": release_date or None, "ptype": primary_type, "id": release_id},
            )
        elif status == "candidate":
            best = result["candidates"][0]
            session.execute(
                text("""UPDATE upcoming_releases SET
                        candidate_release_group_mbid = :mbid,
                        mbid_match_status = 'candidate',
                        mbid_match_score = :score,
                        mbid_confidence = 'medium',
                        mbid_source = 'auto_candidate',
                        mbid_last_checked_at = :checked
                    WHERE id = :id"""),
                {"mbid": best["mbid"], "score": score, "checked": now, "id": release_id},
            )
        else:
            session.execute(
                text("""UPDATE upcoming_releases SET
                        mbid_match_status = 'unmatched',
                        mbid_match_score = :score,
                        mbid_confidence = NULL,
                        mbid_source = NULL,
                        mbid_last_checked_at = :checked,
                        candidate_release_group_mbid = NULL
                    WHERE id = :id"""),
                {"score": score, "checked": now, "id": release_id},
            )


@upcoming_bp.route("", methods=["GET"])
def api_upcoming_releases():
    """Get upcoming releases with collection/queue annotations and pagination.

    Query params:
        filter (str): "all" (default), "collection" (artist/album in library),
                      or "discovered" (Wikipedia-sourced rows only).
        source (str): Optional exact scraper-rule key to filter by
                      (e.g. "2026_kpop", or "musicbrainz_daily_collection" for
                      the MusicBrainz daily scan rows).
        include_queue (str): If "true", include in_queue flag per release.
        page (int): 1-based page number (default 1).
        limit (int): Page size (default 50, max 200).
    """
    try:
        release_filter = (request.args.get("filter", "") or "all").strip().lower()
        if release_filter not in ("all", "collection", "discovered"):
            release_filter = "all"
        # Legacy alias used by queue.html/monitor.js: ?collection=true means
        # "artists already in my library only".
        if (request.args.get("collection") or "").strip().lower() == "true":
            release_filter = "collection"
        source_filter = (request.args.get("source", "") or "").strip()
        include_queue = request.args.get("include_queue", "").strip().lower() == "true"
        page = max(1, request.args.get("page", 1, type=int))
        limit = max(1, min(request.args.get("limit", 50, type=int), 200))
        # Hide albums that already exist in the local library (normalized
        # artist+album match).  Default ON — the feed shows only new drops.
        hide_in_library = request.args.get("hide_in_library", "1").strip().lower() != "0"
        # Optional tight window (days): dashboard snapshot uses ±7 so only
        # releases from last week → next week are queried.  ``total`` keeps
        # the FULL rolling-window count so the dashboard can render a
        # "View All N" link to the full manager.
        window_days = max(0, request.args.get("window", 0, type=int))

        where_sql = ""
        params: dict[str, Any] = {}
        if source_filter and source_filter != "all":
            if source_filter == "musicbrainz_daily_collection":
                where_sql = " WHERE source = 'MusicBrainz Daily Collection'"
            else:
                # Matches both new rows (source_key) and legacy rows scraped
                # before source_key existed (fall back to the source label).
                where_sql = " WHERE COALESCE(source_key, source) = :source_key"
                params["source_key"] = source_filter

        # Rolling display window: last N months → next M months from today
        # (the same window the Wikipedia scraper imports).  Undated (TBA)
        # rows are kept so genuinely unscheduled releases stay visible, but
        # out-of-window dated rows (e.g. the January block when the current
        # month is August) are hidden.
        try:
            from services.upcoming_releases.wikipedia_scraper_service import get_release_window
            _win_start, _win_end = get_release_window()
            _date_clause = (
                " (release_date IS NULL OR release_date BETWEEN :win_start AND :win_end)"
            )
            where_sql = (
                f" WHERE {_date_clause}"
                if not where_sql
                else where_sql + f" AND {_date_clause}"
            )
            params["win_start"] = _win_start.strftime("%Y-%m-%d")
            params["win_end"] = _win_end.strftime("%Y-%m-%d")
        except Exception:
            pass

            # Hide releases whose (artist, album) already exists in the local
            # library — normalized comparison (case + punctuation-insensitive)
            # so "Tanzneid" vs "TANZNEID" never slips through as a new release.
            if hide_in_library:
                _lib_clause = """NOT EXISTS (
                    SELECT 1 FROM tracks t
                    WHERE LOWER(REGEXP_REPLACE(
                              COALESCE(NULLIF(t.album_artist, ''), t.artist),
                              '[^a-zA-Z0-9]', '', 'g'))
                          = LOWER(REGEXP_REPLACE(upcoming_releases.artist_name,
                              '[^a-zA-Z0-9]', '', 'g'))
                      AND LOWER(REGEXP_REPLACE(t.album, '[^a-zA-Z0-9]', '', 'g'))
                          = LOWER(REGEXP_REPLACE(upcoming_releases.album_name,
                              '[^a-zA-Z0-9]', '', 'g'))
                )"""
                where_sql = (
                    f" WHERE {_lib_clause}"
                    if not where_sql
                    else where_sql + f" AND {_lib_clause}"
                )
                params["win7_start"] = _w_start
                params["win7_end"] = _w_end

            result = session.execute(
                text(f"SELECT * FROM upcoming_releases{where_sql} ORDER BY release_date ASC NULLS LAST, id ASC LIMIT :limit OFFSET :offset"),
                {**params, "limit": limit + 1, "offset": (page - 1) * limit},
            )
            rows = result.fetchall()

        has_more = len(rows) > limit
        rows = rows[:limit]
        releases = [dict(r._mapping) for r in rows]

        # "discovered" tab = Wikipedia-sourced rows (everything that is not the
        # MusicBrainz daily collection scan).
        if release_filter == "discovered":
            releases = [r for r in releases if (r.get("source") or "") != "MusicBrainz Daily Collection"]

        artist_names = list({r.get("artist_name", "") or "" for r in releases if r.get("artist_name")})

        # Normalized comparison key (lowercase, punctuation-stripped) so
        # casing/punctuation differences never break library matching.
        def _norm(value: str) -> str:
            return re.sub(r"[^a-zA-Z0-9]", "", (value or "").lower())

        # Batch: artists in collection
        artists_in_collection: set[str] = set()
        if artist_names:
            try:
                with db_session() as session:
                    placeholders = ", ".join(f":a{i}" for i in range(len(artist_names)))
                    params = {f"a{i}": _norm(name) for i, name in enumerate(artist_names)}
                    batch = session.execute(
                        text(f"""SELECT DISTINCT LOWER(REGEXP_REPLACE(
                                      COALESCE(NULLIF(album_artist, ''), artist),
                                      '[^a-zA-Z0-9]', '', 'g')) AS aname
                                FROM tracks
                                WHERE LOWER(REGEXP_REPLACE(
                                      COALESCE(NULLIF(album_artist, ''), artist),
                                      '[^a-zA-Z0-9]', '', 'g')) IN ({placeholders})"""),
                        params,
                    )
                    artists_in_collection = {row[0] for row in batch.fetchall()}
            except Exception:
                pass

        # Batch: albums in collection (artist + album pairs)
        album_pairs = [(r.get("artist_name", "") or "", r.get("album_name", "") or "")
                       for r in releases if r.get("artist_name") and r.get("album_name")]
        albums_in_collection: set[tuple[str, str]] = set()
        if album_pairs:
            try:
                with db_session() as session:
                    pair_conditions = " OR ".join(
                        f"(LOWER(REGEXP_REPLACE(COALESCE(NULLIF(album_artist, ''), artist), '[^a-zA-Z0-9]', '', 'g')) = :a{i} "
                        f"AND LOWER(REGEXP_REPLACE(album, '[^a-zA-Z0-9]', '', 'g')) = :b{i})"
                        for i in range(len(album_pairs))
                    )
                    pair_params = {}
                    for i, (a, b) in enumerate(album_pairs):
                        pair_params[f"a{i}"] = _norm(a)
                        pair_params[f"b{i}"] = _norm(b)
                    batch2 = session.execute(
                        text(f"""SELECT DISTINCT
                                LOWER(REGEXP_REPLACE(COALESCE(NULLIF(album_artist, ''), artist), '[^a-zA-Z0-9]', '', 'g')),
                                LOWER(REGEXP_REPLACE(album, '[^a-zA-Z0-9]', '', 'g'))
                              FROM tracks WHERE {pair_conditions}"""),
                        pair_params,
                    )
                    albums_in_collection = {(row[0], row[1]) for row in batch2.fetchall()}
            except Exception:
                pass

        # Batch: queue status
        queued_pairs: set[tuple[str, str]] = set()
        if include_queue and album_pairs:
            try:
                with db_session() as session:
                    q_conditions = " OR ".join(
                        f"(LOWER(REGEXP_REPLACE(artist, '[^a-zA-Z0-9]', '', 'g')) = :qa{i} "
                        f"AND LOWER(REGEXP_REPLACE(album, '[^a-zA-Z0-9]', '', 'g')) = :qb{i})"
                        for i in range(len(album_pairs))
                    )
                    q_params = {}
                    for i, (a, b) in enumerate(album_pairs):
                        q_params[f"qa{i}"] = _norm(a)
                        q_params[f"qb{i}"] = _norm(b)
                    batch3 = session.execute(
                        text(f"""SELECT DISTINCT
                                LOWER(REGEXP_REPLACE(artist, '[^a-zA-Z0-9]', '', 'g')),
                                LOWER(REGEXP_REPLACE(album, '[^a-zA-Z0-9]', '', 'g'))
                              FROM download_queue
                              WHERE ({q_conditions}) AND status IN ('queued', 'downloading', 'processing')"""),
                        q_params,
                    )
                    queued_pairs = {(row[0], row[1]) for row in batch3.fetchall()}
            except Exception:
                pass

        # Annotate each release using batch data
        for release in releases:
            artist_name = _norm(release.get("artist_name"))
            album_name = _norm(release.get("album_name"))
            release["artist_in_collection"] = artist_name in artists_in_collection
            release["album_in_collection"] = (artist_name, album_name) in albums_in_collection
            if include_queue and artist_name and album_name:
                in_queue = (artist_name, album_name) in queued_pairs
                release["in_queue"] = in_queue
                release["queue_status"] = "queued" if in_queue else None
            else:
                release["in_queue"] = False
                release["queue_status"] = None

        if release_filter == "collection":
            releases = [r for r in releases if r.get("artist_in_collection")]

        return jsonify({
            "success": True,
            "releases": releases,
            "total": total,
            "page": page,
            "limit": limit,
            "has_more": has_more,
        })
    except Exception as exc:
        logger.error("Failed to fetch upcoming releases: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500


@upcoming_bp.route("/sources", methods=["GET"])
def api_upcoming_sources():
    """Distinct release sources with counts, for the Source filter dropdown.

    Wikipedia rows expose their exact scraper-rule key (e.g. ``2026_kpop``);
    MusicBrainz rows are grouped under the ``musicbrainz_daily_collection``
    pseudo-key so the filter can select them too.
    """
    try:
        with db_session() as session:
            rows = session.execute(
                text("""
                    SELECT
                        CASE
                            WHEN source = 'MusicBrainz Daily Collection' THEN 'musicbrainz_daily_collection'
                            ELSE COALESCE(source_key, source)
                        END AS key,
                        CASE
                            WHEN source = 'MusicBrainz Daily Collection' THEN 'MusicBrainz Daily Collection'
                            ELSE COALESCE(source_key, source)
                        END AS label,
                        COUNT(*) AS count
                    FROM upcoming_releases
                    GROUP BY key, label
                    ORDER BY count DESC, label ASC
                """)
            ).fetchall()
        sources = [{"key": str(r[0]), "label": str(r[1]), "count": int(r[2] or 0)} for r in rows]
        return jsonify({"success": True, "sources": sources})
    except Exception as exc:
        logger.error("Failed to fetch upcoming release sources: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500


@upcoming_bp.route("/<int:release_id>/match", methods=["POST"])
async def api_match_upcoming_release(release_id):
    """Match an upcoming release to a MusicBrainz release-group.

    When ``release_group_mbid`` is omitted, falls back to a server-side
    MusicBrainz search using the stored artist/album names and stores the best
    match (``match_source = 'auto_search'``).  404 when nothing is found.
    """
    data = (await request.get_json(silent=True)) or {}
    rg_mbid = (data.get("release_group_mbid") or "").strip()
    source = (data.get("source") or "manual_selection").strip()

    try:
        if rg_mbid:
            with db_session() as session:
                session.execute(
                    text("""UPDATE upcoming_releases SET
                            release_group_mbid = :mbid,
                            match_source = :source,
                            mbid_match_status = 'matched',
                            mbid_confidence = 'high',
                            mbid_source = :source,
                            mbid_last_checked_at = :checked,
                            candidate_release_group_mbid = NULL
                        WHERE id = :id"""),
                    {"mbid": rg_mbid, "source": source, "id": release_id, "checked": datetime.now().isoformat()},
                )
            return jsonify({"success": True, "release_group_mbid": rg_mbid, "match_source": source})

        # ── Fallback: no MBID supplied → run the two-pass matching pipeline ──
        with db_session() as session:
            row = session.execute(
                text("SELECT id, artist_name, album_name, release_date FROM upcoming_releases WHERE id = :id"),
                {"id": release_id},
            ).fetchone()
        if row is None:
            return jsonify({"error": "release not found"}), 404

        artist = str(row[1] or "").strip()
        album = str(row[2] or "").strip()
        if not artist or not album:
            return jsonify({"error": "release has no artist/album to search"}), 400

        from services.upcoming_releases.matching_service import match_to_musicbrainz
        result = match_to_musicbrainz(artist, album, str(row[3] or "").strip() or None)
        _persist_match_result(release_id, result)

        return jsonify({
            "success": True,
            "status": result["status"],
            "score": result["score"],
            "release_group_mbid": result["mbid"],
            "artist": artist,
            "album": album,
            "candidates": result["candidates"],
        })
    except Exception as exc:
        logger.error("Failed to match upcoming release %s: %s", release_id, exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500


@upcoming_bp.route("/scrape", methods=["POST"])
def api_scrape_upcoming_releases():
    """Scrape Wikipedia for upcoming releases and store in DB.

    Also kicks off the MusicBrainz collection refresh (background thread —
    large libraries take minutes, so the request returns immediately with
    the Wikipedia results while the MusicBrainz pass runs and logs progress).
    """
    from services.upcoming_releases.wikipedia_scraper_service import scrape

    try:
        results = scrape()
        total = results.get("total_found", 0)
        new_count = results.get("total_new", 0)
        updated_count = results.get("total_updated", 0)

        # MusicBrainz upcoming/recent releases for collection artists
        # (augments the Wikipedia sources with direct MB release-groups).
        mb_started = False
        try:
            from services.upcoming_releases.musicbrainz_fetcher_service import start_musicbrainz_refresh
            mb_started = start_musicbrainz_refresh()
        except Exception as exc:
            logger.debug("MusicBrainz upcoming refresh start failed: %s", exc)

        message = f"Scraped {total} releases ({new_count} new, {updated_count} updated)"
        message += "; MusicBrainz refresh " + ("started" if mb_started else "already running")

        logger.info(
            "Scraped %s upcoming releases (%s new, %s updated)",
            total, new_count, updated_count,
        )
        return jsonify({
            "success": True,
            "message": message,
            "results": results,
            "musicbrainz": {"started": mb_started},
        })
    except Exception as exc:
        logger.error("Failed to scrape Wikipedia: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500


@upcoming_bp.route("/scrape/status", methods=["GET"])
def api_upcoming_scrape_status():
    """Live status of the background MusicBrainz refresh.

    Returns ``{status: "running"|"idle"|"error", progress, total,
    current_artist, updated_at, last_stats}`` — the UI polls this while the
    refresh is running to render a "Refreshing… (142/500)" badge.
    """
    try:
        from services.upcoming_releases.musicbrainz_fetcher_service import get_refresh_status
        return jsonify(get_refresh_status())
    except Exception as exc:
        logger.error("Failed to read scrape status: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500


@upcoming_bp.route("/refresh-musicbrainz", methods=["POST"])
def api_refresh_upcoming_releases_musicbrainz():
    """Refresh upcoming releases with MusicBrainz metadata.

    Runs the two-pass matching pipeline (sanitize → local MBID / global search
    → rapidfuzz scoring) over every row without a linked release-group.
    Scores ≥0.85 auto-link; 0.65–0.85 are stored as candidates for one-click
    confirmation; manual overrides are never touched.
    """
    try:
        with db_session() as session:
            result = session.execute(
                text("""SELECT id, artist_name, album_name, release_date
                        FROM upcoming_releases
                       WHERE release_group_mbid IS NULL
                         AND COALESCE(mbid_manual_override, FALSE) = FALSE
                       ORDER BY id""")
            )
            rows = result.fetchall()

        from services.upcoming_releases.matching_service import match_to_musicbrainz

        matched = 0
        candidates = 0
        unmatched = 0
        for row in rows:
            release_id = row[0]
            artist = row[1] or ""
            album = row[2] or ""
            release_date = row[3] or ""
            if not artist or not album:
                continue

            try:
                mresult = match_to_musicbrainz(artist, album, str(release_date).strip() or None)
            except Exception:
                continue

            _persist_match_result(release_id, mresult)
            if mresult["status"] == "matched":
                matched += 1
            elif mresult["status"] == "candidate":
                candidates += 1
            else:
                unmatched += 1

        logger.info(
            "Refreshed %s upcoming releases from MusicBrainz (%s matched, %s candidates, %s unmatched)",
            len(rows), matched, candidates, unmatched,
        )
        return jsonify({
            "success": True,
            "message": f"Refreshed {len(rows)} releases ({matched} matched, {candidates} candidates)",
            "matched": matched,
            "candidates": candidates,
            "unmatched": unmatched,
            "total": len(rows),
        })
    except Exception as exc:
        logger.error("Failed to refresh from MusicBrainz: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500


@upcoming_bp.route("/clear", methods=["POST"])
def api_clear_upcoming_releases():
    """Clear all upcoming releases from the database."""
    try:
        with db_session() as session:
            session.execute(text("DELETE FROM upcoming_releases"))
        return jsonify({"success": True})
    except Exception as exc:
        logger.error("Failed to clear upcoming releases: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500


@upcoming_bp.route("/search-musicbrainz", methods=["POST"])
async def api_search_musicbrainz_release():
    """Search MusicBrainz for a release (artist, album, or track)."""
    data = (await request.get_json()) or {}
    artist = data.get("artist", "").strip()
    album = data.get("album", "").strip()
    track = data.get("track", "").strip()
    if not artist and not album and not track:
        return jsonify({"error": "provide artist, album, or track"}), 400

    try:
        results = _search_musicbrainz_release_group(artist, album, track)
        return jsonify({"success": True, "results": results})
    except Exception as exc:
        logger.error("MusicBrainz search failed: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500


@upcoming_bp.route("/search-discogs", methods=["POST"])
async def api_search_discogs_release():
    """Search Discogs for a release."""
    data = (await request.get_json()) or {}
    artist = (data.get("artist") or "").strip()
    album = (data.get("album") or "").strip()
    if not artist or not album:
        return jsonify({"success": True, "results": []})
    try:
        from helpers.config_helpers import get_api_integration
        from api_clients.discogs_http import DiscogsHttpClient

        discogs_cfg = get_api_integration("discogs")
        token = discogs_cfg.get("token") or ""
        if token:
            client = DiscogsHttpClient(token=token)
            params = {"q": f"{artist} {album}", "type": "release", "per_page": 10}
            results = client.search_database(params)
            return jsonify({"success": True, "results": results})
        return jsonify({"success": True, "results": []})
    except Exception as exc:
        logger.error("Discogs search failed: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500
