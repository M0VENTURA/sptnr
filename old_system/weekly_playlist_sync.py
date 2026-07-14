#!/usr/bin/env python3
"""
Weekly Playlist Sync Module

Creates public Navidrome playlists once a week for each user from:
- Last.fm recommendations (using re-command's undocumented endpoint approach)
- ListenBrainz recommendations (via existing CF/RSS API)

Missing tracks are added to the download queue with MusicBrainz metadata.
An hourly job checks if missing tracks have been downloaded and updates
Navidrome playlists accordingly.

Playlist naming convention: "{Username}-{Source}-DiscoverWeekly"
  e.g. "Aaron-LastFM-DiscoverWeekly", "Aaron-ListenBrainz-DiscoverWeekly"
"""

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

PLAYLIST_NAME_TEMPLATE = "{username}-{source}-DiscoverWeekly"
WEEKLY_SYNC_HOUR = 1
WEEKLY_SYNC_WEEKDAY = 0


def fetch_lastfm_recommendations(username: str, api_key: str) -> List[Dict[str, Any]]:
    """
    Fetch Last.fm recommended tracks for a user.
    Tries the undocumented /player/station/user/{user}/recommended endpoint first
    (used by re-command). Falls back to official API top tracks if that fails.
    """
    tracks = _fetch_lastfm_undocumented_recommendations(username)
    if tracks:
        return tracks
    return _fetch_lastfm_official_recommendations(username, api_key)


def _fetch_lastfm_undocumented_recommendations(username: str) -> List[Dict[str, Any]]:
    if not username:
        return []
    url = f"https://www.last.fm/player/station/user/{username}/recommended"
    headers = {
        "Referer": "https://www.last.fm/",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
    }
    try:
        resp = requests.get(url, headers=headers, timeout=(5, 20))
        if resp.status_code != 200:
            logger.debug("[LASTFM_UNDOC] Station endpoint returned %s for %s", resp.status_code, username)
            return []
        data = resp.json()
        playlist = data.get("playlist", [])
        tracks = []
        for item in playlist:
            artists = item.get("artists", [])
            artist = artists[0].get("name", "") if artists else ""
            title = item.get("name", "")
            album = ""
            alb = item.get("album")
            if isinstance(alb, dict):
                album = alb.get("name", "")
            if artist and title:
                tracks.append({
                    "artist_name": artist,
                    "track_name": title,
                    "release_name": album,
                    "recording_mbid": "",
                    "release_mbid": "",
                    "source": "lastfm-undocumented",
                })
        logger.info("[LASTFM_UNDOC] Got %s recommendations for %s", len(tracks), username)
        return tracks
    except Exception as e:
        logger.debug("[LASTFM_UNDOC] Failed for %s: %s", username, e)
        return []


def _fetch_lastfm_official_recommendations(username: str, api_key: str) -> List[Dict[str, Any]]:
    if not api_key or not username:
        return []
    url = "https://ws.audioscrobbler.com/2.0/"
    params = {
        "method": "user.getTopTracks",
        "user": username,
        "api_key": api_key,
        "format": "json",
        "limit": 50,
        "period": "7day",
    }
    try:
        resp = requests.get(url, params=params, timeout=(5, 20))
        resp.raise_for_status()
        data = resp.json()
        track_list = data.get("toptracks", {}).get("track", [])
        if isinstance(track_list, dict):
            track_list = [track_list]
        tracks = []
        for item in track_list:
            artist_field = item.get("artist", {})
            artist = artist_field.get("name", "") if isinstance(artist_field, dict) else str(artist_field)
            title = item.get("name", "")
            if artist and title:
                tracks.append({
                    "artist_name": artist,
                    "track_name": title,
                    "release_name": "",
                    "recording_mbid": "",
                    "release_mbid": "",
                    "source": "lastfm-official",
                })
        logger.info("[LASTFM_OFFICIAL] Got %s top tracks for %s", len(tracks), username)
        return tracks
    except Exception as e:
        logger.warning("[LASTFM_OFFICIAL] Failed for %s: %s", username, e)
        return []


def fetch_listenbrainz_recommendations(username: str, token: str) -> List[Dict[str, Any]]:
    if not token or not username:
        return []
    try:
        from api_clients.audiodb_and_listenbrainz import ListenBrainzUserClient
        client = ListenBrainzUserClient(token)
        created_for = client.get_created_for_playlists(username)
        if not isinstance(created_for, dict):
            return []
        tracks = created_for.get("weekly_jams", [])
        for t in tracks:
            t["source"] = t.get("source", "listenbrainz-cf")
        logger.info("[LISTENBRAINZ] Got %s CF recommendations for %s", len(tracks), username)
        return tracks
    except Exception as e:
        logger.warning("[LISTENBRAINZ] Failed for %s: %s", username, e)
        return []


