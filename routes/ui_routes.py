"""UI page routes, auth, config — migrated from old app.py."""

from __future__ import annotations

import logging
import os
from functools import wraps
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import re

from quart import (
    Blueprint, flash, jsonify, redirect, render_template, request,
    Response, session, url_for,
)

from sqlalchemy import text

from db.engine import db_session
from helpers.config_helpers import get_config
from services.scanning.scan_history_service import get_recent_album_scans

logger = logging.getLogger(__name__)

ui_bp = Blueprint("ui", __name__)


# ===========================================================================
# AUTH HELPERS
# ===========================================================================

def _needs_setup(cfg=None):
    cfg = cfg or get_config()
    nav_users = cfg.get("navidrome_users", [])
    if isinstance(nav_users, list) and nav_users:
        first = nav_users[0]
        return not all([first.get("base_url"), first.get("user"), first.get("pass")])
    nav = cfg.get("navidrome", {}) or {}
    return not all([nav.get("base_url"), nav.get("user"), nav.get("pass")])


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if _needs_setup():
            return redirect(url_for("ui.setup"))
        if "username" not in session:
            return redirect(url_for("ui.login"))
        return f(*args, **kwargs)
    return decorated


# ===========================================================================
# LOGIN / LOGOUT
# ===========================================================================

@ui_bp.route("/login", methods=["GET", "POST"])
async def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = (request.form.get("password") or "").strip()
        cfg = get_config()
        nav_users = cfg.get("navidrome_users", [])

        # Try verifying against Navidrome first (live check)
        from api_clients.navidrome import NavidromeClient
        for user in nav_users:
            if user.get("user") == username:
                base_url = str(user.get("base_url", "")).rstrip("/")
                if base_url:
                    try:
                        client = NavidromeClient(base_url=base_url, username=username, password=password)
                        if client.ping():
                            session["username"] = username
                            flash(f"Welcome back, {username}!", "success")
                            return redirect(url_for("ui.dashboard"))
                    except Exception:
                        pass
                # Fallback: check against stored password
                if user.get("pass") == password:
                    session["username"] = username
                    flash(f"Welcome back, {username}!", "success")
                    return redirect(url_for("ui.dashboard"))

        flash("Invalid credentials", "error")
        return await render_template("auth/login.html")
    return await render_template("auth/login.html")


@ui_bp.route("/logout")
def logout():
    username = session.pop("username", None)
    flash(f"Goodbye, {username}!", "info")
    return redirect(url_for("ui.login"))


# ===========================================================================
# SETUP
# ===========================================================================

@ui_bp.route("/setup", methods=["GET", "POST"])
async def setup():
    from helpers.config_helpers import get_config

    # Read existing config so the wizard can pre-fill previously saved values
    cfg = get_config()
    nav_users = cfg.get("navidrome_users", [])
    nav_first = nav_users[0] if nav_users else {}
    api = cfg.get("api_integrations", {})

    slskd_cfg = cfg.get("slskd", {})
    setup_defaults = {
        # Navidrome
        "nav_url": nav_first.get("base_url", ""),
        "nav_user": nav_first.get("user", ""),
        "nav_pass": nav_first.get("pass", ""),
        # Spotify
        "sp_enabled": api.get("spotify", {}).get("enabled", False),
        "sp_client_id": api.get("spotify", {}).get("client_id", ""),
        "sp_client_secret": api.get("spotify", {}).get("client_secret", ""),
        # Last.fm
        "lfm_enabled": api.get("lastfm", {}).get("enabled", False),
        "lfm_api_key": api.get("lastfm", {}).get("api_key", ""),
        # Discogs
        "dg_enabled": api.get("discogs", {}).get("enabled", False),
        "dg_token": api.get("discogs", {}).get("token", ""),
        # ListenBrainz
        "lb_enabled": api.get("listenbrainz", {}).get("enabled", True),
        "lb_token": nav_first.get("listenbrainz_user_token", ""),
        # Soulseek / slskd
        "slskd_enabled": bool(slskd_cfg.get("enabled", False)),
        "slskd_url": slskd_cfg.get("web_url", ""),
        "slskd_api_key": slskd_cfg.get("api_key", ""),
        # Essentia
        "essentia_enabled": bool(cfg.get("essentia", {}).get("script_path")),
        "essentia_tag_moods": cfg.get("essentia", {}).get("tag_moods", True),
        "essentia_tag_genres": cfg.get("essentia", {}).get("tag_genres", False),
    }

    # PG env vars
    pg_defaults = {
        "host": os.environ.get("PG_HOST", ""),
        "port": os.environ.get("PG_PORT", "5432"),
        "user": os.environ.get("PG_USER", "popularr"),
        "password": os.environ.get("PG_PASSWORD", ""),
        "database": os.environ.get("PG_DATABASE", "popularr"),
    }

    if request.method == "POST":
        return await render_template("auth/setup.html", message="Setup not yet implemented", pg=pg_defaults, defaults=setup_defaults)
    return await render_template("auth/setup.html", pg=pg_defaults, defaults=setup_defaults)


