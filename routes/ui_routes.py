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

def _similar_artist_display_list(session, entries: list) -> list[dict]:
    """Normalise cached similar-artist entries and flag ones already owned.

    Returns dicts with ``name``/``match``/``in_collection`` so the rendered
    list can split artists into "In Collection" and "Recommended" groups
    (Last.fm + ListenBrainz combined, deduped by name).
    """
    normalised: list[dict] = []
    names: set[str] = set()
    for entry in entries or []:
        if isinstance(entry, str):
            item = {"name": entry.strip(), "match": 0.0}
        elif isinstance(entry, dict):
            item = dict(entry)
            item["name"] = str(item.get("name") or "").strip()
            try:
                item["match"] = float(item.get("match") or 0.0)
            except Exception:
                item["match"] = 0.0
        else:
            continue
        if not item.get("name"):
            continue
        item["in_collection"] = False
        normalised.append(item)
        names.add(item["name"].lower())

    if not normalised:
        return []

    owned: set[str] = set()
    if names:
        name_list = list(names)
        placeholders = ", ".join([f":_n{i}" for i in range(len(name_list))])
        params = {f"_n{i}": n for i, n in enumerate(name_list)}
        rows = session.execute(
            text(f"""
                SELECT DISTINCT LOWER(COALESCE(NULLIF(album_artist, ''), artist))
                FROM tracks
                WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) IN ({placeholders})
            """),
            params,
        ).fetchall()
        owned = {str(r[0]) for r in rows if r[0]}

    for item in normalised:
        item["in_collection"] = item["name"].lower() in owned

    return normalised


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
        form = await request.form
        username = (form.get("username") or "").strip()
        password = (form.get("password") or "").strip()
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
                            await flash(f"Welcome back, {username}!", "success")
                            return redirect(url_for("ui.dashboard"))
                    except Exception:
                        pass
                # Fallback: check against stored password
                if user.get("pass") == password:
                    session["username"] = username
                    await flash(f"Welcome back, {username}!", "success")
                    return redirect(url_for("ui.dashboard"))

        await flash("Invalid credentials", "error")
        return await render_template("auth/login.html")
    return await render_template("auth/login.html")


@ui_bp.route("/logout")
async def logout():
    username = session.pop("username", None)
    await flash(f"Goodbye, {username}!", "info")
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
    dl = cfg.get("downloads", {})

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
        # File management (naming + conversion)
        "file_name_format": dl.get("file_name_format", ""),
        "conversion_enabled": bool(dl.get("conversion", {}).get("enabled", False)),
        "conversion_mode": dl.get("conversion", {}).get("mode", "flac_to_mp3"),
        "conversion_bitrate": dl.get("conversion", {}).get("mp3_bitrate_kbps", 320),
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
        logger.error("Dashboard error: %s", exc, exc_info=True)
        return await render_template("pages/dashboard.html", recent_scans=[], nav_users=[], error=str(exc))


@ui_bp.route("/artists")
async def artists():
    from helpers.normalization_service import strip_featured_artist

    with db_session() as session:
        result = session.execute(text("""
            SELECT
                COALESCE(NULLIF(album_artist, ''), artist) AS canonical,
                COUNT(DISTINCT album) AS album_count,
                COUNT(*) AS track_count,
                COALESCE(SUM(CASE WHEN stars = 5 THEN 1 ELSE 0 END), 0) AS five_star_count,
                MAX(last_scanned) AS last_updated
            FROM tracks
            WHERE COALESCE(NULLIF(album_artist, ''), artist) IS NOT NULL
              AND COALESCE(NULLIF(album_artist, ''), artist) != ''
            GROUP BY canonical
            HAVING COUNT(DISTINCT album) > 0
            ORDER BY LOWER(
                COALESCE(NULLIF(album_artist, ''), artist)
            )
        """))
        rows = [dict(r._mapping) for r in result.fetchall()]

    merged: dict[str, dict[str, Any]] = {}

    for row in rows:
        raw_name = row.get("canonical") or ""
        clean = strip_featured_artist(raw_name).strip().lower()

        if not clean:
            continue

        first_char = clean[0].upper()
        sort_letter = first_char if first_char.isalpha() else "#"

        if clean not in merged:
            merged[clean] = {
                "sort_key": clean,
                "sort_letter": sort_letter,
                "display_name": raw_name,
                "link_artist": raw_name,
                "album_count": 0,
                "track_count": 0,
                "five_star_count": 0,
                "last_updated": row.get("last_updated"),
            }

        entry = merged[clean]

        if row.get("album_count", 0) > entry.get("album_count", 0):
            entry["display_name"] = raw_name
            entry["link_artist"] = raw_name

        entry["album_count"] += int(row.get("album_count") or 0)
        entry["track_count"] += int(row.get("track_count") or 0)
        entry["five_star_count"] += int(row.get("five_star_count") or 0)

        row_updated = row.get("last_updated")
        entry_updated = entry.get("last_updated")

        if row_updated and (not entry_updated or row_updated > entry_updated):
            entry["last_updated"] = row_updated

    artists_data = sorted(
        merged.values(),
        key=lambda a: (
            0 if a["sort_letter"] == "#" else 1,
            a["sort_letter"],
            a["sort_key"],
        ),
    )

    with db_session() as session:
        result = session.execute(text("""
            SELECT
                COUNT(*) AS track_count,
                COUNT(DISTINCT album) AS album_count,
                COALESCE(
                    SUM(CASE WHEN stars = 5 THEN 1 ELSE 0 END),
                    0
                ) AS five_star_count
            FROM tracks
        """))
        row = result.fetchone()
        total_stats = dict(row._mapping) if row else {
            "track_count": 0,
            "album_count": 0,
            "five_star_count": 0,
        }

    return await render_template(
        "pages/artist_list.html",
        artists=artists_data,
        total_stats=total_stats,
    )