def _enrich_track_with_musicbrainz(track: Dict[str, Any]) -> Dict[str, Any]:
    artist = track.get("artist_name", "")
    title = track.get("track_name", "")
    if not artist or not title:
        return track
    try:
        from api_clients.musicbrainz import lookup_recording_clean_names
        result = lookup_recording_clean_names(title, artist, enabled=True)
        if result.get("recording_mbid"):
            track["recording_mbid"] = result["recording_mbid"]
        if result.get("artist"):
            track["artist_name"] = result["artist"]
        if result.get("title"):
            track["track_name"] = result["title"]
    except Exception as e:
        logger.debug("[MB_ENRICH] Failed for %s - %s: %s", artist, title, e)
    return track


def _match_track_in_library(conn, track: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    cursor = conn.cursor()
    placeholder = "%s"
    rec_mbid = (track.get("recording_mbid") or "").strip()
    if rec_mbid:
        cursor.execute(
            f"SELECT id, file_path FROM tracks WHERE musicbrainz_id = {placeholder} LIMIT 1",
            (rec_mbid,),
        )
        row = cursor.fetchone()
        if row:
            return {"track_id": row.get("id") if isinstance(row, dict) else row[0],
                    "file_path": row.get("file_path") if isinstance(row, dict) else row[1]}
    artist_name = (track.get("artist_name") or "").strip()
    track_name = (track.get("track_name") or "").strip()
    if artist_name and track_name:
        cursor.execute(
            f"""SELECT id, file_path FROM tracks
                WHERE LOWER(artist) = LOWER({placeholder})
                  AND LOWER(title) = LOWER({placeholder})
                ORDER BY last_scanned DESC NULLS LAST
                LIMIT 1""",
            (artist_name, track_name),
        )
        row = cursor.fetchone()
        if row:
            return {"track_id": row.get("id") if isinstance(row, dict) else row[0],
                    "file_path": row.get("file_path") if isinstance(row, dict) else row[1]}
    return None


def _add_track_to_queue(track: Dict[str, Any]) -> Optional[int]:
    from download_queue_manager import add_to_queue
    artist = track.get("artist_name") or "Unknown Artist"
    title = track.get("track_name") or "Unknown Track"
    album = track.get("release_name") or None
    release_mbid = track.get("release_mbid") or None
    recording_mbid = track.get("recording_mbid") or None
    queued = add_to_queue(
        artist=artist,
        title=title,
        album=album,
        source="lastfm" if "lastfm" in track.get("source", "") else "listenbrainz",
        import_type="playlist",
        release_source="musicbrainz" if recording_mbid else ("lastfm" if "lastfm" in track.get("source", "") else "listenbrainz"),
        release_mbid=release_mbid,
        recording_mbid=recording_mbid,
    )
    if queued and isinstance(queued, dict):
        return queued.get("id")
    return None


def _create_or_update_navidrome_playlist(
    base_url: str,
    user: str,
    password: str,
    playlist_name: str,
    tracks: List[Dict[str, Any]],
    is_public: bool = True,
) -> Optional[str]:
    """Create or update a Navidrome playlist with the given tracks."""
    try:
        from api_clients.navidrome import NavidromeClient
        client = NavidromeClient(base_url, user, password)
        existing = client.find_playlist_by_name(playlist_name)
        playlist_id = None
        if existing:
            playlist_id = existing.get("id")
            # Clear existing tracks by recreating
            client.delete_playlist(playlist_id)
            existing = None
        # Create new playlist
        import requests as req
        create_response = req.post(
            f"{base_url}/rest/createPlaylist.view",
            params={
                "u": user,
                "p": password,
                "c": "popularr",
                "f": "json",
                "name": playlist_name,
                "comment": f"Auto-generated weekly discovery playlist",
                "public": "true" if is_public else "false",
            },
            timeout=10,
        )
        create_data = create_response.json()
        playlist_id = create_data.get("subsonic-response", {}).get("playlist", {}).get("id")
        if not playlist_id:
            logger.error("[WEEKLY_SYNC] Failed to create playlist %s", playlist_name)
            return None
        # Add tracks
        for t in tracks:
            tid = t.get("id")
            if not tid:
                continue
            req.post(
                f"{base_url}/rest/updatePlaylist.view",
                params={
                    "u": user,
                    "p": password,
                    "c": "popularr",
                    "f": "json",
                    "playlistId": playlist_id,
                    "songIdToAdd": tid,
                },
                timeout=10,
            )
        logger.info("[WEEKLY_SYNC] Created/updated playlist %s with %s tracks", playlist_name, len(tracks))
        return playlist_id
    except Exception as e:
        logger.error("[WEEKLY_SYNC] Error creating playlist %s: %s", playlist_name, e)
        return None


def _ensure_weekly_sync_tables(conn):
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS weekly_sync_state (
            username TEXT NOT NULL,
            source TEXT NOT NULL,
            last_synced_week TEXT,
            last_synced_at TEXT,
            navidrome_playlist_id TEXT,
            PRIMARY KEY (username, source)
        )""")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS weekly_playlist_tracks (
            id SERIAL PRIMARY KEY,
            username TEXT NOT NULL,
            source TEXT NOT NULL,
            artist_name TEXT,
            track_name TEXT,
            release_name TEXT,
            recording_mbid TEXT,
            release_mbid TEXT,
            match_status TEXT NOT NULL DEFAULT 'missing',
            local_track_id INTEGER,
            file_path TEXT,
            queue_id INTEGER,
            synced_at TEXT NOT NULL,
            week_key TEXT NOT NULL
        )""")
    cursor.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_weekly_playlist_unique
        ON weekly_playlist_tracks (username, source, artist_name, track_name)""")
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_weekly_playlist_status
        ON weekly_playlist_tracks (username, source, match_status)""")
    conn.commit()