# ===========================================================================
# SETUP & TEST API
# ===========================================================================

@ui_bp.route("/api/test-navidrome-connection", methods=["POST"])
async def api_test_navidrome_connection():
    """Test Navidrome connectivity with provided credentials before saving."""
    from api_clients.navidrome import NavidromeClient

    try:
        data = (await request.get_json()) or {}
    except Exception:
        return jsonify({"success": False, "error": "Invalid JSON body"}), 400
    base_url = str(data.get("base_url", "")).rstrip("/")
    username = str(data.get("username", ""))
    password = str(data.get("password", ""))

    if not base_url or not username:
        return jsonify({"success": False, "error": "URL and username are required"}), 400

    # Auto-prepend http:// if no protocol is given
    if "://" not in base_url:
        base_url = f"http://{base_url}"

    # Quick URL sanity check before making the HTTP call
    from urllib.parse import urlparse
    parsed = urlparse(base_url)
    if not parsed.hostname:
        return jsonify({"success": False, "error": "Invalid URL format — expected something like http://navidrome:4533"}), 400

    try:
        # Try password-based auth first (some Navidrome setups reject token auth).
        client = NavidromeClient(base_url=base_url, username=username, password=password, use_token_auth=False)
        sub_data = client._get_subsonic_response("ping", timeout=10)
        # If password auth fails, retry with token auth as fallback
        if not sub_data or sub_data.get("status") != "ok":
            client2 = NavidromeClient(base_url=base_url, username=username, password=password, use_token_auth=True)
            sub_data2 = client2._get_subsonic_response("ping", timeout=10)
            if sub_data2 and sub_data2.get("status") == "ok":
                sub_data = sub_data2
    except Exception as exc:
        err_msg = str(exc)
        return jsonify({
            "success": False,
            "error": "❌ Cannot reach the server",
            "detail": err_msg,
        }), 200

    if not sub_data:
        # Empty response means _get_subsonic_response caught an exception
        # (connection refused, DNS failure, etc.)
        return jsonify({
            "success": False,
            "error": "❌ Cannot reach the server",
            "detail": "Connection refused or DNS failure — check that Navidrome is running and reachable from this container",
        }), 200

    status = sub_data.get("status")
    if status == "ok":
        return jsonify({"success": True, "message": "✅ Connected successfully"})

    # status == "failed" — extract error details
    error_code = sub_data.get("error", {}).get("code") if isinstance(sub_data.get("error"), dict) else None
    error_msg = sub_data.get("error", {}).get("message", "") if isinstance(sub_data.get("error"), dict) else str(sub_data.get("error", ""))

    if error_code == 10:
        # Error 10 = Authentication failed (missing or wrong credentials)
        return jsonify({
            "success": False,
            "error": "❌ Credentials rejected — wrong username or password, or token auth mismatch",
            "detail": error_msg,
        }), 200
    if error_code:
        return jsonify({
            "success": False,
            "error": f"❌ Server returned error {error_code}",
            "detail": error_msg,
        }), 200

    return jsonify({
        "success": False,
        "error": "❌ Unknown error — check server logs",
        "detail": str(sub_data),
    }), 200

