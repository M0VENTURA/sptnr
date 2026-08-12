"""ListenBrainz playlist sync service.

Handles RSS feed parsing, track matching, playlist persistence, and M3U
playlist file generation.  Migrated from the old monolithic app.py.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any
import httpx

from sqlalchemy import text
from db.engine import db_session
from db.utils import row_get

logger = logging.getLogger(__name__)

LISTENBRAINZ_PLAYLIST_SPECS: dict[str, dict[str, str]] = {
    "weekly_jams": {"suffix": "Weekly Jams", "bucket": "jams"},
    "weekly_exploration": {"suffix": "Weekly Exploration", "bucket": "exploration"},
    "last_week_jams": {"suffix": "Last Weeks Jams", "bucket": "jams"},
    "last_week_exploration": {"suffix": "Last Weeks Exploration", "bucket": "exploration"},
    "rolling_jams": {"suffix": "Jams", "bucket": "jams"},
    "rolling_exploration": {"suffix": "Exploration", "bucket": "exploration"},
}


# ── RSS helpers ──────────────────────────────────────────────────────────────


def _extract_mbid(value: str) -> str | None:
    if not value:
        return None
    m = re.search(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})", value)
    return m.group(1) if m else None


def _normalize_lb_text(value: str) -> str:
    text = (value or "").strip().lower()
    if not text:
        return ""
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = re.sub(r"\s+", " ", text)
    return text


def _lb_track_uid(track: dict) -> str:
    mbid = (track.get("recording_mbid") or "").strip().lower()
    if mbid:
        return "mbid:" + mbid
    key = "|".join([
        _normalize_lb_text(track.get("artist_name")),
        _normalize_lb_text(track.get("track_name")),
        _normalize_lb_text(track.get("release_name")),
    ])
    return "hash:" + hashlib.sha1(key.encode("utf-8")).hexdigest()


def _rss_candidates(username: str, rec_type: str) -> list[str]:
    slug_map = {
        "weekly_jams": "weekly-jams",
        "weekly_exploration": "weekly-exploration",
        "last_week_jams": "last-week-jams",
        "last_week_exploration": "last-week-exploration",
    }
    slug = slug_map.get(rec_type, rec_type.replace("_", "-"))
    return [
        f"https://listenbrainz.org/user/{username}/recommendations/{slug}/rss",
        f"https://listenbrainz.org/user/{username}/recommendations/{slug}.rss",
        f"https://listenbrainz.org/user/{username}/playlists/{slug}/rss",
    ]


def _parse_rss(xml_text: str) -> list[dict]:
    tracks: list[dict] = []
    if not xml_text:
        return tracks
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return tracks

    def _append(title_text, creator_text, guid_text, link_text, desc_text):
        artist = (creator_text or "").strip()
        track_name = (title_text or "").strip()
        if " - " in title_text and not artist:
            parts = title_text.split(" - ", 1)
            artist = parts[0].strip()
            track_name = parts[1].strip()
        recording_mbid = _extract_mbid(guid_text) or _extract_mbid(link_text) or _extract_mbid(desc_text)
        if not artist and not track_name:
            return
        tracks.append({
            "artist_name": artist,
            "track_name": track_name,
            "release_name": "",
            "recording_mbid": recording_mbid or "",
            "release_mbid": None,
            "source": "listenbrainz-rss",
        })

    for item in root.findall(".//item"):
        _append(
            item.findtext("title"),
            item.findtext("{http://purl.org/dc/elements/1.1/}creator"),
            item.findtext("guid"),
            item.findtext("link"),
            item.findtext("description"),
        )
    for entry in root.findall(".//{http://www.w3.org/2005/Atom}entry"):
        atom_title = entry.findtext("{http://www.w3.org/2005/Atom}title")
        atom_id = entry.findtext("{http://www.w3.org/2005/Atom}id")
        atom_summary = entry.findtext("{http://www.w3.org/2005/Atom}summary")
        atom_content = entry.findtext("{http://www.w3.org/2005/Atom}content")
        author = entry.find("{http://www.w3.org/2005/Atom}author")
        atom_creator = author.findtext("{http://www.w3.org/2005/Atom}name") if author is not None else ""
        atom_link = ""
        link_node = entry.find("{http://www.w3.org/2005/Atom}link")
        if link_node is not None:
            atom_link = link_node.get("href", "")
        _append(atom_title, atom_creator, atom_id, atom_link, atom_summary or atom_content)

    return tracks


def _fetch_feed_tracks(listenbrainz_username: str, rec_type: str) -> list[dict]:
    from api_clients.listenbrainz import ListenBrainzUserClient

    headers = {"User-Agent": "popularr/1.0", "Accept": "application/rss+xml, application/atom+xml"}
    for url in _rss_candidates(listenbrainz_username, rec_type):
        try:
            resp = httpx.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                tracks = _parse_rss(resp.text)
                if tracks:
                    return tracks
        except Exception:
            continue
    return []


# ── DB helpers ───────────────────────────────────────────────────────────────


def _ensure_tables(conn=None) -> None:
    from sqlalchemy import text as _text
    from db.engine import db_session as _db_session
    with _db_session() as session:
        session.execute(_text("""
            CREATE TABLE IF NOT EXISTS listenbrainz_playlist_tracks (
                id SERIAL PRIMARY KEY,
                username TEXT NOT NULL,
                listenbrainz_username TEXT NOT NULL,
                playlist_key TEXT NOT NULL,
                playlist_name TEXT NOT NULL,
                track_uid TEXT NOT NULL,
                artist_name TEXT, track_name TEXT, release_name TEXT,
                recording_mbid TEXT, release_mbid TEXT, source TEXT,
                week_key TEXT, match_status TEXT NOT NULL,
                local_track_id INTEGER, file_path TEXT, queue_id INTEGER,
                synced_at TEXT NOT NULL, metadata TEXT
            )"""))
        session.execute(_text("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_lb_playlist_unique
            ON listenbrainz_playlist_tracks (username, playlist_key, track_uid)"""))
        session.execute(_text("""
            CREATE TABLE IF NOT EXISTS listenbrainz_playlist_scheduler_state (
                username TEXT PRIMARY KEY, last_synced_week TEXT,
                last_synced_at TEXT, last_rematch_at TEXT
            )"""))


