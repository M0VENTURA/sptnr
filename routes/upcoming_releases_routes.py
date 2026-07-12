"""Upcoming releases routes — migrated from old app.py.

Uses ``services.enrichment.musicbrainz_service`` for all MusicBrainz lookups
to ensure proper rate limiting, User-Agent, and Lucene-escaping.
"""

from __future__ import annotations

import logging
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
    """Get upcoming releases with collection/recommended artist annotations."""
    try:
        with db_session() as session:
            result = session.execute(text("SELECT * FROM upcoming_releases ORDER BY release_date ASC NULLS LAST LIMIT 100"))
            rows = result.fetchall()
        return jsonify({"success": True, "releases": [dict(r._mapping) for r in rows]})
    except Exception as exc:
        logger.error("Failed to fetch upcoming releases: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500


@upcoming_bp.route("/<int:release_id>/match", methods=["POST"])
async def api_match_upcoming_release(release_id):
    """Match an upcoming release to a MusicBrainz release-group."""
    data = (await request.get_json()) or {}
    rg_mbid = (data.get("release_group_mbid") or "").strip()
    source = (data.get("source") or "manual_selection").strip()

    if not rg_mbid:
        return jsonify({"error": "release_group_mbid is required"}), 400

    try:
        with db_session() as session:
            session.execute(text("UPDATE upcoming_releases SET release_group_mbid = :mbid, match_source = :source WHERE id = :id"),
                          {"mbid": rg_mbid, "source": source, "id": release_id})
        return jsonify({"success": True, "release_group_mbid": rg_mbid})
    except Exception as exc:
        logger.error("Failed to match upcoming release %s: %s", release_id, exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500


@upcoming_bp.route("/scrape", methods=["POST"])
def api_scrape_upcoming_releases():
    """Scrape Wikipedia for upcoming releases and store in DB."""
    from datetime import datetime

    try:
        headers = {"User-Agent": "Popularr/1.0", "Accept": "application/json"}
        # Fetch Wikipedia "List of upcoming albums" page
        wiki_url = "https://en.wikipedia.org/w/api.php"
        params = {
            "action": "parse",
            "page": "List_of_upcoming_albums",
            "format": "json",
            "prop": "text",
            "section": "0",
        }
        resp = httpx.get(wiki_url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        text = data.get("parse", {}).get("text", {}).get("*", "")

        if not text:
            return jsonify({"success": False, "error": "No content from Wikipedia"}), 500

        # Parse table rows — basic extraction of artist and album names
        import re
        rows_found = 0

        # Match table rows with artist and album cells
        to_insert = []
        for match in re.finditer(
            r'<tr>.*?<td>(.*?)</td>.*?<td><a[^>]*>(.*?)</a>.*?</tr>',
            text, re.DOTALL,
        ):
            artist_name = re.sub(r'<[^>]+>', '', match.group(1)).strip()
            album_title = re.sub(r'<[^>]+>', '', match.group(2)).strip()
            if artist_name and album_title and len(artist_name) < 200:
                to_insert.append((artist_name, album_title))
                rows_found += 1

        if to_insert:
            with db_session() as session:
                for artist_name, album_title in to_insert:
                    try:
                        session.execute(
                            text("""INSERT INTO upcoming_releases (artist_name, album_name, source, created_at)
                                   VALUES (:artist, :album, 'wikipedia', CURRENT_TIMESTAMP)
                                   ON CONFLICT DO NOTHING"""),
                            {"artist": artist_name, "album": album_title},
                        )
                    except Exception:
                        pass

        logger.info("Scraped %s upcoming releases from Wikipedia", rows_found)
        return jsonify({"success": True, "message": f"Scraped {rows_found} releases"})
    except Exception as exc:
        logger.error("Failed to scrape Wikipedia: %s", exc, exc_info=True)
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


@upcoming_bp.route("/release-group-tracks", methods=["GET"])
def api_get_release_group_tracks():
    """Fetch track listing for the first release in a release group."""
    rg_mbid = request.args.get("release_group_mbid", "").strip()
    if not rg_mbid:
        return jsonify({"error": "release_group_mbid required"}), 400
    try:
        client = _get_mb_client()
        releases = client.browse_releases_for_group(rg_mbid, inc="recordings+artist-credits", limit=1)
        if releases:
            # Fetch full release details with recordings
            first = releases[0]
            rid = first.get("id")
            if rid:
                release_data = client.get_release(rid, inc="recordings+artist-credits")
                return jsonify({"success": True, "release": release_data})
        return jsonify({"success": True, "releases": releases})
    except Exception as exc:
        logger.error("Failed to fetch release group tracks: %s", exc, exc_info=True)
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


@upcoming_bp.route("/search", methods=["POST"])
def api_search_upcoming_release():
    """Search for downloads of an upcoming release (placeholder)."""
    return jsonify({"success": True, "results": []}), 200