@ui_bp.route("/artist/<string:name>")
async def artist_detail(name: str):
    name = unquote(name or "").strip()
    cfg = get_config()

    def safe_float(value: Any) -> float | None:
        try:
            if value in (None, ""):
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    def safe_int(value: Any) -> int | None:
        try:
            if value in (None, ""):
                return None
            return int(value)
        except (TypeError, ValueError):
            return None

    def first_value(rows: list[dict[str, Any]], *keys: str) -> Any:
        for row in rows:
            for key in keys:
                value = row.get(key)
                if value not in (None, ""):
                    return value
        return None

    def split_tag_values(value: Any) -> list[str]:
        if not value:
            return []

        if isinstance(value, list):
            raw_items = value
        else:
            text = str(value)
            # Genre/tag columns store JSON arrays (e.g. '["rock","metal"]') as
            # well as plain delimited text.  Parse the JSON form first so the
            # artist "Top Genres" sections show clean names.
            if text.startswith("["):
                try:
                    import json as _json
                    parsed = _json.loads(text)
                    if isinstance(parsed, list):
                        raw_items = []
                        for item in parsed:
                            if isinstance(item, dict):
                                raw_items.append(str(item.get("name") or ""))
                            else:
                                raw_items.append(str(item))
                    else:
                        raw_items = re.split(r"[,;|\\]+", text)
                except Exception:
                    raw_items = re.split(r"[,;|\\]+", text)
            else:
                raw_items = re.split(r"[,;|\\]+", text)

        cleaned: list[str] = []
        seen: set[str] = set()

        for item in raw_items:
            tag = str(item).strip()

            if not tag:
                continue

            key = tag.lower()

            if key in seen:
                continue

            seen.add(key)
            cleaned.append(tag)

        return cleaned

    def collect_top_genres(
        rows: list[dict[str, Any]],
        limit: int = 30,
    ) -> list[str]:
        genre_fields = [
            "manual_genres",
            "navidrome_genres",
            "musicbrainz_genres",
            "spotify_genres",
            "discogs_genres",
            "lastfm_tags",
            "listenbrainz_genres",
            "essentia_genres",
            "mood",
        ]

        counts: dict[str, int] = {}
        display_names: dict[str, str] = {}

        for row in rows:
            for field in genre_fields:
                for genre in split_tag_values(row.get(field)):
                    key = genre.lower()

                    counts[key] = counts.get(key, 0) + 1
                    display_names.setdefault(key, genre)

        sorted_keys = sorted(
            counts.keys(),
            key=lambda k: (-counts[k], display_names[k].lower()),
        )

        return [display_names[k] for k in sorted_keys[:limit]]

    def classify_album(album_row: dict[str, Any]) -> str:
        import re as _re

        raw_type = str(
            album_row.get("musicbrainz_albumtype")
            or album_row.get("spotify_album_type")
            or album_row.get("album_type")
            or ""
        ).lower()

        album_name = str(
            album_row.get("album") or ""
        ).lower()

        # Compilation / soundtrack
        if "compilation" in raw_type:
            return "compilation"
        if "soundtrack" in raw_type or "soundtrack" in album_name:
            return "compilation"

        # Live / unplugged / acoustic
        if "live" in raw_type or _re.search(r'\blive\b', album_name) or "unplugged" in album_name:
            return "live_album"

        # Remix
        if "remix" in raw_type or "remix" in album_name:
            return "remix_album"

        # EP
        if raw_type == "ep" or raw_type.startswith("ep") or " ep" in raw_type:
            return "ep"

        # Single
        if "single" in raw_type:
            return "single"

        return "album"
    with db_session() as session:
        result = session.execute(
            text("""
                SELECT *
                FROM tracks
                WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:name)
                ORDER BY
                    LOWER(COALESCE(album, '')),
                    COALESCE(disc_number, '1'),
                    track_number,
                    title
            """),
            {"name": name},
        )
        tracks = [dict(r._mapping) for r in result.fetchall()]

        appears_result = session.execute(
            text("""
                SELECT *
                FROM tracks
                WHERE LOWER(COALESCE(artist, '')) = LOWER(:name)
                  AND LOWER(COALESCE(NULLIF(album_artist, ''), artist)) != LOWER(:name)
                ORDER BY
                    LOWER(COALESCE(album_artist, '')),
                    LOWER(COALESCE(album, '')),
                    COALESCE(disc_number, '1'),
                    track_number,
                    title
            """),
            {"name": name},
        )
        appears_tracks = [dict(r._mapping) for r in appears_result.fetchall()]

    albums_by_key: dict[str, dict[str, Any]] = {}

    for track in tracks:
        album_name = str(track.get("album") or "").strip()
        if not album_name:
            continue

        album_key = album_name.lower().strip()

        if album_key not in albums_by_key:
            albums_by_key[album_key] = {
                "album": album_name,
                "album_year": safe_int(track.get("year")),
                "track_count": 0,
                "avg_stars": None,
                "total_duration": 0,
                "last_updated": track.get("updated_at"),
                # musicbrainz_albumtype is the canonical album type column
                # (written by the scan); spotify_album_type is legacy.
                "spotify_album_type": track.get("musicbrainz_albumtype")
                    or track.get("spotify_album_type")
                    or track.get("album_type"),
                "album_type": track.get("musicbrainz_albumtype")
                    or track.get("album_type")
                    or track.get("spotify_album_type"),
                "is_missing": False,
            }

        album_entry = albums_by_key[album_key]
        album_entry["track_count"] += 1

        duration = safe_float(track.get("duration"))
        if duration:
            album_entry["total_duration"] += duration

        year = safe_int(track.get("year"))
        if year and not album_entry.get("album_year"):
            album_entry["album_year"] = year

        updated = track.get("updated_at")
        existing_updated = album_entry.get("last_updated")
        if updated and (not existing_updated or updated > existing_updated):
            album_entry["last_updated"] = updated

    for album_key, album_entry in albums_by_key.items():
        album_tracks = [
            track for track in tracks
            if str(track.get("album") or "").strip().lower() == album_key
        ]

        stars = [
            safe_float(track.get("stars"))
            for track in album_tracks
            if safe_float(track.get("stars")) is not None
        ]

        album_entry["avg_stars"] = (
            round(sum(stars) / len(stars), 2)
            if stars
            else None
        )

        # Attach tracks to the album entry so the macro can render them
        # Sort numerically by disc_number then track_number
        album_entry["tracks"] = sorted(
            album_tracks,
            key=lambda t: (
                safe_int(t.get("disc_number")) or 1,
                safe_int(t.get("track_number")) or 0,
                str(t.get("title") or "").lower(),
            ),
        )

    albums = sorted(
        albums_by_key.values(),
        key=lambda album: (
            album.get("album_year") is None,
            -(album.get("album_year") or 0),
            str(album.get("album") or "").lower(),
        ),
    )

    album_count = len(albums)
    track_count = len(tracks)

    star_values = [
        safe_float(track.get("stars"))
        for track in tracks
        if safe_float(track.get("stars")) is not None
    ]

    duration_values = [
        safe_float(track.get("duration"))
        for track in tracks
        if safe_float(track.get("duration")) is not None
    ]

    year_values = [
        safe_int(track.get("year"))
        for track in tracks
        if safe_int(track.get("year")) is not None
    ]

    five_star_count = sum(
        1 for track in tracks
        if safe_int(track.get("stars")) == 5
    )

    stats = {
        "track_count": track_count,
        "album_count": album_count,
        "avg_stars": round(sum(star_values) / len(star_values), 2)
            if star_values
            else None,
        "five_star_count": five_star_count,
        "total_duration": sum(duration_values) if duration_values else None,
        "earliest_year": min(year_values) if year_values else None,
        "latest_year": max(year_values) if year_values else None,
        "musicbrainz_artist_id": first_value(
            tracks,
            "musicbrainz_artist_id",
            "musicbrainz_artistid",
            "musicbrainz_albumartistid",
            "artist_mbid",
        ),
        "lastfm_artist_mbid": first_value(
            tracks,
            "lastfm_artist_mbid",
            "lastfm_mbid",
        ),
        "discogs_artist_id": first_value(
            tracks,
            "discogs_artist_id",
            "discogs_artistid",
        ),
    }

    top_tracks = sorted(
        tracks,
        key=lambda track: (
            safe_float(track.get("artist_z_score"))
            if safe_float(track.get("artist_z_score")) is not None
            else safe_float(track.get("final_score"))
            if safe_float(track.get("final_score")) is not None
            else safe_float(track.get("stars"))
            if safe_float(track.get("stars")) is not None
            else 0
        ),
        reverse=True,
    )[:20]

    for track in top_tracks:
        if track.get("artist_z_score") is None:
            track["artist_z_score"] = track.get("final_score") or 0

        if track.get("popularity_score") is None:
            track["popularity_score"] = track.get("final_score") or 0

        if track.get("is_single") is None:
            track["is_single"] = 0

        if track.get("file_path") is None:
            track["file_path"] = ""

    genre_rows = tracks

    genre_sources = {}
    try:
        from services.enrichment.genre_tag_aggregator import get_artist_genre_sources
        genre_sources = get_artist_genre_sources(genre_rows)
    except Exception as exc:
        logger.debug("Failed to aggregate artist genre sources: %s", exc)

    genres = collect_top_genres(tracks)

    # ── Load missing releases from DB ──────────────────────────────────────
    missing_entries: list[dict[str, Any]] = []
    try:
        with db_session() as session:
            missing_result = session.execute(
                text("""
                    SELECT title, release_id, primary_type, first_release_date,
                           cover_art_url, category
                    FROM missing_releases
                    WHERE LOWER(artist) = LOWER(:name)
                    ORDER BY first_release_date DESC NULLS LAST
                """),
                {"name": name},
            )
            for row in missing_result.fetchall():
                mr = dict(row._mapping)
                mr_title = str(mr.get("title") or "").strip()
                if not mr_title:
                    continue
                # Skip if we already have this album in the library
                album_key = mr_title.lower()
                if album_key in albums_by_key:
                    continue

                release_year = None
                first_release = str(mr.get("first_release_date") or "")
                if len(first_release) == 4 and first_release.isdigit():
                    release_year = safe_int(first_release)
                elif first_release and len(first_release) >= 4:
                    try:
                        release_year = safe_int(first_release[:4])
                    except Exception:
                        pass

                missing_entry = {
                    "album": mr_title,
                    "title": mr_title,
                    "album_year": release_year,
                    "track_count": 0,
                    "avg_stars": None,
                    "total_duration": 0,
                    "is_missing": True,
                    "first_release_date": first_release,
                    "cover_art_url": mr.get("cover_art_url") or "",
                    "release_id": mr.get("release_id") or "",
                }

                # Determine category based on MusicBrainz primary type
                primary_type = str(mr.get("primary_type") or mr.get("category") or "").lower()
                if primary_type in ("ep", "single", "compilation"):
                    missing_entry["_category"] = primary_type
                elif primary_type == "live":
                    missing_entry["_category"] = "live_album"
                elif primary_type in ("remix", "remix+compilation"):
                    missing_entry["_category"] = "remix_album"
                elif primary_type == "soundtrack":
                    missing_entry["_category"] = "compilation"
                else:
                    missing_entry["_category"] = "album"

                missing_entries.append(missing_entry)
    except Exception as exc:
        logger.debug("Failed to load missing releases for '%s': %s", name, exc)

    # ── Merge discovered + missing into a single sorted list ───────────────
    all_albums = albums + missing_entries
    all_albums.sort(
        key=lambda a: (
            a.get("album_year") is None,
            -(a.get("album_year") or 0),
            str(a.get("album") or a.get("title") or "").lower(),
        ),
    )

    # ── Categorize ─────────────────────────────────────────────────────────
    albums_by_category = {
        "album": [],
        "ep": [],
        "single": [],
        "compilation": [],
        "live_album": [],
        "remix_album": [],
    }

    for album_entry in all_albums:
        # Missing releases have a pre-computed category
        category = album_entry.get("_category") or classify_album(album_entry)
        albums_by_category.setdefault(category, []).append(album_entry)

    appears_by_key: dict[str, dict[str, Any]] = {}

    for track in appears_tracks:
        album_name = str(track.get("album") or "").strip()
        album_artist = str(
            track.get("album_artist")
            or track.get("artist")
            or ""
        ).strip()

        if not album_name:
            continue

        key = f"{album_artist.lower()}::{album_name.lower()}"

        if key not in appears_by_key:
            appears_by_key[key] = {
                "album": album_name,
                "album_artist": album_artist,
                "album_year": safe_int(track.get("year")),
                "track_count": 0,
                "avg_stars": None,
                "is_missing": False,
            }

        entry = appears_by_key[key]
        entry["track_count"] += 1

        year = safe_int(track.get("year"))
        if year and not entry.get("album_year"):
            entry["album_year"] = year

    for key, entry in appears_by_key.items():
        album_artist_key, album_key = key.split("::", 1)

        matching_tracks = [
            track for track in appears_tracks
            if str(track.get("album") or "").strip().lower() == album_key
            and str(track.get("album_artist") or track.get("artist") or "").strip().lower() == album_artist_key
        ]

        stars = [
            safe_float(track.get("stars"))
            for track in matching_tracks
            if safe_float(track.get("stars")) is not None
        ]

        entry["avg_stars"] = (
            round(sum(stars) / len(stars), 2)
            if stars
            else None
        )

    appears_on_albums = sorted(
        appears_by_key.values(),
        key=lambda item: (
            item.get("album_year") is None,
            -(item.get("album_year") or 0),
            str(item.get("album_artist") or "").lower(),
            str(item.get("album") or "").lower(),
        ),
    )

    artist_bio = first_value(
        tracks,
        "artist_bio",
        "bio",
        "lastfm_bio",
        "musicbrainz_bio",
    ) or ""

    # Fallback to artists table if tracks don't have bio data
    if not artist_bio:
        try:
            with db_session() as session:
                row = session.execute(
                    text("SELECT bio FROM artists WHERE name = :name AND bio IS NOT NULL AND bio != '' LIMIT 1"),
                    {"name": name},
                ).fetchone()
                if row:
                    artist_bio = str(row[0])
        except Exception:
            pass

    artist_country = first_value(
        tracks,
        "artist_country",
        "country",
        "origin_country",
        "musicbrainz_country",
    ) or ""

    # Fallback to artists table for country
    if not artist_country:
        try:
            with db_session() as session:
                row = session.execute(
                    text("SELECT country FROM artists WHERE name = :name AND country IS NOT NULL AND country != '' LIMIT 1"),
                    {"name": name},
                ).fetchone()
                if row:
                    artist_country = str(row[0])
        except Exception:
            pass

    artist_members_value = first_value(
        tracks,
        "artist_members",
        "musicbrainz_members",
        "members",
    )

    artist_members: list[Any] = []

    if isinstance(artist_members_value, list):
        artist_members = artist_members_value
    elif isinstance(artist_members_value, str) and artist_members_value.strip():
        try:
            import json
            parsed_members = json.loads(artist_members_value)
            if isinstance(parsed_members, list):
                artist_members = parsed_members
        except Exception:
            artist_members = []

    # Fallback: fetch members from MusicBrainz API via cache
    if not artist_members:
        try:
            from services.metadata.artist_metadata_service import get_artist_members_cached
            artist_members = get_artist_members_cached(name)
        except Exception as exc:
            logger.debug("Failed to fetch artist members for '%s': %s", name, exc)

    # ── Similar artists (pre-rendered to avoid spinner) ────────────────────
    # Last.fm + ListenBrainz are combined (deduped by name) and split into
    # "In Collection" and "Recommended" for display.
    similar_artists: dict[str, list[dict]] = {"lastfm": [], "listenbrainz": [], "display": []}
    try:
        with db_session() as session:
            row = session.execute(
                text("SELECT similar_artists_lastfm, similar_artists_listenbrainz FROM artists WHERE name = :name"),
                {"name": name},
            ).fetchone()
            if row:
                import json
                lf_raw = str(row[0] or "") if row[0] else ""
                lb_raw = str(row[1] or "") if row[1] else ""
                if lf_raw:
                    try:
                        similar_artists["lastfm"] = json.loads(lf_raw)
                    except Exception:
                        pass
                if lb_raw:
                    try:
                        similar_artists["listenbrainz"] = json.loads(lb_raw)
                    except Exception:
                        pass
            for source in ("lastfm", "listenbrainz"):
                similar_artists[source] = _similar_artist_display_list(
                    session, similar_artists[source]
                )
            # Merge both sources into a single deduped display list.
            merged: dict[str, dict] = {}
            for source in ("lastfm", "listenbrainz"):
                for item in similar_artists[source]:
                    key = item["name"].lower()
                    if key not in merged:
                        entry = dict(item)
                        entry["sources"] = []
                        merged[key] = entry
                    merged[key]["sources"].append(source)
                    if item.get("in_collection"):
                        merged[key]["in_collection"] = True
            similar_artists["display"] = list(merged.values())
    except Exception as exc:
        logger.debug("Failed to load similar artists for '%s': %s", name, exc)

    return await render_template(
        "pages/artist_detail.html",
        artist_name=name,
        albums=albums,
        stats=stats,
        top_tracks=top_tracks,
        genre_sources=genre_sources,
        genres=genres,
        albums_by_category=albums_by_category,
        appears_on_albums=appears_on_albums,
        artist_bio=artist_bio,
        artist_country=artist_country,
        artist_members=artist_members,
        similar_artists=similar_artists,
        qbit_config=cfg.get("qbittorrent", {}),
        slskd_config=cfg.get("slskd", {}),
    )