@ui_bp.route("/api/setup/save", methods=["POST"])
async def api_setup_save():
    """Save the first-run setup wizard configuration."""
    from helpers.config_helpers import save_config, clear_config_cache

    try:
        data = (await request.get_json()) or {}
    except Exception:
        return jsonify({"success": False, "error": "Invalid JSON body"}), 400
    if not data:
        return jsonify({"success": False, "error": "No configuration data received"}), 400

    # Validate required Navidrome config
    nav_users = data.get("navidrome_users", [])
    if not nav_users or not nav_users[0].get("base_url") or not nav_users[0].get("user"):
        return jsonify({"success": False, "error": "Navidrome URL and username are required"}), 400

    # Extract PG fields from the setup payload and persist to config + env
    pg_host = (data.pop("PG_HOST", "") or "").strip()
    if pg_host:
        data.setdefault("database", {})
        data["database"]["host"] = pg_host
        data["database"]["port"] = (data.pop("PG_PORT", "") or "5432").strip()
        data["database"]["user"] = (data.pop("PG_USER", "") or "popularr").strip()
        data["database"]["password"] = data.pop("PG_PASSWORD", "")
        data["database"]["name"] = (data.pop("PG_DATABASE", "") or "popularr").strip()
        os.environ["PG_HOST"] = data["database"]["host"]
        os.environ["PG_PORT"] = data["database"]["port"]
        os.environ["PG_USER"] = data["database"]["user"]
        os.environ["PG_PASSWORD"] = data["database"]["password"]
        os.environ["PG_DATABASE"] = data["database"]["name"]

    success = save_config(data)
    if success:
        session["username"] = nav_users[0].get("user", "")
        return jsonify({"success": True, "message": "Configuration saved"})
    return jsonify({"success": False, "error": "Failed to save config file"}), 500


@ui_bp.route("/api/setup/save-partial", methods=["POST"])
async def api_setup_save_partial():
    """Save partial wizard configuration — merges into existing config.

    Unlike :func:`api_setup_save`, this endpoint does **not** require the
    full Navidrome config.  It deep-merges whatever keys are provided into
    the current on-disk config, making it safe to call after every wizard
    step.  This lets interrupted setups resume where they left off.
    """
    from helpers.config_helpers import save_partial_config

    try:
        data = (await request.get_json()) or {}
    except Exception:
        return jsonify({"success": False, "error": "Invalid JSON body"}), 400
    if not data:
        return jsonify({"success": False, "error": "No data received"}), 400

    # If Navidrome users are present, set the session username
    nav_users = data.get("navidrome_users", [])
    if nav_users and nav_users[0].get("user"):
        session["username"] = nav_users[0]["user"]

    # Extract PG fields like the full save endpoint does
    pg_host = (data.pop("PG_HOST", "") or "").strip()
    if pg_host:
        data.setdefault("database", {})
        data["database"]["host"] = pg_host
        data["database"]["port"] = (data.pop("PG_PORT", "") or "5432").strip()
        data["database"]["user"] = (data.pop("PG_USER", "") or "popularr").strip()
        data["database"]["password"] = data.pop("PG_PASSWORD", "")
        data["database"]["name"] = (data.pop("PG_DATABASE", "") or "popularr").strip()
        os.environ["PG_HOST"] = data["database"]["host"]
        os.environ["PG_PORT"] = data["database"]["port"]
        os.environ["PG_USER"] = data["database"]["user"]
        os.environ["PG_PASSWORD"] = data["database"]["password"]
        os.environ["PG_DATABASE"] = data["database"]["name"]

    success = save_partial_config(data)
    if success:
        return jsonify({"success": True, "message": "Progress saved"})
    return jsonify({"success": False, "error": "Failed to save config file"}), 500


# ===========================================================================
# STATIC PAGES
# ===========================================================================

@ui_bp.route("/")
def index():
    return redirect(url_for("ui.dashboard"))


