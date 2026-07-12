"""ListenBrainz playlist sync service.

Handles RSS feed parsing, track matching, playlist persistence, and M3U
playlist file generation.  Migrated from the old monolithic app.py.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from db.engine import db_session
from db.utils import get_db_connection, row_get

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


def _ensure_tables(conn) -> None:
    cursor = conn.cursor()
    cursor.execute("""
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
        )""")
    cursor.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_lb_playlist_unique
        ON listenbrainz_playlist_tracks (username, playlist_key, track_uid)""")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS listenbrainz_playlist_scheduler_state (
            username TEXT PRIMARY KEY, last_synced_week TEXT,
            last_synced_at TEXT, last_rematch_at TEXT
        )""")
    conn.commit()


def _save_rows(conn, app_username, listenbrainz_username, playlist_key, tracks, replace_existing=False, week_key=None):
    cursor = conn.cursor()
    if replace_existing:
        cursor.execute(
            "DELETE FROM listenbrainz_playlist_tracks WHERE username = %s AND playlist_key = %s",
            (app_username, playlist_key),
        )
    now_iso = datetime.now().isoformat()
    spec = LISTENBRAINZ_PLAYLIST_SPECS.get(playlist_key, {})
    suffix = spec.get("suffix", playlist_key.replace("_", " ").title())
    playlist_name = f"{listenbrainz_username} {suffix}"

    for track in tracks:
        cursor.execute(
            """INSERT INTO listenbrainz_playlist_tracks
               (username, listenbrainz_username, playlist_key, playlist_name, track_uid,
                artist_name, track_name, release_name, recording_mbid, release_mbid,
                source, week_key, match_status, synced_at, metadata)
               VALUES (%s,%s,%s,%s,%s, %s,%s,%s,%s,%s, %s,%s,%s,%s,%s)
               ON CONFLICT (username, playlist_key, track_uid) DO NOTHING""",
            (
                app_username, listenbrainz_username, playlist_key, playlist_name,
                _lb_track_uid(track),
                track.get("artist_name"), track.get("track_name"), track.get("release_name"),
                track.get("recording_mbid"), track.get("release_mbid"),
                track.get("source"), week_key,
                track.get("match_status", "missing"),
                now_iso, json.dumps(track, ensure_ascii=False),
            ),
        )
    conn.commit()


def _load_rows(conn, app_username, playlist_key, limit=500):
    cursor = conn.cursor()
    cursor.execute(
        """SELECT p.playlist_name, p.artist_name, p.track_name, p.release_name,
                  p.recording_mbid, p.release_mbid, p.match_status, p.local_track_id,
                  p.file_path, p.queue_id, p.source, p.synced_at,
                  dq.status as queue_status, dq.failure_reason
           FROM listenbrainz_playlist_tracks p
           LEFT JOIN download_queue dq ON dq.id = p.queue_id
           WHERE p.username = %s AND p.playlist_key = %s
           ORDER BY p.id DESC LIMIT %s""",
        (app_username, playlist_key, limit),
    )
    rows = cursor.fetchall() or []
    tracks = []
    for row in rows:
        tracks.append({
            "artist": row_get(row, "artist_name", 1) or "",
            "title": row_get(row, "track_name", 2) or "",
            "album": row_get(row, "release_name", 3) or "",
            "recording_mbid": row_get(row, "recording_mbid", 4) or "",
            "release_mbid": row_get(row, "release_mbid", 5) or "",
            "match_status": row_get(row, "match_status", 6) or "missing",
            "track_id": row_get(row, "local_track_id", 7),
            "file_path": row_get(row, "file_path", 8),
            "queue_id": row_get(row, "queue_id", 9),
            "source": row_get(row, "source", 10) or "",
            "synced_at": row_get(row, "synced_at", 11) or "",
            "queue_status": row_get(row, "queue_status", 12),
            "queue_failure_reason": row_get(row, "failure_reason", 13),
        })
    return tracks


def _match_in_library(conn, track):
    cursor = conn.cursor()
    rec_mbid = (track.get("recording_mbid") or "").strip()
    if rec_mbid:
        cursor.execute("SELECT id, file_path FROM tracks WHERE musicbrainz_id = %s LIMIT 1", (rec_mbid,))
        row = cursor.fetchone()
        if row:
            return {"track_id": row_get(row, "id", 0), "file_path": row_get(row, "file_path", 1)}
    artist_name = (track.get("artist_name") or "").strip()
    track_name = (track.get("track_name") or "").strip()
    if artist_name and track_name:
        cursor.execute(
            "SELECT id, file_path FROM tracks WHERE LOWER(artist) = LOWER(%s) AND LOWER(title) = LOWER(%s) "
            "ORDER BY last_scanned DESC NULLS LAST LIMIT 1",
            (artist_name, track_name),
        )
        row = cursor.fetchone()
        if row:
            return {"track_id": row_get(row, "id", 0), "file_path": row_get(row, "file_path", 1)}
    return None


def _rematch_missing(conn, app_username):
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, recording_mbid, artist_name, track_name, playlist_key FROM listenbrainz_playlist_tracks "
        "WHERE username = %s AND match_status IN ('missing', 'queued')",
        (app_username,),
    )
    rows = cursor.fetchall() or []
    matched = 0
    for row in rows:
        row_id = row_get(row, "id", 0)
        track = {
            "recording_mbid": row_get(row, "recording_mbid", 1) or "",
            "artist_name": row_get(row, "artist_name", 2) or "",
            "track_name": row_get(row, "track_name", 3) or "",
        }
        result = _match_in_library(conn, track)
        if result:
            cursor.execute(
                "UPDATE listenbrainz_playlist_tracks SET match_status = 'matched', local_track_id = %s, file_path = %s "
                "WHERE id = %s",
                (result["track_id"], result["file_path"], row_id),
            )
            matched += 1
    conn.commit()
    return matched


# ── Public API ────────────────────────────────────────────────────────────────


def sync_rss_playlists_for_user(app_username: str, listenbrainz_username: str) -> dict:
    """Sync ListenBrainz RSS feeds into the database for a user."""
    conn = get_db_connection()
    try:
        _ensure_tables(conn)
        week_key = f"{datetime.now().isocalendar().year}-W{datetime.now().isocalendar().week:02d}"
        feed_keys = ["weekly_jams", "weekly_exploration", "last_week_jams", "last_week_exploration"]

        results: dict[str, list] = {}
        for feed_key in feed_keys:
            tracks = _fetch_feed_tracks(listenbrainz_username, feed_key)
            results[feed_key] = tracks
            _save_rows(conn, app_username, listenbrainz_username, feed_key, tracks, replace_existing=True, week_key=week_key)

        # Rolling: merge weekly + last week
        for key, src_keys in [("rolling_jams", ["weekly_jams", "last_week_jams"]),
                               ("rolling_exploration", ["weekly_exploration", "last_week_exploration"])]:
            merged = []
            for sk in src_keys:
                merged.extend(results.get(sk, []))
            _save_rows(conn, app_username, listenbrainz_username, key, merged, replace_existing=False, week_key=week_key)

        conn.commit()
        return {"success": True, "username": app_username, "feeds": list(results.keys())}
    except Exception as exc:
        logger.error("LB RSS sync failed: %s", exc)
        return {"success": False, "error": str(exc)}
    finally:
        conn.close()


def get_playlists_for_user(app_username: str) -> dict:
    """Return persisted LB playlists for a user."""
    conn = get_db_connection()
    try:
        _ensure_tables(conn)
        result = {}
        for key in LISTENBRAINZ_PLAYLIST_SPECS:
            tracks = _load_rows(conn, app_username, key)
            if tracks:
                result[key] = tracks
        return {"success": True, "playlists": result}
    except Exception as exc:
        return {"success": False, "error": str(exc)}
    finally:
        conn.close()


def get_sync_status(app_username: str) -> dict:
    """Return last sync/rematch time for a user."""
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT last_synced_week, last_synced_at, last_rematch_at FROM listenbrainz_playlist_scheduler_state WHERE username = %s",
            (app_username,),
        )
        row = cursor.fetchone()
        if row:
            return {
                "success": True,
                "last_synced_week": row_get(row, "last_synced_week", 0) or "",
                "last_synced_at": row_get(row, "last_synced_at", 1),
                "last_rematch_at": row_get(row, "last_rematch_at", 2),
            }
        return {"success": True, "last_synced_week": None, "last_synced_at": None, "last_rematch_at": None}
    except Exception as exc:
        return {"success": False, "error": str(exc)}
    finally:
        conn.close()