@ui_bp.route("/album/<path:artist>/<path:album>", methods=["GET", "POST"])
async def album_detail(artist: str, album: str):
    artist_name = unquote(artist or "").strip()
    album_name = unquote(album or "").strip()
    cfg = get_config()

    # Fetch tracks early — both GET and POST need them
    with db_session() as session:
        result = session.execute(
            text("""
                SELECT *
                FROM tracks
                WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)
                  AND LOWER(COALESCE(album, '')) = LOWER(:album)
                ORDER BY
                    COALESCE(disc_number, '1'),
                    NULLIF(regexp_replace(COALESCE(track_number::text, ''), '[^0-9].*$', ''), '')::int NULLS LAST,
                    track_number,
                    title
            """),
            {"artist": artist_name, "album": album_name},
        )
        tracks = [dict(r._mapping) for r in result.fetchall()]

    if request.method == "POST":
        form = await request.form
        from db.repositories.tracks import insert_or_update_track
        from services.metadata.tag_file_service import update_file_tags

        new_title = (form.get("album_title") or "").strip()
        new_artist = (form.get("album_artist") or "").strip()
        release_year = (form.get("release_year") or "").strip()
        album_type = (form.get("album_type") or "").strip()
        track_artist = (form.get("track_artist") or "").strip()
        track_composer = (form.get("track_composer") or "").strip()
        track_comment = (form.get("track_comment") or "").strip()
        album_mbid = (form.get("album_mbid") or "").strip()
        album_rg_mbid = (form.get("album_release_group_mbid") or "").strip()
        discogs_id = (form.get("album_discogs_id") or "").strip()
        artist_mbid = (form.get("artist_mbid") or "").strip()
        genres_str = (form.get("album_genres") or "").strip()
        cover_url = (form.get("cover_art_url") or "").strip()

        updated_count = 0
        reverted_live_count = 0

        for track in tracks:
            track_id = track.get("id")
            if not track_id:
                continue

            payload: dict[str, Any] = {"id": track_id}

            # Album title / artist
            if new_title and new_title != track.get("album"):
                payload["album"] = new_title

            if new_artist and new_artist != (track.get("album_artist") or track.get("artist")):
                payload["album_artist"] = new_artist
                payload["artist"] = new_artist

            # Year — write to all year-related fields for display consistency
            if release_year:
                payload["year"] = release_year
                try:
                    payload["release_year"] = int(release_year)
                except ValueError:
                    pass

            # Album type
            if album_type:
                payload["spotify_album_type"] = album_type
                payload["musicbrainz_albumtype"] = album_type

            # Track-level overrides
            if track_artist and track_artist != track.get("artist"):
                payload["artist"] = track_artist
            if track_composer:
                # The tracks table has no "composer" column — writer is the
                # composer/lyricist field.
                payload["writer"] = track_composer

            # MBIDs
            if album_mbid:
                payload["musicbrainz_album_mbid"] = album_mbid
                payload["musicbrainz_albumid"] = album_mbid
            if album_rg_mbid:
                payload["musicbrainz_releasegroupid"] = album_rg_mbid
            if artist_mbid:
                payload["musicbrainz_artistid"] = artist_mbid

            # Cover art URL
            if cover_url:
                payload["cover_art_url"] = cover_url

            # Genres — write to both DB and audio file
            if genres_str:
                genres_list = [g.strip() for g in genres_str.split(",") if g.strip()]
                if genres_list:
                    from db.repositories.metadata import update_track_genres
                    update_track_genres(track_id=track_id, genres_str=genres_str)
                    # Write to audio file
                    file_path = track.get("file_path")
                    if file_path and os.path.exists(file_path):
                        try:
                            update_file_tags(file_path, {"genres": genres_list})
                        except Exception as tag_err:
                            logger.debug("[album_detail] Tag write failed for %s: %s", track_id, tag_err)

            if len(payload) > 1:  # more than just id
                try:
                    insert_or_update_track(track_id, payload)
                    updated_count += 1
                except Exception as db_err:
                    logger.debug("[album_detail] DB update failed for %s: %s", track_id, db_err)

            # Album type corrected away from live: undo the live/acoustic
            # tagging (suffix, flags, injected genre) the album stage may
            # have applied — wrongly-detected live albums keep "(Live)"
            # titles otherwise. Only an explicit non-live type reverts;
            # "Unknown" (empty) leaves the state alone.
            if album_type and "+live" not in album_type.lower() and "(live)" not in album_type.lower():
                try:
                    from services.popularity.stages.album_stage import revert_track_live_state
                    if revert_track_live_state(str(track_id)):
                        reverted_live_count += 1
                except Exception as revert_err:
                    logger.debug(
                        "[album_detail] Live-state revert failed for %s: %s",
                        track_id, revert_err,
                    )

        if updated_count > 0:
            await flash(f"Album metadata saved — {updated_count} track(s) updated.", "success")
        if reverted_live_count > 0:
            await flash(
                f"Removed \"(Live)\"/\"(Acoustic)\" suffixes from {reverted_live_count} track(s).",
                "info",
            )
        if updated_count == 0 and reverted_live_count == 0:
            await flash("No changes were made.", "info")

        # Redirect to the new album name if changed
        redirect_artist = new_artist or artist_name
        redirect_album = new_title or album_name
        return redirect(url_for("ui.album_detail", artist=redirect_artist, album=redirect_album))

    first_track = tracks[0] if tracks else {}

    def first_value(*keys: str) -> Any:
        for row in tracks:
            for key in keys:
                value = row.get(key)

                if value not in (None, ""):
                    return value

        return None

    def split_tag_values(value: Any) -> list[str]:
        if not value:
            return []

        if isinstance(value, list):
            raw_items = value
        else:
            text = str(value)
            if text.startswith("["):
                try:
                    import json as _json
                    parsed = _json.loads(text)
                    if isinstance(parsed, list):
                        raw_items = []
                        for item in parsed:
                            if isinstance(item, dict):
                                raw_items.append(str(item.get("name") or ""))
                            else:
                                raw_items.append(str(item))
                    else:
                        raw_items = re.split(r"[,;|\\]+", text)
                except Exception:
                    raw_items = re.split(r"[,;|\\]+", text)
            else:
                raw_items = re.split(r"[,;|\\]+", text)

        cleaned: list[str] = []
        seen: set[str] = set()

        for item in raw_items:
            tag = str(item).strip()

            if not tag:
                continue

            key = tag.lower()

            if key in seen:
                continue

            seen.add(key)
            cleaned.append(tag)

        return cleaned

    def collect_album_genres() -> list[str]:
        genre_fields = [
            "manual_genres",
            "navidrome_genres",
            "musicbrainz_genres",
            "spotify_genres",
            "discogs_genres",
            "lastfm_tags",
            "listenbrainz_genres",
            "essentia_genres",
            "mood",
        ]

        genres: list[str] = []
        seen: set[str] = set()

        for row in tracks:
            for field in genre_fields:
                for genre in split_tag_values(row.get(field)):
                    key = genre.lower()

                    if key in seen:
                        continue

                    seen.add(key)
                    genres.append(genre)

        return genres

    def safe_float(value: Any) -> float | None:
        try:
            if value in (None, ""):
                return None

            return float(value)

        except (TypeError, ValueError):
            return None

    def safe_int(value: Any) -> int | None:
        try:
            if value in (None, ""):
                return None

            return int(value)

        except (TypeError, ValueError):
            return None
            
    durations = [
        safe_float(track.get("duration"))
        for track in tracks
        if safe_float(track.get("duration")) is not None
    ]

    star_values = [
        safe_float(track.get("stars"))
        for track in tracks
        if safe_float(track.get("stars")) is not None
    ]

    disc_values = [
        safe_int(track.get("disc_number"))
        for track in tracks
        if safe_int(track.get("disc_number")) is not None
    ]

    singles_count = sum(
        1
        for track in tracks
        if safe_int(track.get("is_single")) == 1
        and str(track.get("single_confidence") or "").strip().lower() == "high"
    )

    album_data = {
        **first_track,
        "track_count": len(tracks),
        "avg_stars": round(sum(star_values) / len(star_values), 2) if star_values else None,
        "total_duration": sum(durations) if durations else None,
        "total_discs": max(disc_values) if disc_values else 1,
        "singles_count": singles_count,
        "spotify_release_date": first_value("spotify_release_date", "release_date", "date"),
        "spotify_album_type": first_value("musicbrainz_albumtype", "spotify_album_type", "album_type"),
        "record_label": first_value("record_label", "label"),
        "catalog_number": first_value("catalog_number", "catalog"),
        "last_scanned": first_value("last_scanned", "updated_at", "created_at"),
        "musicbrainz_album_mbid": first_value("musicbrainz_album_mbid", "musicbrainz_releaseid", "musicbrainz_albumid"),
        "musicbrainz_releasegroupid": first_value("musicbrainz_releasegroupid", "musicbrainz_release_group_id"),
        "discogs_album_id": first_value("discogs_album_id", "discogs_release_id"),
    }

    # Derive album year from track years as fallback
    year_values = [
        safe_int(track.get("year"))
        for track in tracks
        if safe_int(track.get("year")) is not None
    ]
    album_data["album_year"] = (
        max(year_values) if year_values else None
    )

    # Check if album is bookmarked/favourited
    is_album_favourite = False
    try:
        with db_session() as session:
            row = session.execute(
                text("""
                    SELECT 1 FROM bookmarks
                    WHERE bookmark_type = 'album'
                      AND LOWER(artist_name) = LOWER(:artist)
                      AND LOWER(album_name) = LOWER(:album)
                    LIMIT 1
                """),
                {"artist": artist_name, "album": album_name},
            ).fetchone()
            if row:
                is_album_favourite = True
    except Exception as exc:
        logger.debug("Failed to check album bookmark for '%s' - '%s': %s", artist_name, album_name, exc)

    tracks_by_disc: dict[int, list[dict[str, Any]]] = {}
    for track in tracks:
        disc_number = safe_int(track.get("disc_number")) or 1
        tracks_by_disc.setdefault(disc_number, []).append(track)

    album_genres = collect_album_genres()

    genre_sources = {}
    try:
        from services.enrichment.genre_tag_aggregator import get_album_genre_sources
        genre_sources = get_album_genre_sources(tracks)
    except Exception as exc:
        logger.debug("Failed to aggregate album genre sources: %s", exc)

    album_artist_mbid = first_value(
        "artist_mbid",
        "musicbrainz_artistid",
        "musicbrainz_albumartistid",
    ) or ""

    return await render_template(
        "pages/album_detail.html",

        # Names expected by album_detail.html
        artist_name=artist_name,
        album_name=album_name,

        # Backwards-compatible names, in case older template/js pieces still use them
        artist=artist_name,
        album=album_name,

        # Album page data
        album_data=album_data,
        tracks=tracks,
        tracks_by_disc=tracks_by_disc,
        album_genres=album_genres,
        genre_sources=genre_sources,
        album_artist_mbid=album_artist_mbid,

        is_album_favourite=is_album_favourite,

        # Download provider config
        qbit_config=cfg.get("qbittorrent", {}),
        slskd_config=cfg.get("slskd", {}),
    )