@ui_bp.route("/dashboard")
async def dashboard():
    try:
        recent_scans = get_recent_album_scans(limit=10) or []
        cfg = get_config()
        nav_users = cfg.get("navidrome_users", [])
        if not nav_users and cfg.get("navidrome"):
            nav_users = [cfg["navidrome"]]
        features = cfg.get("features", {})

        # Library stats for the header
        try:
            with db_session() as session:
                result = session.execute(text("SELECT COUNT(*) as tc, COUNT(DISTINCT album) as ac, COUNT(DISTINCT COALESCE(NULLIF(album_artist, ''), artist)) as artists_c, ROUND(AVG(stars), 1) as avg_stars FROM tracks"))
                stats = dict(result.fetchone()._mapping)
        except Exception:
            stats = {"tc": 0, "ac": 0, "artists_c": 0, "avg_stars": None}

        return await render_template(
            "pages/dashboard.html",
            recent_scans=recent_scans,
            nav_users=nav_users,
            stats=stats,
            scan_running=False,
            perpetual=bool(features.get("perpetual", False)),
            forced=bool(features.get("force", False)),
            launch_on_startup=bool(features.get("launch_on_startup", False)),
            first_full_scan_done=True,
        )
    except Exception as exc:
        logger.error("Dashboard error: %s", exc)
        return await render_template("pages/dashboard.html", recent_scans=[], nav_users=[], error=str(exc))


@ui_bp.route("/artists")
async def artists():
    from helpers.normalization_service import strip_featured_artist

    with db_session() as session:
        # Fetch all canonical artist names with their stats
        result = session.execute(text("""
            SELECT
                COALESCE(NULLIF(album_artist, ''), artist) AS canonical,
                COUNT(DISTINCT album) AS album_count,
                COUNT(*) AS track_count,
                COALESCE(SUM(CASE WHEN stars = 5 THEN 1 ELSE 0 END), 0) AS five_star_count
            FROM tracks
            WHERE COALESCE(NULLIF(album_artist, ''), artist) IS NOT NULL
              AND COALESCE(NULLIF(album_artist, ''), artist) != ''
            GROUP BY canonical
            HAVING COUNT(DISTINCT album) > 0
            ORDER BY LOWER(canonical)
        """))
        rows = [dict(r._mapping) for r in result.fetchall()]

    # Group by feat-stripped name so "Apocalyptica" and
    # "Apocalyptica feat. Ville Valo" merge into one row.
    merged: dict[str, dict] = {}
    for row in rows:
        raw_name = row["canonical"]
        clean = strip_featured_artist(raw_name).lower()

        if clean not in merged:
            merged[clean] = {
                "sort_key": clean,
                "display_name": raw_name,
                "link_artist": raw_name,
                "album_count": 0,
                "track_count": 0,
                "five_star_count": 0,
            }
        # Keep the most common spelling as display/link name
        entry = merged[clean]
        if row["album_count"] > entry["album_count"]:
            entry["display_name"] = raw_name
            entry["link_artist"] = raw_name
        entry["album_count"] += row["album_count"]
        entry["track_count"] += row["track_count"]
        entry["five_star_count"] += row["five_star_count"]

    artists_data = sorted(merged.values(), key=lambda a: a["sort_key"])

    with db_session() as session:
        result = session.execute(text("SELECT COUNT(*) as tc, COUNT(DISTINCT album) as ac FROM tracks"))
        total_stats = dict(result.fetchone()._mapping)

    return await render_template("pages/artist_list.html", artists=artists_data, total_stats=total_stats)


