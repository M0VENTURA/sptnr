"""Upcoming releases routes — migrated from old app.py.

Uses ``services.enrichment.musicbrainz_service`` for all MusicBrainz lookups
to ensure proper rate limiting, User-Agent, and Lucene-escaping.
"""

from __future__ import annotations

import logging
from datetime import datetime
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


@upcoming_bp.route("", methods=["GET"])
def api_upcoming_releases():
    """Get upcoming releases with collection/queue annotations and pagination.

    Query params:
        filter (str): "all" (default), "collection" (artist/album in library),
                      or "discovered" (Wikipedia-sourced rows only).
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
        include_queue = request.args.get("include_queue", "").strip().lower() == "true"
        page = max(1, request.args.get("page", 1, type=int))
        limit = max(1, min(request.args.get("limit", 50, type=int), 200))

        with db_session() as session:
            total = session.execute(text("SELECT COUNT(*) FROM upcoming_releases")).scalar() or 0
            result = session.execute(
                text("SELECT * FROM upcoming_releases ORDER BY release_date ASC NULLS LAST, id ASC LIMIT :limit OFFSET :offset"),
                {"limit": limit + 1, "offset": (page - 1) * limit},
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

        # Batch: artists in collection
        artists_in_collection: set[str] = set()
        if artist_names:
            try:
                with db_session() as session:
                    placeholders = ", ".join(f":a{i}" for i in range(len(artist_names)))
                    params = {f"a{i}": name.lower() for i, name in enumerate(artist_names)}
                    batch = session.execute(
                        text(f"SELECT DISTINCT LOWER(COALESCE(NULLIF(album_artist, ''), artist)) AS aname FROM tracks WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) IN ({placeholders})"),
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
                        f"(LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = :a{i} AND LOWER(album) = :b{i})"
                        for i in range(len(album_pairs))
                    )
                    pair_params = {}
                    for i, (a, b) in enumerate(album_pairs):
                        pair_params[f"a{i}"] = a.lower()
                        pair_params[f"b{i}"] = b.lower()
                    batch2 = session.execute(
                        text(f"SELECT DISTINCT LOWER(COALESCE(NULLIF(album_artist, ''), artist)), LOWER(album) FROM tracks WHERE {pair_conditions}"),
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
                        f"(LOWER(artist) = :qa{i} AND LOWER(album) = :qb{i})"
                        for i in range(len(album_pairs))
                    )
                    q_params = {}
                    for i, (a, b) in enumerate(album_pairs):
                        q_params[f"qa{i}"] = a.lower()
                        q_params[f"qb{i}"] = b.lower()
                    batch3 = session.execute(
                        text(f"SELECT DISTINCT LOWER(artist), LOWER(album) FROM download_queue WHERE ({q_conditions}) AND status IN ('queued', 'downloading', 'processing')"),
                        q_params,
                    )
                    queued_pairs = {(row[0], row[1]) for row in batch3.fetchall()}
            except Exception:
                pass

        # Annotate each release using batch data
        for release in releases:
            artist_name = (release.get("artist_name") or "").lower()
            album_name = (release.get("album_name") or "").lower()
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
                session.execute(text("UPDATE upcoming_releases SET release_group_mbid = :mbid, match_source = :source, mbid_last_checked_at = :checked WHERE id = :id"),
                              {"mbid": rg_mbid, "source": source, "id": release_id, "checked": datetime.now().isoformat()})
            return jsonify({"success": True, "release_group_mbid": rg_mbid, "match_source": source})

        # ── Fallback: no MBID supplied → look the row up and search MB ──
        with db_session() as session:
            row = session.execute(
                text("SELECT id, artist_name, album_name FROM upcoming_releases WHERE id = :id"),
                {"id": release_id},
            ).fetchone()
        if row is None:
            return jsonify({"error": "release not found"}), 404

        artist = str(row[1] or "").strip()
        album = str(row[2] or "").strip()
        if not artist or not album:
            return jsonify({"error": "release has no artist/album to search"}), 400

        results = _search_musicbrainz_release_group(artist, album)
        if not results:
            return jsonify({"error": "no MusicBrainz release group found", "artist": artist, "album": album}), 404

        best = results[0]
        best_mbid = (best.get("id") or "").strip()
        if not best_mbid:
            return jsonify({"error": "no MusicBrainz release group found", "artist": artist, "album": album}), 404

        release_date = (best.get("first-release-date") or "")[:10]
        primary_type = (best.get("primary-type") or best.get("type") or "")
        with db_session() as session:
            session.execute(
                text("""UPDATE upcoming_releases
                        SET release_group_mbid = :mbid,
                            match_source = 'auto_search',
                            release_date = CASE
                                WHEN release_date IS NULL OR (:date IS NOT NULL AND :date < release_date)
                                    THEN :date
                                ELSE release_date
                            END,
                            primary_type = :ptype,
                            mbid_match_status = 'matched',
                            mbid_source = 'auto_search',
                            mbid_last_checked_at = :checked
                        WHERE id = :id"""),
                {"mbid": best_mbid, "date": release_date or None, "ptype": primary_type,
                 "id": release_id, "checked": datetime.now().isoformat()},
            )
        logger.info("Auto-matched upcoming release %s (%s - %s) -> %s", release_id, artist, album, best_mbid)
        return jsonify({"success": True, "release_group_mbid": best_mbid,
                        "match_source": "auto_search", "artist": artist, "album": album})
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
    """Refresh upcoming releases with MusicBrainz metadata."""
    try:
        with db_session() as session:
            result = session.execute(text("SELECT id, artist_name, album_name FROM upcoming_releases WHERE release_group_mbid IS NULL ORDER BY id"))
            rows = result.fetchall()

        updated = 0
        for row in rows:
            release_id = row[0]
            artist = row[1] or ""
            album = row[2] or ""
            if not artist or not album:
                continue

            try:
                results = _search_musicbrainz_release_group(artist, album)
                if results:
                    best = results[0]
                    rg_mbid = best.get("id")
                    release_date = (best.get("first-release-date") or "")[:10]
                    primary_type = (best.get("primary-type") or best.get("type") or "")
                    with db_session() as session:
                        session.execute(
                            text("""UPDATE upcoming_releases
                                   SET release_group_mbid = :mbid, release_date = :date, primary_type = :ptype
                                   WHERE id = :id"""),
                            {"mbid": rg_mbid, "date": release_date or None, "ptype": primary_type, "id": release_id},
                        )
                    updated += 1
            except Exception:
                continue

        logger.info("Refreshed %s upcoming releases from MusicBrainz", updated)
        return jsonify({"success": True, "message": f"Refreshed {updated} releases"})
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