@ui_bp.route("/track/<track_id>", methods=["GET", "POST"])
async def track_detail(track_id: str):
    """View and edit track details."""
    cfg = get_config()

    def split_tag_values(value: Any) -> list[str]:
        if value in (None, ""):
            return []

        try:
            import json

            if isinstance(value, str) and value.strip().startswith("["):
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    raw_items = parsed
                else:
                    raw_items = [value]
            elif isinstance(value, list):
                raw_items = value
            else:
                raw_items = re.split(r"[,;|\\]+", str(value))
        except Exception:
            raw_items = re.split(r"[,;|\\]+", str(value))

        cleaned: list[str] = []
        seen: set[str] = set()

        for item in raw_items:
            text = str(item).strip()
            if not text:
                continue

            key = text.lower()
            if key in seen:
                continue

            seen.add(key)
            cleaned.append(text)

        return cleaned

    def stringify_tag_field(value: Any) -> str:
        return ", ".join(split_tag_values(value))

    def get_track_column_types(db) -> dict[str, str]:
        try:
            result = db.execute(text("""
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_name = 'tracks'
            """))
            return {
                str(row._mapping["column_name"]): str(row._mapping["data_type"]).lower()
                for row in result.fetchall()
            }
        except Exception:
            return {}

    def quote_identifier(column_name: str) -> str:
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", column_name):
            raise ValueError(f"Unsafe column name: {column_name}")
        return f'"{column_name}"'

    def parse_optional_int(value: Any, allow_prefix: bool = False) -> int | None:
        text_value = str(value or "").strip()
        if not text_value:
            return None

        if allow_prefix and "/" in text_value:
            text_value = text_value.split("/", 1)[0].strip()

        try:
            return int(text_value)
        except ValueError:
            return None

    def parse_optional_float(value: Any) -> float | None:
        text_value = str(value or "").strip()
        if not text_value:
            return None

        try:
            return float(text_value)
        except ValueError:
            return None

    def parse_bool(value: Any) -> bool:
        return str(value or "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
            "checked",
        }

    def normalize_for_column(
        column_name: str,
        value: Any,
        column_types: dict[str, str],
    ) -> Any:
        column_type = column_types.get(column_name, "")

        if column_type in {
            "integer",
            "bigint",
            "smallint",
        }:
            return parse_optional_int(
                value,
                allow_prefix=(column_name == "track_number"),
            )

        if column_type in {
            "numeric",
            "double precision",
            "real",
            "decimal",
        }:
            return parse_optional_float(value)

        if column_type == "boolean":
            return parse_bool(value)

        text_value = str(value or "").strip()
        return text_value if text_value else None

    def normalize_flag_for_column(
        column_name: str,
        value: bool,
        column_types: dict[str, str],
    ) -> Any:
        column_type = column_types.get(column_name, "")

        if column_type == "boolean":
            return bool(value)

        return 1 if value else 0

    def apply_track_template_aliases(track: dict[str, Any]) -> dict[str, Any]:
        # Display-friendly genre fields.
        for field in [
            "navidrome_genres",
            "lastfm_tags",
            "discogs_genres",
            "musicbrainz_genres",
            "essentia_genres",
            "listenbrainz_genres",
        ]:
            if track.get(field):
                track[field] = stringify_tag_field(track.get(field))

        # Main editable genre field.
        if not track.get("genres"):
            track["genres"] = (
                track.get("manual_genres")
                or track.get("top_genres")
                or track.get("navidrome_genres")
                or ""
            )

        # Mood list for badge display.
        track["mood_list"] = split_tag_values(track.get("mood"))

        # Writer sometimes arrives as JSON.
        if track.get("writer"):
            track["writer"] = stringify_tag_field(track.get("writer"))

        # Single-source list for template checks — parse the stored JSON array
        # of source dicts and keep the `source` keys of matched entries, so
        # the track page's detection-source table reflects reality.
        _sources_raw = (
            track.get("single_sources")
            or track.get("single_detection_sources")
            or ""
        )
        _source_keys: list[str] = []
        try:
            import json as _json
            _parsed = _json.loads(_sources_raw) if isinstance(_sources_raw, str) and _sources_raw.strip() else _sources_raw
            if isinstance(_parsed, list):
                for _entry in _parsed:
                    if isinstance(_entry, dict):
                        if _entry.get("matched"):
                            _k = str(_entry.get("source") or "").strip()
                            if _k:
                                _source_keys.append(_k)
                    elif isinstance(_entry, str) and _entry.strip():
                        _source_keys.append(_entry.strip())
        except Exception:
            # Fallback: legacy comma/backslash-separated fragments
            _source_keys = split_tag_values(_sources_raw)
        track["single_sources_list"] = _source_keys

        # MusicBrainz compatibility aliases.
        if not track.get("musicbrainz_albumid"):
            track["musicbrainz_albumid"] = (
                track.get("musicbrainz_album_mbid")
                or track.get("musicbrainz_releaseid")
                or ""
            )

        if not track.get("musicbrainz_album_mbid"):
            track["musicbrainz_album_mbid"] = (
                track.get("musicbrainz_albumid")
                or track.get("musicbrainz_releaseid")
                or ""
            )

        if not track.get("musicbrainz_trackid"):
            track["musicbrainz_trackid"] = (
                track.get("mbid")
                or track.get("beets_mbid")
                or ""
            )

        if not track.get("mbid"):
            track["mbid"] = (
                track.get("musicbrainz_trackid")
                or track.get("beets_mbid")
                or ""
            )

        if not track.get("beets_mbid"):
            track["beets_mbid"] = (
                track.get("mbid")
                or track.get("musicbrainz_trackid")
                or ""
            )

        if not track.get("musicbrainz_artist_id"):
            track["musicbrainz_artist_id"] = (
                track.get("musicbrainz_artistid")
                or track.get("artist_mbid")
                or ""
            )

        if not track.get("musicbrainz_artistid"):
            track["musicbrainz_artistid"] = (
                track.get("musicbrainz_artist_id")
                or track.get("artist_mbid")
                or ""
            )

        if not track.get("album_artist"):
            track["album_artist"] = (
                track.get("albumartist")
                or track.get("artist")
                or ""
            )

        if track.get("artist_z_score") is None:
            track["artist_z_score"] = (
                track.get("final_score")
                or track.get("popularity_score")
            )

        if track.get("popularity_score") is None:
            track["popularity_score"] = (
                track.get("final_score")
                or track.get("artist_z_score")
            )

        if track.get("is_single") is None:
            track["is_single"] = 0

        if not track.get("single_confidence"):
            track["single_confidence"] = "low"

        if track.get("file_path") is None:
            track["file_path"] = ""

        return track

    def resolve_music_file_path(path_value: Any) -> str | None:
        if not path_value:
            return None

        raw_path = str(path_value).strip()
        if not raw_path:
            return None

        candidates = [raw_path]

        if not os.path.isabs(raw_path):
            for root in [
                os.environ.get("MUSIC_FOLDER"),
                os.environ.get("MUSIC_ROOT"),
                os.environ.get("MUSIC_DIR"),
                "/music",
            ]:
                if root:
                    candidates.append(os.path.join(str(root).strip(), raw_path))

        seen: set[str] = set()

        for candidate in candidates:
            if candidate in seen:
                continue

            seen.add(candidate)

            if os.path.exists(candidate):
                return candidate

        return None

    def build_tags_to_write(update_values: dict[str, Any]) -> dict[str, Any]:
        tag_map = {
            "title": "title",
            "artist": "artist",
            "album": "album",
            "album_artist": "album_artist",
            "genres": "genre",
            "year": "year",
            "composer": "composer",
            "writer": "writer",
            "arranger": "arranger",
            "mixer": "mixer",
            "producer": "producer",
            "work": "work",
            "track_number": "track_number",
            "disc_number": "disc_number",
            "comment": "comment",
            "mbid": "mbid",
            "isrc": "isrc",
            "bpm": "bpm",
            "titlesort": "titlesort",
            "albumsort": "albumsort",
            "artistsort": "artistsort",
            "composersort": "composersort",
            "albumartistsort": "albumartistsort",
            "lyricistsort": "lyricistsort",
            "artistssort": "artistssort",
            "albumartistssort": "albumartistssort",
            "artists": "artists",
            "albumartists": "albumartists",
            "conductor": "conductor",
            "performer": "performer",
            "director": "director",
            "djmixer": "djmixer",
            "engineer": "engineer",
            "remixer": "remixer",
            "lyricist": "lyricist",
            "albumversion": "albumversion",
            "recordlabel": "recordlabel",
            "copyright": "copyright",
            "releasedate": "releasedate",
            "releasetype": "releasetype",
            "releasestatus": "releasestatus",
            "releasecountry": "releasecountry",
            "media": "media",
            "barcode": "barcode",
            "catalognumber": "catalognumber",
            "asin": "asin",
            "originalyear": "originalyear",
            "originaldate": "originaldate",
            "tracktotal": "tracktotal",
            "disctotal": "disctotal",
            "script": "script",
            "discsubtitle": "discsubtitle",
            "lyrics": "lyrics",
            "subtitle": "subtitle",
            "grouping": "grouping",
            "movement": "movement",
            "movementname": "movementname",
            "movementtotal": "movementtotal",
            "key": "key",
            "language": "language",
            "license": "license",
            "website": "website",
            "encodedby": "encodedby",
            "encodersettings": "encodersettings",
            "explicitstatus": "explicitstatus",
            "musicbrainz_albumid": "musicbrainz_albumid",
            "musicbrainz_artistid": "musicbrainz_artistid",
            "musicbrainz_albumartistid": "musicbrainz_albumartistid",
            "musicbrainz_releasegroupid": "musicbrainz_releasegroupid",
            "musicbrainz_releasetrackid": "musicbrainz_releasetrackid",
            "musicbrainz_workid": "musicbrainz_workid",
            "musicbrainz_trackid": "musicbrainz_trackid",
            "replaygain_track_gain": "replaygain_track_gain",
            "replaygain_track_peak": "replaygain_track_peak",
            "replaygain_album_gain": "replaygain_album_gain",
            "replaygain_album_peak": "replaygain_album_peak",
            "r128_track_gain": "r128_track_gain",
            "r128_album_gain": "r128_album_gain",
        }

        tags: dict[str, Any] = {}

        for column_name, tag_name in tag_map.items():
            value = update_values.get(column_name)

            if value not in (None, ""):
                tags[tag_name] = value

        return tags

    try:
        with db_session() as db:
            column_types = get_track_column_types(db)

            result = db.execute(
                text("""
                    SELECT *
                    FROM tracks
                    WHERE CAST(id AS TEXT) = :id
                    LIMIT 1
                """),
                {"id": str(track_id)},
            )
            row = result.fetchone()

            if not row:
                await flash("Track not found", "error")
                return redirect(url_for("ui.dashboard"))

            raw_db_columns = set(row._mapping.keys())
            track = apply_track_template_aliases(dict(row._mapping))
            # Use the original DB column names (not aliased) for UPDATE checks
            existing_columns = raw_db_columns

            if request.method == "POST":
                form = await request.form

                direct_fields = [
                    "title",
                    "artist",
                    "album",
                    "album_artist",
                    "stars",
                    "is_single",
                    "single_confidence",
                    "mbid",
                    "suggested_mbid",
                    "suggested_mbid_confidence",
                    "genres",
                    "year",
                    "composer",
                    "writer",
                    "arranger",
                    "mixer",
                    "producer",
                    "work",
                    "track_number",
                    "disc_number",
                    "comment",
                    "isrc",
                    "bpm",
                    "bitrate",
                    "sample_rate",
                    "titlesort",
                    "albumsort",
                    "artistsort",
                    "composersort",
                    "albumartistsort",
                    "lyricistsort",
                    "artistssort",
                    "albumartistssort",
                    "artists",
                    "albumartists",
                    "conductor",
                    "performer",
                    "director",
                    "djmixer",
                    "engineer",
                    "remixer",
                    "lyricist",
                    "albumversion",
                    "recordlabel",
                    "copyright",
                    "releasedate",
                    "releasetype",
                    "releasestatus",
                    "releasecountry",
                    "media",
                    "barcode",
                    "catalognumber",
                    "asin",
                    "originalyear",
                    "originaldate",
                    "tracktotal",
                    "disctotal",
                    "script",
                    "discsubtitle",
                    "lyrics",
                    "subtitle",
                    "grouping",
                    "movement",
                    "movementname",
                    "movementtotal",
                    "key",
                    "language",
                    "license",
                    "website",
                    "encodedby",
                    "encodersettings",
                    "explicitstatus",
                    "musicbrainz_albumid",
                    "musicbrainz_artistid",
                    "musicbrainz_albumartistid",
                    "musicbrainz_releasegroupid",
                    "musicbrainz_releasetrackid",
                    "musicbrainz_workid",
                    "musicbrainz_trackid",
                    "replaygain_track_gain",
                    "replaygain_track_peak",
                    "replaygain_album_gain",
                    "replaygain_album_peak",
                    "r128_track_gain",
                    "r128_album_gain",
                ]

                flag_fields = [
                    "is_cover",
                    "cover_manual_override",
                    "alternate_take",
                    "is_compilation",
                    "is_live",
                    "is_acoustic",
                    "is_remix",
                    "single_manual_override",
                ]

                update_values: dict[str, Any] = {}

                for field_name in direct_fields:
                    if field_name not in existing_columns:
                        continue

                    if field_name not in form:
                        continue

                    update_values[field_name] = normalize_for_column(
                        field_name,
                        form.get(field_name),
                        column_types,
                    )

                # Keep track ID aliases in sync where those columns exist.
                mbid_value = form.get("mbid", "").strip() if "mbid" in form else None
                if mbid_value:
                    for column_name in [
                        "mbid",
                        "beets_mbid",
                        "musicbrainz_trackid",
                    ]:
                        if column_name in existing_columns:
                            update_values[column_name] = mbid_value

                # Keep MusicBrainz album aliases in sync where those columns exist.
                mb_album_value = (
                    form.get("musicbrainz_albumid", "").strip()
                    if "musicbrainz_albumid" in form
                    else None
                )
                if mb_album_value:
                    for column_name in [
                        "musicbrainz_albumid",
                        "musicbrainz_album_mbid",
                        "musicbrainz_releaseid",
                    ]:
                        if column_name in existing_columns:
                            update_values[column_name] = mb_album_value

                # Keep genre aliases in sync where those columns exist.
                genres_value = form.get("genres", "").strip() if "genres" in form else None
                if genres_value is not None:
                    for column_name in [
                        "genres",
                        "manual_genres",
                    ]:
                        if column_name in existing_columns:
                            update_values[column_name] = genres_value or None

                for field_name in flag_fields:
                    if field_name not in existing_columns:
                        continue

                    update_values[field_name] = normalize_flag_for_column(
                        field_name,
                        parse_bool(form.get(field_name)),
                        column_types,
                    )

                # Compatibility with old behaviour: manually editing is_single marks the override.
                if "single_manual_override" in existing_columns:
                    update_values["single_manual_override"] = normalize_flag_for_column(
                        "single_manual_override",
                        True,
                        column_types,
                    )

                if update_values:
                    params: dict[str, Any] = {
                        "id": str(track_id),
                    }
                    set_clauses: list[str] = []

                    for index, (column_name, value) in enumerate(update_values.items()):
                        param_name = f"value_{index}"
                        set_clauses.append(f"{quote_identifier(column_name)} = :{param_name}")
                        params[param_name] = value

                    if "updated_at" in existing_columns:
                        set_clauses.append("updated_at = CURRENT_TIMESTAMP")

                    db.execute(
                        text(f"""
                            UPDATE tracks
                            SET {", ".join(set_clauses)}
                            WHERE CAST(id AS TEXT) = :id
                        """),
                        params,
                    )

                    # Album-scoped fields describe the release as a whole:
                    # push the same values to every other track on the album
                    # so a per-track edit can never split one release into
                    # two albums.
                    album_scoped_fields = {
                        "album", "album_artist", "year",
                        "musicbrainz_albumid", "musicbrainz_album_mbid",
                        "musicbrainz_releasegroupid", "musicbrainz_albumartistid",
                    }
                    album_updates = {
                        k: v for k, v in update_values.items()
                        if k in album_scoped_fields
                    }
                    if album_updates and update_values.get("album"):
                        try:
                            album_set_clause = ", ".join(
                                f"{quote_identifier(k)} = :{k}" for k in album_updates
                            )
                            album_params = {
                                **album_updates,
                                "id": str(track_id),
                                "old_album": track.get("album"),
                                "old_album_artist": (
                                    track.get("album_artist")
                                    or track.get("albumartist")
                                    or track.get("artist")
                                    or ""
                                ),
                            }
                            album_result = db.execute(
                                text(f"""
                                    UPDATE tracks
                                    SET {album_set_clause}
                                    WHERE CAST(id AS TEXT) <> :id
                                      AND album = :old_album
                                      AND COALESCE(NULLIF(album_artist, ''), artist) = :old_album_artist
                                """),
                                album_params,
                            )
                            album_tracks_updated = album_result.rowcount or 0
                        except Exception as album_err:
                            logger.debug(
                                "Album-scoped propagation failed for %s: %s",
                                track_id,
                                album_err,
                            )
                            album_tracks_updated = 0
                        if album_tracks_updated:
                            await flash(
                                f"Album metadata also applied to {album_tracks_updated} other track(s) on this album.",
                                "info",
                            )
                    db.commit()

                    # Legacy parity undo: un-marking a track as live/acoustic
                    # must strip the "(Live)"/"(Acoustic)" suffix the album
                    # stage appended (wrongly-detected live tracks keep the
                    # suffix otherwise).
                    if any(f in update_values for f in ("is_live", "is_acoustic")) \
                            and not (update_values.get("is_live") or update_values.get("is_acoustic")):
                        try:
                            from services.popularity.stages.album_stage import revert_track_live_state
                            if revert_track_live_state(str(track_id)):
                                await flash(
                                    "Removed \"(Live)\"/\"(Acoustic)\" suffix from the track title.",
                                    "info",
                                )
                        except Exception as revert_err:
                            logger.debug(
                                "Live-state revert failed for %s: %s",
                                track_id, revert_err,
                            )

                    file_path = track.get("file_path")
                    resolved_path = resolve_music_file_path(file_path)

                    if resolved_path:
                        try:
                            from services.metadata.tag_file_service import write_tags_to_file

                            tags_to_write = build_tags_to_write(update_values)

                            if tags_to_write:
                                file_write_success = write_tags_to_file(
                                    resolved_path,
                                    tags_to_write,
                                )

                                if file_write_success:
                                    await flash(
                                        "Track metadata updated and written to the audio file.",
                                        "success",
                                    )
                                else:
                                    await flash(
                                        "Track metadata updated, but writing tags to the audio file failed.",
                                        "warning",
                                    )
                            else:
                                await flash("Track metadata updated.", "success")

                        except Exception as tag_err:
                            logger.warning(
                                "Tag writing unavailable for track %s: %s",
                                track_id,
                                tag_err,
                            )
                            await flash(
                                "Track metadata updated. Audio tag writing was unavailable.",
                                "info",
                            )
                    elif file_path:
                        await flash(
                            "Track metadata updated, but the audio file could not be found on disk.",
                            "warning",
                        )
                    else:
                        await flash(
                            "Track metadata updated. No file path is available for audio tag writing.",
                            "info",
                        )

                return redirect(url_for("ui.track_detail", track_id=str(track_id)))

            # Recommended genres from other tracks by this artist.
            recommended_genres: list[str] = []
            artist_name = track.get("artist") or ""

            if artist_name:
                try:
                    genre_result = db.execute(
                        text("""
                            SELECT genres
                            FROM tracks
                            WHERE artist = :artist
                              AND genres IS NOT NULL
                              AND genres != ''
                            LIMIT 10
                        """),
                        {"artist": artist_name},
                    )

                    genre_set: set[str] = set()

                    for genre_row in genre_result.fetchall():
                        genre_text = genre_row._mapping.get("genres")
                        for genre in split_tag_values(genre_text):
                            genre_set.add(genre)

                    recommended_genres = sorted(genre_set)

                except Exception as rec_err:
                    logger.debug(
                        "Could not get recommended genres for track %s: %s",
                        track_id,
                        rec_err,
                    )

            # Favourite status.  Tracks are favourited as bookmark rows with
            # type='track_favourite' and name=<track id> (see track_routes.py);
            # the bookmarks table has no track_id column.
            is_track_favourite = False
            try:
                fav_result = db.execute(
                    text("""
                        SELECT 1
                        FROM bookmarks
                        WHERE type = 'track_favourite'
                          AND LOWER(name) = LOWER(:track_id)
                        LIMIT 1
                    """),
                    {"track_id": str(track_id)},
                )
                is_track_favourite = fav_result.fetchone() is not None
            except Exception as fav_err:
                logger.debug(
                    "Track favourite check failed for %s: %s",
                    track_id,
                    fav_err,
                )

        genre_sources = {}
        try:
            try:
                from services.enrichment.genre_tag_aggregator import get_track_genre_sources

                genre_sources = get_track_genre_sources(track)
            except ImportError:
                from services.enrichment.genre_tag_aggregator import get_track_genres_summary

                genre_sources = get_track_genres_summary(track)
        except Exception as ge_err:
            logger.debug(
                "Could not get genre sources for track %s: %s",
                track_id,
                ge_err,
            )

        return await render_template(
            "pages/track_detail.html",
            track=track,
            recommended_genres=recommended_genres,
            track_id=str(track_id),
            is_track_favourite=is_track_favourite,
            genre_sources=genre_sources,
            qbit_config=cfg.get(
                "qbittorrent",
                {"enabled": False, "web_url": "http://localhost:8080"},
            ),
            slskd_config=cfg.get(
                "slskd",
                {"enabled": False},
            ),
        )

    except Exception as exc:
        logger.error("Error loading track %s: %s", track_id, exc, exc_info=True)
        await flash(f"Error loading track: {exc}", "error")
        return redirect(url_for("ui.dashboard"))


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
        # Apply a changed log level immediately (no restart required).
        try:
            from helpers.logging_config import set_log_level
            log_cfg = data.get("logging") or {}
            level = log_cfg.get("level") or data.get("log_level")
            if level:
                applied = set_log_level(level)
                logger.info("[CONFIG] Log level set to %s", applied)
        except Exception as exc:
            logger.warning("[CONFIG] Could not apply log level at runtime: %s", exc)
        return jsonify({"success": True})
    return jsonify({"error": "Failed to save config"}), 500