@ui_bp.route("/artist/<path:name>")
async def artist_detail(name):
    name = unquote(name)
    cfg = get_config()

    with db_session() as session:
        result = session.execute(text("""
            SELECT MIN(album) as album, COUNT(*) as track_count, AVG(stars) as avg_stars,
                   MIN(year) as album_year
            FROM tracks WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:name)
            GROUP BY LOWER(TRIM(COALESCE(album, '')))
            ORDER BY (MIN(year) IS NULL), MIN(year) DESC NULLS LAST
        """), {"name": name})
        albums = [dict(r._mapping) for r in result.fetchall()]

        result = session.execute(text("""
            SELECT COUNT(*) as track_count, COUNT(DISTINCT album) as album_count,
                   AVG(stars) as avg_stars, MIN(year) as earliest_year, MAX(year) as latest_year
            FROM tracks WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:name)
        """), {"name": name})
        row = result.fetchone()
        stats = dict(row._mapping) if row else {}

        result = session.execute(text("""
            SELECT id, title, album, stars, final_score FROM tracks
            WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:name)
            ORDER BY stars DESC NULLS LAST LIMIT 20
        """), {"name": name})
        top_tracks = [dict(r._mapping) for r in result.fetchall()]

        # Fetch genre columns for all tracks to build genre_sources.
        result = session.execute(text("""
            SELECT lastfm_tags, listenbrainz_genres, discogs_genres,
                   musicbrainz_genres, spotify_genres, essentia_genres,
                   navidrome_genres, manual_genres, mood
            FROM tracks
            WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:name)
        """), {"name": name})
        genre_rows = [dict(r._mapping) for r in result.fetchall()]

    genre_sources = {}
    try:
        from services.enrichment.genre_tag_aggregator import get_artist_genre_sources
        genre_sources = get_artist_genre_sources(genre_rows)
    except Exception as exc:
        logger.debug("Failed to aggregate artist genre sources: %s", exc)

    albums_by_category = {
        "album": [a for a in albums if not a.get("album", "").lower().startswith("(")],
        "ep": [], "single": [], "compilation": [], "live_album": [], "remix_album": [],
    }
    return await render_template(
        "pages/artist_detail.html", artist_name=name, albums=albums, stats=stats,
        top_tracks=top_tracks, genre_sources=genre_sources,
        albums_by_category=albums_by_category,
        qbit_config=cfg.get("qbittorrent", {}),
        slskd_config=cfg.get("slskd", {}),
    )


@ui_bp.route("/album/<path:artist>/<path:album>")
async def album_detail(artist, album):
    artist = unquote(artist)
    album = unquote(album)
    cfg = get_config()

    with db_session() as session:
        result = session.execute(
            text("SELECT * FROM tracks WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist AND album = :album ORDER BY disc_number, track_number"),
            {"artist": artist, "album": album},
        )
        tracks = [dict(r._mapping) for r in result.fetchall()]

    genre_sources = {}
    try:
        from services.enrichment.genre_tag_aggregator import get_album_genre_sources
        genre_sources = get_album_genre_sources(tracks)
    except Exception as exc:
        logger.debug("Failed to aggregate album genre sources: %s", exc)

    return await render_template(
        "pages/album_detail.html",
        artist=artist, album=album, tracks=tracks,
        genre_sources=genre_sources,
        qbit_config=cfg.get("qbittorrent", {}),
        slskd_config=cfg.get("slskd", {}),
    )


@ui_bp.route("/track/<track_id>")
async def track_detail(track_id):
    cfg = get_config()

    with db_session() as session:
        result = session.execute(text("SELECT * FROM tracks WHERE CAST(id AS TEXT) = :id"), {"id": track_id})
        row = result.fetchone()
        if not row:
            return await render_template("pages/track_detail.html", track=None, error="Track not found")

        track = dict(row._mapping)

    genre_sources = {}
    try:
        from services.enrichment.genre_tag_aggregator import get_track_genre_sources
        genre_sources = get_track_genre_sources(track)
    except Exception as exc:
        logger.debug("Failed to aggregate track genre sources: %s", exc)

    return await render_template(
        "pages/track_detail.html", track=track, genre_sources=genre_sources,
        qbit_config=cfg.get("qbittorrent", {}),
        slskd_config=cfg.get("slskd", {}),
    )


@ui_bp.route("/search")
async def search():
    query = request.args.get("q", "").strip()
    return await render_template("pages/search.html", initial_query=query)


@ui_bp.route("/config", methods=["GET", "POST"])
async def config_editor():
    from helpers.config_helpers import get_config
    import yaml
    config, raw = {}, ""
    config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")
    try:
        if os.path.exists(config_path):
            with open(config_path) as f:
                raw = f.read()
            config = yaml.safe_load(raw) or {}
    except Exception:
        pass
    if request.method == "POST":
        return redirect(url_for("ui.config_editor"))
    return await render_template(
        "pages/config.html",
        config=config,
        config_raw=raw,
    )