def _save_rows(conn=None, app_username="", listenbrainz_username="", playlist_key="", tracks=None, replace_existing=False, week_key=None):
    from sqlalchemy import text as _text
    from db.engine import db_session as _db_session
    with _db_session() as session:
        if replace_existing:
            session.execute(
                _text("DELETE FROM listenbrainz_playlist_tracks WHERE username = :u AND playlist_key = :k"),
                {"u": app_username, "k": playlist_key},
            )
        now_iso = datetime.now().isoformat()
        spec = LISTENBRAINZ_PLAYLIST_SPECS.get(playlist_key, {})
        suffix = spec.get("suffix", playlist_key.replace("_", " ").title())
        playlist_name = f"{listenbrainz_username} {suffix}"

        for track in tracks or []:
            session.execute(
                _text("""INSERT INTO listenbrainz_playlist_tracks
                   (username, listenbrainz_username, playlist_key, playlist_name, track_uid,
                    artist_name, track_name, release_name, recording_mbid, release_mbid,
                    source, week_key, match_status, synced_at, metadata)
                   VALUES (:u,:lu,:k,:pn,:uid, :an,:tn,:rn,:rm,:lm, :src,:wk,:ms,:syn,:meta)
                   ON CONFLICT (username, playlist_key, track_uid) DO NOTHING"""),
                {
                    "u": app_username, "lu": listenbrainz_username, "k": playlist_key,
                    "pn": playlist_name, "uid": _lb_track_uid(track),
                    "an": track.get("artist_name"), "tn": track.get("track_name"),
                    "rn": track.get("release_name"), "rm": track.get("recording_mbid"),
                    "lm": track.get("release_mbid"), "src": track.get("source"),
                    "wk": week_key, "ms": track.get("match_status", "missing"),
                    "syn": now_iso, "meta": json.dumps(track, ensure_ascii=False),
                },
            )


def _load_rows(conn=None, app_username="", playlist_key="", limit=500):
    from sqlalchemy import text as _text
    from db.engine import db_session as _db_session
    with _db_session() as session:
        rows = session.execute(
            _text("""SELECT p.playlist_name, p.artist_name, p.track_name, p.release_name,
                      p.recording_mbid, p.release_mbid, p.match_status, p.local_track_id,
                      p.file_path, p.queue_id, p.source, p.synced_at,
                      dq.status as queue_status, dq.failure_reason
               FROM listenbrainz_playlist_tracks p
               LEFT JOIN download_queue dq ON dq.id = p.queue_id
               WHERE p.username = :u AND p.playlist_key = :k
               ORDER BY p.id DESC LIMIT :lim"""),
            {"u": app_username, "k": playlist_key, "lim": limit},
        ).fetchall() or []
    tracks = []
    for row in rows:
        tracks.append({
            "artist": row[1] or "",
            "title": row[2] or "",
            "album": row[3] or "",
            "recording_mbid": row[4] or "",
            "release_mbid": row[5] or "",
            "match_status": row[6] or "missing",
            "track_id": row[7],
            "file_path": row[8],
            "queue_id": row[9],
            "source": row[10] or "",
            "synced_at": row[11] or "",
            "queue_status": row[12],
            "queue_failure_reason": row[13],
        })
    return tracks