@ui_bp.route("/config/migrate_postgres", methods=["POST"])
def config_migrate_postgres():
    return jsonify({"success": True})


@ui_bp.route("/logs")
async def logs():
    from helpers.logging_config import resolve_log_dir
    # Only surface logs that are actively written to.  Unused leftovers
    # (app.log, queue_processor.log) and unrelated files are hidden so the
    # /logs page stays a clean list of what the system actually produces.
    _USED_LOG_FILES = {
        "unified_scan.log",  # scanning progress
        "info.log",
        "debug.log",
        "queue.log",         # download queue activity
        "search.log",        # soulseek searches
        "access.log",        # hypercorn access log
        "error.log",         # hypercorn / app errors
    }
    log_dir = resolve_log_dir()
    log_files = []
    if os.path.isdir(log_dir):
        for f in sorted(os.listdir(log_dir)):
            if f.endswith(".log") and f in _USED_LOG_FILES:
                full = os.path.join(log_dir, f)
                size = os.path.getsize(full) if os.path.isfile(full) else 0
                log_files.append({"name": f, "path": full, "size": size})
    # Show the scanning progress log first so the page opens on the same
    # feed the dashboard's Scanning Log panel displays.
    log_files.sort(key=lambda f: (f["name"] != "unified_scan.log", f["name"]))
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
    from services.infrastructure.filesystem_service import resolve_downloads_dir
    return await render_template(
        "pages/downloads/monitor.html",
        qbit_config=cfg.get("qbittorrent", {}),
        slskd_config=cfg.get("slskd", {}),
        downloads_dir=resolve_downloads_dir(prefer_music_subfolder=False),
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


@ui_bp.route("/downloads/search")
async def downloads_search():
    """Unified search interface — all providers via tabs."""
    cfg = get_config()
    return await render_template(
        "pages/downloads/search.html",
        qbit_config=cfg.get("qbittorrent", {}),
        slskd_config=cfg.get("slskd", {}),
    )


# Legacy redirects — keep old URLs working.
@ui_bp.route("/downloads/search/soulseek")
async def downloads_search_soulseek():
    return redirect(url_for("ui.downloads_search") + "#search-soulseek")


@ui_bp.route("/downloads/search/musicbrainz")
async def downloads_search_musicbrainz():
    return redirect(url_for("ui.downloads_search") + "#search-musicbrainz")


@ui_bp.route("/downloads/search/qbittorrent")
async def downloads_search_qbittorrent():
    return redirect(url_for("ui.downloads_search") + "#search-qbittorrent")


@ui_bp.route("/downloads/search/playlists")
async def downloads_search_playlists():
    return redirect(url_for("ui.downloads_search") + "#search-playlists")


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
    from services.metadata.artist_service import get_artist_corrections
    data, code = get_artist_corrections(unquote(name or "").strip())
    if code != 200:
        return jsonify(data), code
    return await render_template("pages/artist_corrections.html", **data)


@ui_bp.route("/artist/<path:name>/genre-management")
async def artist_genre_management(name):
    return await render_template("pages/artist_genres.html", artist_name=name)


@ui_bp.route("/metadata-compare")
async def metadata_compare():
    """Metadata comparison page — compare Navidrome vs Beets album metadata."""
    try:
        with db_session() as session:
            result = session.execute(text("""
                SELECT
                    album,
                    COALESCE(NULLIF(album_artist, ''), artist) AS artist_name,
                    year AS navidrome_year,
                    beets_year,
                    navidrome_genres,
                    musicbrainz_genres,
                    COUNT(*) AS track_count
                FROM tracks
                WHERE album IS NOT NULL
                  AND album != ''
                  AND COALESCE(NULLIF(album_artist, ''), artist) IS NOT NULL
                  AND COALESCE(NULLIF(album_artist, ''), artist) != ''
                GROUP BY
                    album,
                    COALESCE(NULLIF(album_artist, ''), artist),
                    year,
                    beets_year,
                    navidrome_genres,
                    musicbrainz_genres
                ORDER BY
                    LOWER(COALESCE(NULLIF(album_artist, ''), artist)),
                    LOWER(album)
            """))
            rows = [dict(row._mapping) for row in result.fetchall()]

        album_comparisons: list[dict[str, Any]] = []

        for row in rows:
            album = row.get("album") or ""
            artist = row.get("artist_name") or ""
            nav_year = row.get("navidrome_year")
            beets_year = row.get("beets_year")
            nav_genres_raw = row.get("navidrome_genres") or ""
            beets_genres_raw = row.get("musicbrainz_genres") or ""
            track_count = int(row.get("track_count") or 0)

            nav_genres = [
                genre.strip()
                for genre in str(nav_genres_raw).split(",")
                if genre.strip()
            ]

            beets_genres = [
                genre.strip()
                for genre in str(beets_genres_raw).split(",")
                if genre.strip()
            ]

            has_year_mismatch = str(nav_year or "") != str(beets_year or "")
            has_genre_mismatch = sorted(g.lower() for g in nav_genres) != sorted(
                g.lower() for g in beets_genres
            )

            if has_year_mismatch or has_genre_mismatch:
                album_comparisons.append({
                    "album": album,
                    "artist": artist,
                    "track_count": track_count,
                    "navidrome": {
                        "year": nav_year,
                        "genres": nav_genres,
                    },
                    "beets": {
                        "year": beets_year,
                        "genres": beets_genres,
                    },
                })

        return await render_template(
            "pages/metadata_compare.html",
            album_comparisons=album_comparisons,
        )

    except Exception as exc:
        logger.error("metadata-compare: %s", exc, exc_info=True)
        await flash(f"Error loading metadata comparison: {exc}", "danger")
        return redirect(url_for("ui.dashboard"))


@ui_bp.route("/api/metadata-compare/search-musicbrainz", methods=["POST"])
async def metadata_compare_search_mb():
    """Search MusicBrainz for possible album metadata matches."""
    data = (await request.get_json(silent=True)) or {}

    artist = str(data.get("artist") or "").strip()
    album = str(data.get("album") or "").strip()

    if not artist or not album:
        return jsonify({"error": "artist and album required"}), 400

    try:
        from services.enrichment.musicbrainz_service import MusicBrainzService

        svc = MusicBrainzService()
        results: list[dict[str, Any]] = []

        raw_results = None

        if hasattr(svc, "search_releases"):
            raw_results = svc.search_releases(artist=artist, album=album)
        elif hasattr(svc, "search_album"):
            raw_results = svc.search_album(artist=artist, album=album)
        elif hasattr(svc, "search_release"):
            raw_results = svc.search_release(artist=artist, album=album)

        if raw_results:
            if isinstance(raw_results, dict):
                raw_results = [raw_results]

            for item in raw_results:
                if not isinstance(item, dict):
                    continue

                release_date = (
                    item.get("first-release-date")
                    or item.get("first_release_date")
                    or item.get("date")
                    or item.get("release_date")
                    or ""
                )

                year = item.get("year")
                if not year and release_date:
                    year = str(release_date).split("-")[0]

                genres = (
                    item.get("genres")
                    or item.get("tags")
                    or item.get("musicbrainz_genres")
                    or []
                )

                if isinstance(genres, str):
                    genres = [
                        genre.strip()
                        for genre in genres.split(",")
                        if genre.strip()
                    ]

                results.append({
                    "id": item.get("id") or item.get("mbid") or item.get("release_id"),
                    "title": item.get("title") or item.get("album") or album,
                    "album": item.get("album") or item.get("title") or album,
                    "artist": item.get("artist") or item.get("artist-credit") or artist,
                    "artist-credit": item.get("artist-credit") or item.get("artist") or artist,
                    "first-release-date": release_date,
                    "year": year,
                    "genres": genres,
                    "confidence": item.get("confidence"),
                })

        if not results and hasattr(svc, "get_suggested_mbid"):
            suggested = svc.get_suggested_mbid(album, artist)

            mbid = None
            confidence = None

            if isinstance(suggested, tuple):
                mbid = suggested[0] if len(suggested) > 0 else None
                confidence = suggested[1] if len(suggested) > 1 else None
            else:
                mbid = suggested

            if mbid:
                results.append({
                    "id": mbid,
                    "title": album,
                    "album": album,
                    "artist": artist,
                    "artist-credit": artist,
                    "first-release-date": "",
                    "year": None,
                    "genres": [],
                    "confidence": confidence,
                })

        return jsonify({
            "success": True,
            "results": results,
        })

    except Exception as exc:
        logger.error("metadata-compare MB search: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500



@ui_bp.route("/api/metadata-compare/apply-musicbrainz", methods=["POST"])
async def metadata_compare_apply_mb():
    """Apply selected MusicBrainz metadata to an album."""
    data = (await request.get_json(silent=True)) or {}

    artist = str(data.get("artist") or "").strip()
    album = str(data.get("album") or "").strip()
    mb_data = data.get("mb_data") or {}

    if not artist or not album or not isinstance(mb_data, dict):
        return jsonify({"error": "artist, album, and mb_data required"}), 400

    try:
        release_date = (
            mb_data.get("first-release-date")
            or mb_data.get("first_release_date")
            or mb_data.get("release_date")
            or ""
        )

        year = mb_data.get("year")
        if not year and release_date:
            year = str(release_date).split("-")[0]

        genres = mb_data.get("genres") or []

        if isinstance(genres, str):
            genres = [
                genre.strip()
                for genre in genres.split(",")
                if genre.strip()
            ]

        genres_text = ",".join(genres)

        mbid = (
            mb_data.get("id")
            or mb_data.get("mbid")
            or mb_data.get("release_id")
            or ""
        )

        with db_session() as session:
            result = session.execute(
                text("""
                    UPDATE tracks
                    SET
                        year = COALESCE(:year, year),
                        beets_year = COALESCE(:year, beets_year),
                        musicbrainz_genres = COALESCE(NULLIF(:genres, ''), musicbrainz_genres),
                        musicbrainz_album_mbid = COALESCE(NULLIF(:mbid, ''), musicbrainz_album_mbid),
                        mb_override = TRUE
                    WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)
                      AND LOWER(COALESCE(album, '')) = LOWER(:album)
                    RETURNING id
                """),
                {
                    "year": year,
                    "genres": genres_text,
                    "mbid": mbid,
                    "artist": artist,
                    "album": album,
                },
            )

            updated_ids = [row[0] for row in result.fetchall()]

        return jsonify({
            "success": True,
            "message": f"Applied MusicBrainz data to {artist} - {album} ({len(updated_ids)} tracks)",
            "tracks_updated": len(updated_ids),
        })

    except Exception as exc:
        logger.error("metadata-compare apply MB: %s", exc, exc_info=True)
        return jsonify({"error": str(exc)}), 500

@ui_bp.route("/api/metadata-compare/accept-navidrome", methods=["POST"])
async def metadata_compare_accept_navidrome():
    """Lock current Navidrome metadata for an album."""
    data = (await request.get_json(silent=True)) or {}

    artist = str(data.get("artist") or "").strip()
    album = str(data.get("album") or "").strip()

    if not artist or not album:
        return jsonify({"error": "artist and album required"}), 400

    try:
        with db_session() as session:
            session.execute(
                text("""
                    UPDATE tracks
                    SET metadata_locked = TRUE
                    WHERE LOWER(COALESCE(NULLIF(album_artist, ''), artist)) = LOWER(:artist)
                      AND LOWER(COALESCE(album, '')) = LOWER(:album)
                """),
                {
                    "artist": artist,
                    "album": album,
                },
            )

        return jsonify({
            "success": True,
            "message": f"Navidrome data locked for {artist} - {album}",
        })

    except Exception as exc:
        logger.error("metadata-compare accept navidrome: %s", exc, exc_info=True)
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