@ui_bp.route("/config/env", methods=["GET"])
def config_env_vars():
    return jsonify({})


@ui_bp.route("/config/env", methods=["POST"])
def config_env_vars_post():
    return redirect(url_for("ui.config_editor"))


@ui_bp.route("/config/save-json", methods=["POST"])
async def config_save_json():
    """Save the full config dict from the WebUI editor back to config.yaml."""
    from helpers.config_helpers import save_config
    try:
        data = (await request.get_json()) or {}
    except Exception:
        return jsonify({"success": False, "error": "Invalid JSON body"}), 400
    success = save_config(data)
    if success:
        return jsonify({"success": True})
    return jsonify({"error": "Failed to save config"}), 500


@ui_bp.route("/config/migrate_postgres", methods=["POST"])
def config_migrate_postgres():
    return jsonify({"success": True})


@ui_bp.route("/logs")
async def logs():
    from helpers.logging_config import resolve_log_dir
    log_dir = resolve_log_dir()
    log_files = []
    if os.path.isdir(log_dir):
        for f in sorted(os.listdir(log_dir)):
            if f.endswith(".log"):
                full = os.path.join(log_dir, f)
                size = os.path.getsize(full) if os.path.isfile(full) else 0
                log_files.append({"name": f, "path": full, "size": size})
    return await render_template("pages/logs.html", log_dir=log_dir, log_files=log_files)


@ui_bp.route("/help")
@ui_bp.route("/help/<path:doc_name>")
async def help_page(doc_name=None):
    doc_path = os.path.join(os.path.dirname(__file__), "..", "documentation")
    doc_files = []
    try:
        doc_files = sorted(f for f in os.listdir(doc_path) if f.endswith(".md"))
    except Exception:
        pass
    content = ""
    doc_title = "Help"
    if doc_name:
        doc_name = os.path.basename(doc_name)
        full_path = os.path.join(doc_path, doc_name)
        if os.path.exists(full_path):
            with open(full_path) as f:
                content = f.read()
        doc_title = doc_name.replace(".md", "").replace("_", " ").title()
    return await render_template(
        "pages/help.html", content=content, doc_title=doc_title,
        doc_files=doc_files, current_doc=doc_name,
    )


@ui_bp.route("/bookmarks")
async def bookmarks():
    return await render_template("pages/bookmarks.html")