def _get_week_key() -> str:
    now = datetime.now()
    return f"{now.isocalendar().year}-W{now.isocalendar().week:02d}"


def run_weekly_sync_for_user(
    app_user: str,
    base_url: str,
    navidrome_user: str,
    navidrome_pass: str,
    lastfm_username: Optional[str] = None,
    lastfm_api_key: Optional[str] = None,
    listenbrainz_username: Optional[str] = None,
    listenbrainz_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Run weekly playlist sync for a single user."""
    from helpers.db_utils import get_db_connection
    conn = get_db_connection()
    try:
        _ensure_weekly_sync_tables(conn)
        cursor = conn.cursor()
        placeholder = "%s"
        week_key = _get_week_key()
        results = {}

        # Last.fm sync
        if lastfm_username and lastfm_api_key:
            tracks = fetch_lastfm_recommendations(lastfm_username, lastfm_api_key)
            if tracks:
                playlist_name = PLAYLIST_NAME_TEMPLATE.format(username=app_user, source="LastFM")
                result = _process_tracks_for_playlist(
                    conn, cursor, placeholder, app_user, "LastFM", week_key,
                    playlist_name, tracks, base_url, navidrome_user, navidrome_pass
                )
                results["lastfm"] = result

        # ListenBrainz sync
        if listenbrainz_username and listenbrainz_token:
            tracks = fetch_listenbrainz_recommendations(listenbrainz_username, listenbrainz_token)
            if tracks:
                playlist_name = PLAYLIST_NAME_TEMPLATE.format(username=app_user, source="ListenBrainz")
                result = _process_tracks_for_playlist(
                    conn, cursor, placeholder, app_user, "ListenBrainz", week_key,
                    playlist_name, tracks, base_url, navidrome_user, navidrome_pass
                )
                results["listenbrainz"] = result

        conn.commit()
        return {"success": True, "username": app_user, "week": week_key, "results": results}
    except Exception as e:
        logger.error("[WEEKLY_SYNC] Failed for %s: %s", app_user, e, exc_info=True)
        try:
            conn.rollback()
        except Exception:
            pass
        return {"success": False, "error": str(e)}
    finally:
        conn.close()


def _process_tracks_for_playlist(
    conn, cursor, placeholder, app_user, source, week_key,
    playlist_name, tracks, base_url, navidrome_user, navidrome_pass
):
    matched_tracks = []
    missing_tracks = []
    for track in tracks:
        track = _enrich_track_with_musicbrainz(track)
        match = _match_track_in_library(conn, track)
        if match:
            track["match_status"] = "matched"
            track["local_track_id"] = match["track_id"]
            track["file_path"] = match["file_path"]
            matched_tracks.append(track)
        else:
            track["match_status"] = "missing"
            track["local_track_id"] = None
            track["file_path"] = None
            queue_id = _add_track_to_queue(track)
            if queue_id:
                track["match_status"] = "queued"
                track["queue_id"] = queue_id
            missing_tracks.append(track)

    # Save to DB
    cursor.execute(
        f"DELETE FROM weekly_playlist_tracks WHERE username = {placeholder} AND source = {placeholder} AND week_key = {placeholder}",
        (app_user, source, week_key),
    )
    now_iso = datetime.now().isoformat()
    for track in matched_tracks + missing_tracks:
        cursor.execute(
            f"""INSERT INTO weekly_playlist_tracks
                (username, source, artist_name, track_name, release_name, recording_mbid,
                 release_mbid, match_status, local_track_id, file_path, queue_id, synced_at, week_key)
                VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder},
                        {placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder},
                        {placeholder}, {placeholder}, {placeholder})
                ON CONFLICT (username, source, artist_name, track_name) DO UPDATE SET
                    match_status = EXCLUDED.match_status,
                    local_track_id = EXCLUDED.local_track_id,
                    file_path = EXCLUDED.file_path,
                    queue_id = EXCLUDED.queue_id,
                    week_key = EXCLUDED.week_key,
                    synced_at = EXCLUDED.synced_at""",
            (app_user, source, track.get("artist_name"), track.get("track_name"),
             track.get("release_name"), track.get("recording_mbid"), track.get("release_mbid"),
             track.get("match_status"), track.get("local_track_id"), track.get("file_path"),
             track.get("queue_id"), now_iso, week_key),
        )

    # Create Navidrome playlist with matched tracks only
    playlist_songs = []
    for t in matched_tracks:
        tid = t.get("local_track_id")
        if tid:
            playlist_songs.append({"id": tid, "artist": t.get("artist_name"), "title": t.get("track_name")})

    playlist_id = None
    if playlist_songs:
        playlist_id = _create_or_update_navidrome_playlist(
            base_url, navidrome_user, navidrome_pass, playlist_name, playlist_songs, is_public=True
        )

    # Update sync state
    cursor.execute(
        f"""INSERT INTO weekly_sync_state (username, source, last_synced_week, last_synced_at, navidrome_playlist_id)
            VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder})
            ON CONFLICT (username, source) DO UPDATE SET
                last_synced_week = EXCLUDED.last_synced_week,
                last_synced_at = EXCLUDED.last_synced_at,
                navidrome_playlist_id = EXCLUDED.navidrome_playlist_id""",
        (app_user, source, week_key, now_iso, playlist_id),
    )

    return {
        "playlist_name": playlist_name,
        "playlist_id": playlist_id,
        "total": len(tracks),
        "matched": len(matched_tracks),
        "missing": len(missing_tracks),
    }


def run_hourly_playlist_update() -> Dict[str, Any]:
    """
    Check for newly downloaded tracks and update Navidrome playlists.
    Returns summary of updates.
    """
    from helpers.db_utils import get_db_connection
    from helpers.config_helpers import get_config

    cfg = get_config()
    users = cfg.get("navidrome_users", []) or []
    if not users:
        return {"success": True, "updated": 0, "message": "No users configured"}

    conn = get_db_connection()
    total_updated = 0
    try:
        _ensure_weekly_sync_tables(conn)
        cursor = conn.cursor()
        placeholder = "%s"

        for user_cfg in users:
            app_user = (user_cfg.get("user") or "").strip()
            base_url = user_cfg.get("base_url", "")
            nav_user = user_cfg.get("user", "")
            nav_pass = user_cfg.get("pass", "")
            if not app_user or not base_url or not nav_pass:
                continue

            # For each source, find queued tracks that now have matches
            for source in ["LastFM", "ListenBrainz"]:
                cursor.execute(
                    f"""SELECT id, artist_name, track_name, release_name, recording_mbid,
                               release_mbid, queue_id, week_key
                        FROM weekly_playlist_tracks
                        WHERE username = {placeholder} AND source = {placeholder}
                          AND match_status IN ('missing', 'queued')""",
                    (app_user, source),
                )
                rows = cursor.fetchall() or []
                if not rows:
                    continue

                newly_matched = []
                for row in rows:
                    row_id = row.get("id") if isinstance(row, dict) else row[0]
                    track = {
                        "artist_name": (row.get("artist_name") if isinstance(row, dict) else row[1]) or "",
                        "track_name": (row.get("track_name") if isinstance(row, dict) else row[2]) or "",
                        "recording_mbid": (row.get("recording_mbid") if isinstance(row, dict) else row[4]) or "",
                    }
                    match = _match_track_in_library(conn, track)
                    if match:
                        cursor.execute(
                            f"""UPDATE weekly_playlist_tracks
                                SET match_status = 'matched', local_track_id = {placeholder},
                                    file_path = {placeholder}
                                WHERE id = {placeholder}""",
                            (match["track_id"], match["file_path"], row_id),
                        )
                        newly_matched.append({
                            "id": match["track_id"],
                            "artist": track["artist_name"],
                            "title": track["track_name"],
                        })

                if newly_matched:
                    # Get playlist name and existing playlist ID
                    playlist_name = PLAYLIST_NAME_TEMPLATE.format(username=app_user, source=source)
                    cursor.execute(
                        f"SELECT navidrome_playlist_id FROM weekly_sync_state WHERE username = {placeholder} AND source = {placeholder}",
                        (app_user, source),
                    )
                    prow = cursor.fetchone()
                    playlist_id = (prow.get("navidrome_playlist_id") if isinstance(prow, dict) else prow[0]) if prow else None

                    if playlist_id:
                        # Add newly matched tracks to existing playlist
                        for t in newly_matched:
                            try:
                                requests.post(
                                    f"{base_url}/rest/updatePlaylist.view",
                                    params={
                                        "u": nav_user,
                                        "p": nav_pass,
                                        "c": "popularr",
                                        "f": "json",
                                        "playlistId": playlist_id,
                                        "songIdToAdd": t["id"],
                                    },
                                    timeout=10,
                                )
                            except Exception as e:
                                logger.debug("[HOURLY_UPDATE] Failed to add track %s to playlist %s: %s", t["id"], playlist_id, e)
                        logger.info("[HOURLY_UPDATE] Added %s tracks to %s for %s", len(newly_matched), playlist_name, app_user)
                        total_updated += len(newly_matched)
                    else:
                        # Create new playlist if it doesn't exist yet
                        _create_or_update_navidrome_playlist(
                            base_url, nav_user, nav_pass, playlist_name, newly_matched, is_public=True
                        )
                        logger.info("[HOURLY_UPDATE] Created playlist %s with %s tracks for %s", playlist_name, len(newly_matched), app_user)
                        total_updated += len(newly_matched)

        conn.commit()
        return {"success": True, "updated": total_updated}
    except Exception as e:
        logger.error("[HOURLY_UPDATE] Error: %s", e, exc_info=True)
        try:
            conn.rollback()
        except Exception:
            pass
        return {"success": False, "error": str(e)}
    finally:
        conn.close()


def run_weekly_sync_for_all_users() -> Dict[str, Any]:
    """Run weekly sync for all configured navidrome users."""
    from helpers.config_helpers import get_config
    cfg = get_config()
    users = cfg.get("navidrome_users", []) or []
    if not users:
        return {"success": True, "message": "No users configured"}

    lastfm_cfg = cfg.get("api_integrations", {}).get("lastfm", {})
    lastfm_api_key = (lastfm_cfg.get("api_key") or "").strip()

    results = []
    for user_cfg in users:
        app_user = (user_cfg.get("user") or "").strip()
        base_url = user_cfg.get("base_url", "").strip()
        nav_user = user_cfg.get("user", "").strip()
        nav_pass = user_cfg.get("pass", "").strip()
        lastfm_username = (user_cfg.get("lastfm_username") or "").strip()
        lb_username = (user_cfg.get("listenbrainz_username") or app_user).strip()
        lb_token = (user_cfg.get("listenbrainz_user_token") or "").strip()

        if not app_user or not base_url or not nav_pass:
            continue

        result = run_weekly_sync_for_user(
            app_user=app_user,
            base_url=base_url,
            navidrome_user=nav_user,
            navidrome_pass=nav_pass,
            lastfm_username=lastfm_username if lastfm_api_key else None,
            lastfm_api_key=lastfm_api_key if lastfm_username else None,
            listenbrainz_username=lb_username if lb_token else None,
            listenbrainz_token=lb_token if lb_username else None,
        )
        results.append(result)

    success_count = sum(1 for r in results if r.get("success"))
    return {
        "success": success_count == len(results),
        "total_users": len(results),
        "successful": success_count,
        "results": results,
    }


def should_run_weekly_sync() -> bool:
    """Check if it's time to run the weekly sync (Monday 1 AM)."""
    now = datetime.now()
    return now.weekday() == WEEKLY_SYNC_WEEKDAY and now.hour == WEEKLY_SYNC_HOUR