def _match_in_library(conn=None, track=None):
    from sqlalchemy import text as _text
    from db.engine import db_session as _db_session
    with _db_session() as session:
        rec_mbid = (track.get("recording_mbid") or "").strip()
        if rec_mbid:
            row = session.execute(
                _text("SELECT id, file_path FROM tracks WHERE musicbrainz_id = :m LIMIT 1"),
                {"m": rec_mbid},
            ).fetchone()
            if row:
                return {"track_id": row[0], "file_path": row[1]}
        artist_name = (track.get("artist_name") or "").strip()
        track_name = (track.get("track_name") or "").strip()
        if artist_name and track_name:
            row = session.execute(
                _text(
                    "SELECT id, file_path FROM tracks WHERE LOWER(artist) = LOWER(:a) AND LOWER(title) = LOWER(:t) "
                    "ORDER BY last_scanned DESC NULLS LAST LIMIT 1"
                ),
                {"a": artist_name, "t": track_name},
            ).fetchone()
            if row:
                return {"track_id": row[0], "file_path": row[1]}
    return None


def _rematch_missing(conn=None, app_username=""):
    from sqlalchemy import text as _text
    from db.engine import db_session as _db_session
    with _db_session() as session:
        rows = session.execute(
            _text(
                "SELECT id, recording_mbid, artist_name, track_name, playlist_key FROM listenbrainz_playlist_tracks "
                "WHERE username = :u AND match_status IN ('missing', 'queued')"
            ),
            {"u": app_username},
        ).fetchall() or []
        matched = 0
        for row in rows:
            row_id = row[0]
            track = {
                "recording_mbid": row[1] or "",
                "artist_name": row[2] or "",
                "track_name": row[3] or "",
            }
            result = _match_in_library(track=track)
            if result:
                session.execute(
                    _text(
                        "UPDATE listenbrainz_playlist_tracks SET match_status = 'matched', local_track_id = :tid, file_path = :fp "
                        "WHERE id = :id"
                    ),
                    {"tid": result["track_id"], "fp": result["file_path"], "id": row_id},
                )
                matched += 1
    return matched


# ── Public API ────────────────────────────────────────────────────────────────


def sync_rss_playlists_for_user(app_username: str, listenbrainz_username: str) -> dict:
    """Sync ListenBrainz RSS feeds into the database for a user."""
    try:
        _ensure_tables()
        week_key = f"{datetime.now().isocalendar().year}-W{datetime.now().isocalendar().week:02d}"
        feed_keys = ["weekly_jams", "weekly_exploration", "last_week_jams", "last_week_exploration"]

        results: dict[str, list] = {}
        for feed_key in feed_keys:
            tracks = _fetch_feed_tracks(listenbrainz_username, feed_key)
            results[feed_key] = tracks
            _save_rows(app_username=app_username, listenbrainz_username=listenbrainz_username, playlist_key=feed_key, tracks=tracks, replace_existing=True, week_key=week_key)

        # Rolling: merge weekly + last week
        for key, src_keys in [("rolling_jams", ["weekly_jams", "last_week_jams"]),
                               ("rolling_exploration", ["weekly_exploration", "last_week_exploration"])]:
            merged = []
            for sk in src_keys:
                merged.extend(results.get(sk, []))
            _save_rows(app_username=app_username, listenbrainz_username=listenbrainz_username, playlist_key=key, tracks=merged, replace_existing=False, week_key=week_key)

        return {"success": True, "username": app_username, "feeds": list(results.keys())}
    except Exception as exc:
        logger.error("LB RSS sync failed: %s", exc)
        return {"success": False, "error": str(exc)}


def get_playlists_for_user(app_username: str) -> dict:
    """Return persisted LB playlists for a user."""
    try:
        _ensure_tables()
        result = {}
        for key in LISTENBRAINZ_PLAYLIST_SPECS:
            tracks = _load_rows(app_username=app_username, playlist_key=key)
            if tracks:
                result[key] = tracks
        return {"success": True, "playlists": result}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def get_sync_status(app_username: str) -> dict:
    """Return last sync/rematch time for a user."""
    try:
        from sqlalchemy import text as _text
        from db.engine import db_session as _db_session
        with _db_session() as session:
            row = session.execute(
                _text(
                    "SELECT last_synced_week, last_synced_at, last_rematch_at FROM listenbrainz_playlist_scheduler_state WHERE username = :u"
                ),
                {"u": app_username},
            ).fetchone()
        if row:
            return {
                "success": True,
                "last_synced_week": row[0] or "",
                "last_synced_at": row[1],
                "last_rematch_at": row[2],
            }
        return {"success": True, "last_synced_week": None, "last_synced_at": None, "last_rematch_at": None}
    except Exception as exc:
        return {"success": False, "error": str(exc)}