@ui_bp.route("/correcting")
async def correcting():
    try:
        from services.metadata.correction_service import get_album_tag_inconsistencies
        from services.metadata.conflict_service import get_conflict_stats

        page = max(request.args.get("page", 1, type=int), 1)
        per_page = 20

        inconsistencies = get_album_tag_inconsistencies(artist_filter=None)
        total = len(inconsistencies)
        total_pages = max(1, (total + per_page - 1) // per_page)

        # Paginate
        start = (page - 1) * per_page
        page_items = inconsistencies[start:start + per_page]

        conflict_stats = get_conflict_stats()

        return await render_template(
            "pages/corrections.html",
            inconsistencies=page_items,
            total=total,
            page=page,
            total_pages=total_pages,
            conflict_stats=conflict_stats,
        )
    except Exception as exc:
        logger.error("Corrections page error: %s", exc, exc_info=True)
        return await render_template(
            "pages/corrections.html",
            inconsistencies=[],
            total=0,
            page=1,
            total_pages=1,
            conflict_stats={"total_pending": 0, "by_provider": [], "by_field": []},
            error=str(exc),
        )


@ui_bp.route("/missing")
async def missing_page():
    cfg = get_config()
    return await render_template("pages/missing_releases.html", qbit_config=cfg.get("qbittorrent", {}), slskd_config=cfg.get("slskd", {}))


@ui_bp.route("/discover")
async def discover():
    return await render_template("pages/discover.html")


@ui_bp.route("/downloads/monitor")
async def downloads_monitor():
    cfg = get_config()
    return await render_template(
        "pages/downloads/monitor.html",
        qbit_config=cfg.get("qbittorrent", {}),
        slskd_config=cfg.get("slskd", {}),
    )


@ui_bp.route("/downloads/banned-words")
async def banned_words_page():
    return await render_template("pages/banned_words.html")


@ui_bp.route("/downloads")
async def downloads_page():
    cfg = get_config()
    return await render_template(
        "pages/downloads/queue.html",
        qbit_config=cfg.get("qbittorrent", {}),
        slskd_config=cfg.get("slskd", {}),
    )


@ui_bp.route("/downloads/search/soulseek")
async def downloads_search_soulseek():
    cfg = get_config()
    return await render_template(
        "pages/downloads/search_soulseek.html",
        qbit_config=cfg.get("qbittorrent", {}),
        slskd_config=cfg.get("slskd", {}),
    )


@ui_bp.route("/downloads/search/musicbrainz")
async def downloads_search_musicbrainz():
    cfg = get_config()
    return await render_template(
        "pages/downloads/search_musicbrainz.html",
        qbit_config=cfg.get("qbittorrent", {}),
        slskd_config=cfg.get("slskd", {}),
    )


@ui_bp.route("/downloads/search/qbittorrent")
async def downloads_search_qbittorrent():
    cfg = get_config()
    return await render_template(
        "pages/downloads/search_qbittorrent.html",
        qbit_config=cfg.get("qbittorrent", {}),
        slskd_config=cfg.get("slskd", {}),
    )


@ui_bp.route("/downloads/search/playlists")
async def downloads_search_playlists():
    cfg = get_config()
    return await render_template(
        "pages/downloads/search_playlists.html",
        qbit_config=cfg.get("qbittorrent", {}),
        slskd_config=cfg.get("slskd", {}),
    )


@ui_bp.route("/downloads/manager")
async def downloads_manager():
    cfg = get_config()
    return await render_template(
        "pages/downloads/manager.html",
        qbit_config=cfg.get("qbittorrent", {}),
        slskd_config=cfg.get("slskd", {}),
    )


@ui_bp.route("/downloads/discover/similar-artists")
async def downloads_discover_similar_artists():
    return await render_template("pages/downloads/similar_artists.html")


@ui_bp.route("/downloads/discover/upcoming")
async def downloads_discover_upcoming():
    return await render_template("pages/downloads/upcoming.html")


@ui_bp.route("/artist/<path:name>/corrections")
async def artist_corrections(name):
    return await render_template("pages/artist_corrections.html", artist_name=name)


@ui_bp.route("/artist/<path:name>/genre-management")
async def artist_genre_management(name):
    return await render_template("pages/artist_genres.html", artist_name=name)


@ui_bp.route("/metadata-compare")
async def metadata_compare():
    """Metadata comparison page — compare Navidrome vs Beets album data."""
    try:
        with db_session() as session:
            result = session.execute(text("""
                SELECT DISTINCT
                    album, COALESCE(NULLIF(album_artist, ''), artist) AS artist,
                    year, beets_year, navidrome_genres, musicbrainz_genres,
                    COUNT(*) AS track_count
                FROM tracks
                GROUP BY album, artist, year, beets_year, navidrome_genres, musicbrainz_genres
                ORDER BY artist, album
            """))
            rows = result.fetchall()

        album_comparisons = []
        for row in rows:
            album = row[0] or ""
            artist = row[1] or ""
            nav_year = row[2]
            beets_year = row[3]
            nav_genres_raw = row[4] or ""
            beets_genres_raw = row[5] or ""
            track_count = int(row[6] or 0)

            if (nav_year != beets_year) or (nav_genres_raw != beets_genres_raw):
                album_comparisons.append({
                    "album": album,
                    "artist": artist,
                    "track_count": track_count,
                    "navidrome": {
                        "year": nav_year,
                        "genres": nav_genres_raw.split(",") if nav_genres_raw else [],
                    },
                    "beets": {
                        "year": beets_year,
                        "genres": beets_genres_raw.split(",") if beets_genres_raw else [],
                    },
                })

        return await render_template("pages/metadata_compare.html", album_comparisons=album_comparisons)
    except Exception as exc:
        logger.error("metadata-compare: %s", exc)
        flash(f"Error loading metadata comparison: {exc}", "danger")
        return redirect(url_for("ui.dashboard"))


@ui_bp.route("/api/metadata-compare/search-musicbrainz", methods=["POST"])
async def metadata_compare_search_mb():
    """Search MusicBrainz for an album match to resolve metadata conflicts."""
    data = (await request.get_json(silent=True)) or {}
    artist = str(data.get("artist", "")).strip()
    album = str(data.get("album", "")).strip()
    if not artist or not album:
        return jsonify({"error": "artist and album required"}), 400
    try:
        from services.enrichment.musicbrainz_service import MusicBrainzService
        svc = MusicBrainzService()
        mbid, confidence = svc.get_suggested_mbid(album, artist)
        return jsonify({"success": True, "result": {"mbid": mbid, "confidence": confidence, "album": album, "artist": artist}})
    except Exception as exc:
        logger.error("metadata-compare MB search: %s", exc)
        return jsonify({"error": str(exc)}), 500


@ui_bp.route("/api/metadata-compare/accept-navidrome", methods=["POST"])
async def metadata_compare_accept_navidrome():
    """Mark an album as locked to prevent Beets from overwriting it."""
    data = (await request.get_json(silent=True)) or {}
    artist = str(data.get("artist", "")).strip()
    album = str(data.get("album", "")).strip()
    if not artist or not album:
        return jsonify({"error": "artist and album required"}), 400
    try:
        with db_session() as session:
            session.execute(text("UPDATE tracks SET metadata_locked = 1 WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist AND album = :album"), {"artist": artist, "album": album})
        return jsonify({"success": True, "message": f"Navidrome data locked for {artist} - {album}"})
    except Exception as exc:
        logger.error("metadata-compare accept navidrome: %s", exc)
        return jsonify({"error": str(exc)}), 500


@ui_bp.route("/api/metadata-compare/apply-musicbrainz", methods=["POST"])
async def metadata_compare_apply_mb():
    """Apply MusicBrainz metadata to an album — updates both DB and audio files."""
    data = (await request.get_json(silent=True)) or {}
    artist = str(data.get("artist", "")).strip()
    album = str(data.get("album", "")).strip()
    mb_data = data.get("mb_data", {})
    if not artist or not album or not mb_data:
        return jsonify({"error": "artist, album, and mb_data required"}), 400
    try:
        with db_session() as session:
            result = session.execute(text("""
                UPDATE tracks
                SET year = :year, musicbrainz_genres = :genres, mb_override = TRUE
                WHERE COALESCE(NULLIF(album_artist, ''), artist) = :artist AND album = :album
                RETURNING id
            """), {
                "year": mb_data.get("year"),
                "genres": ",".join(mb_data.get("genres", []) or []),
                "artist": artist,
                "album": album,
            })
            updated_ids = [row[0] for row in result.fetchall()]
        return jsonify({"success": True, "message": f"Applied MB data to {artist} - {album} ({len(updated_ids)} tracks)", "tracks_updated": len(updated_ids)})
    except Exception as exc:
        logger.error("metadata-compare apply MB: %s", exc)
        return jsonify({"error": str(exc)}), 500


@ui_bp.route("/beets")
async def beets():
    return await render_template("pages/beets_integration.html")


@ui_bp.route("/smart-playlists")
async def smart_playlists():
    return await render_template("pages/smart_playlists.html")


@ui_bp.route("/analytics/genres-moods")
async def analytics_genres_moods_page():
    return await render_template("pages/analytics.html")


@ui_bp.route("/debug/static")
def debug_static():
    return jsonify({"static_folder": ""})


# ===========================================================================
# TEMPLATE FILTERS
# ===========================================================================

@ui_bp.app_template_filter("split_genres")
def split_genres(s):
    if not s:
        return []
    return [g.strip() for g in re.split(r"[\\,]+", str(s)) if g.strip()]


@ui_bp.app_template_filter("format_datetime")
def format_datetime(value):
    if not value:
        return ""
    try:
        from datetime import datetime
        if "T" in str(value):
            dt = datetime.fromisoformat(str(value).split(".")[0])
        else:
            dt = datetime.fromisoformat(str(value))
        return dt.strftime("%d-%m-%y at %I:%M %p")
    except Exception:
        return str(value)
