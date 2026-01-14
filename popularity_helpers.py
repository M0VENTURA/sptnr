#!/usr/bin/env python3
"""
Shared popularity helpers for Spotify/Last.fm/ListenBrainz lookups and weights.
Functions are used by both the main scanner (start.py) and popularity.py.
"""

import os
import yaml
from typing import Any, Tuple

from api_clients.spotify import SpotifyClient
from api_clients.lastfm import LastFmClient
from api_clients.audiodb_and_listenbrainz import ListenBrainzClient, score_by_age as _score_by_age

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/config.yaml")

_DEFAULT_WEIGHTS = {
    "spotify": 0.4,
    "lastfm": 0.3,
    "listenbrainz": 0.2,
    "age": 0.1,
}

_DEFAULT_FEATURES = {
    "scan_worker_threads": 4,
}

_spotify_client: SpotifyClient | None = None
_lastfm_client: LastFmClient | None = None
_listenbrainz_client: ListenBrainzClient | None = None

_spotify_enabled = True
_listenbrainz_enabled = True
_clients_configured = False


def _load_config() -> dict:
    config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")
    if not os.path.exists(config_path):
        return {}
    try:
        with open(config_path, "r") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _resolve_weights(cfg: dict) -> Tuple[float, float, float, float]:
    weights = cfg.get("weights") if isinstance(cfg, dict) else None
    weights = weights or {}
    return (
        float(weights.get("spotify", _DEFAULT_WEIGHTS["spotify"])),
        float(weights.get("lastfm", _DEFAULT_WEIGHTS["lastfm"])),
        float(weights.get("listenbrainz", _DEFAULT_WEIGHTS["listenbrainz"])),
        float(weights.get("age", _DEFAULT_WEIGHTS["age"])),
    )


SPOTIFY_WEIGHT, LASTFM_WEIGHT, LISTENBRAINZ_WEIGHT, AGE_WEIGHT = _resolve_weights(_load_config())


def _worker_threads(cfg: dict) -> int:
    features = cfg.get("features") if isinstance(cfg, dict) else None
    features = features or {}
    try:
        return int(features.get("scan_worker_threads", _DEFAULT_FEATURES["scan_worker_threads"]))
    except Exception:
        return _DEFAULT_FEATURES["scan_worker_threads"]


def configure_popularity_helpers(
    *,
    spotify_client: SpotifyClient | None = None,
    lastfm_client: LastFmClient | None = None,
    listenbrainz_client: ListenBrainzClient | None = None,
    config: dict | None = None,
) -> None:
    """Configure shared clients and refresh weights based on provided config."""
    global _spotify_client, _lastfm_client, _listenbrainz_client
    global _spotify_enabled, _listenbrainz_enabled, _clients_configured
    global SPOTIFY_WEIGHT, LASTFM_WEIGHT, LISTENBRAINZ_WEIGHT, AGE_WEIGHT

    cfg = config if config is not None else _load_config()

    # Refresh weights from config
    SPOTIFY_WEIGHT, LASTFM_WEIGHT, LISTENBRAINZ_WEIGHT, AGE_WEIGHT = _resolve_weights(cfg)

    api_cfg = cfg.get("api_integrations") if isinstance(cfg, dict) else None
    api_cfg = api_cfg or {}

    spotify_cfg = api_cfg.get("spotify") or {}
    _spotify_enabled = bool(spotify_cfg.get("enabled", True))
    if spotify_client is not None:
        _spotify_client = spotify_client
    elif _spotify_enabled:
        _spotify_client = SpotifyClient(
            spotify_cfg.get("client_id", ""),
            spotify_cfg.get("client_secret", ""),
            worker_threads=_worker_threads(cfg),
        )
    else:
        _spotify_client = None

    lastfm_cfg = api_cfg.get("lastfm") or {}
    if lastfm_client is not None:
        _lastfm_client = lastfm_client
    else:
        _lastfm_client = LastFmClient(lastfm_cfg.get("api_key", ""))

    listenbrainz_cfg = api_cfg.get("listenbrainz") or {}
    _listenbrainz_enabled = bool(listenbrainz_cfg.get("enabled", True))
    if listenbrainz_client is not None:
        _listenbrainz_client = listenbrainz_client
    else:
        _listenbrainz_client = ListenBrainzClient(enabled=_listenbrainz_enabled)

    _clients_configured = True


def _ensure_clients_from_config() -> None:
    if not _clients_configured:
        configure_popularity_helpers()


def get_spotify_artist_id(artist_name: str) -> str | None:
    _ensure_clients_from_config()
    if not _spotify_enabled or _spotify_client is None:
        return None
    return _spotify_client.get_artist_id(artist_name)


def get_spotify_artist_single_track_ids(artist_id: str) -> set[str]:
    _ensure_clients_from_config()
    if not _spotify_enabled or _spotify_client is None:
        return set()
    return _spotify_client.get_artist_singles(artist_id) or set()


def search_spotify_track(title: str, artist: str, album: str | None = None):
    _ensure_clients_from_config()
    if not _spotify_enabled or _spotify_client is None:
        return []
    return _spotify_client.search_track(title, artist, album)


def get_lastfm_track_info(artist: str, title: str) -> dict:
    _ensure_clients_from_config()
    if _lastfm_client is None:
        return {"track_play": 0}
    return _lastfm_client.get_track_info(artist, title)


def get_listenbrainz_score(mbid: str, artist: str = "", title: str = "") -> int:
    _ensure_clients_from_config()
    if not _listenbrainz_enabled or _listenbrainz_client is None:
        return 0
    return _listenbrainz_client.get_listen_count(mbid, artist, title)


def score_by_age(playcount: Any, release_str: str):
    return _score_by_age(playcount, release_str)



# --- Shared DB/API/Helper Functions (moved from start.py) ---
import math
import logging
import json
import time
from datetime import datetime
from collections import defaultdict
from db_utils import get_db_connection

def fetch_artist_albums(artist_id):
    """Fetch albums for an artist (wrapper using NavidromeClient)."""
    from start import nav_client
    return nav_client.fetch_artist_albums(artist_id)

def fetch_album_tracks(album_id):
    """
    Fetch all tracks for an album using Subsonic API (wrapper using NavidromeClient).
    :param album_id: Album ID in Navidrome
    :return: List of track objects
    """
    from start import nav_client
    return nav_client.fetch_album_tracks(album_id)

def save_to_db(track_data):
    """Save or update a track in the database."""
    conn = get_db_connection()
    cursor = conn.cursor()
    columns = ', '.join(track_data.keys())
    placeholders = ', '.join(['?'] * len(track_data))
    update_clause = ', '.join([f"{k}=excluded.{k}" for k in track_data.keys()])
    sql = f"INSERT INTO tracks ({columns}) VALUES ({placeholders}) ON CONFLICT(id) DO UPDATE SET {update_clause}"
    cursor.execute(sql, list(track_data.values()))
    conn.commit()
    conn.close()

def build_artist_index(verbose: bool = False):
    """Build artist index from Navidrome (wrapper using NavidromeClient)."""
    from start import nav_client
    artist_map_from_api = nav_client.build_artist_index()
    max_retries = 3
    for attempt in range(max_retries):
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            for artist_name, info in artist_map_from_api.items():
                artist_id = info.get("id")
                cursor.execute("""
                    INSERT OR REPLACE INTO artist_stats (artist_id, artist_name, album_count, track_count, last_updated)
                    VALUES (?, ?, ?, ?, ?)
                """, (artist_id, artist_name, 0, 0, None))
                if verbose:
                    print(f"   📝 Added artist to index: {artist_name} (ID: {artist_id})")
                    logging.info(f"Added artist to index: {artist_name} (ID: {artist_id})")
            conn.commit()
            conn.close()
            break
        except Exception as e:
            if "locked" in str(e) and attempt < max_retries - 1:
                logging.debug(f"Database locked during artist index build, retrying ({attempt + 1}/{max_retries})...")
                time.sleep(1.0 * (attempt + 1))
                continue
            else:
                logging.error(f"Failed to build artist index after {max_retries} attempts: {e}")
                raise
    logging.info(f"✅ Cached {len(artist_map_from_api)} artists in DB")
    print(f"✅ Cached {len(artist_map_from_api)} artists in DB")
    return artist_map_from_api

def load_artist_map():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT artist_id, artist_name, album_count, track_count, last_updated FROM artist_stats")
    rows = cursor.fetchall()
    conn.close()
    return {row[1]: {"id": row[0], "album_count": row[2], "track_count": row[3], "last_updated": row[4]} for row in rows}

def get_album_last_scanned_from_db(artist_name: str, album_name: str) -> str | None:
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT MAX(last_scanned) FROM tracks WHERE artist = ? AND album = ?",
            (artist_name, album_name),
        )
        row = cursor.fetchone()
        conn.close()
        return (row[0] if row and row[0] else None)
    except Exception as e:
        logging.debug(f"get_album_last_scanned_from_db failed for '{artist_name} / {album_name}': {e}")
        return None

def get_album_track_count_in_db(artist_name: str, album_name: str) -> int:
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM tracks WHERE artist = ? AND album = ?",
            (artist_name, album_name),
        )
        count = cursor.fetchone()[0] or 0
        conn.close()
        return count
    except Exception as e:
        logging.debug(f"get_album_track_count_in_db failed for '{artist_name} / {album_name}': {e}")
        return 0

def rate_artist(artist_id, artist_name, verbose=False, force=False):
    """
    Rate all tracks for a given artist and build a single smart "Essential {artist}" playlist.
    """
    import os
    import logging
    from datetime import datetime, timedelta
    from statistics import median
    from concurrent.futures import ThreadPoolExecutor
    from popularity_helpers import (
        get_spotify_artist_id,
        get_spotify_artist_single_track_ids,
        search_spotify_track,
        get_lastfm_track_info,
        get_listenbrainz_score,
        score_by_age,
        SPOTIFY_WEIGHT,
        LASTFM_WEIGHT,
        LISTENBRAINZ_WEIGHT,
        AGE_WEIGHT,
        fetch_artist_albums,
        fetch_album_tracks,
        save_to_db,
        get_album_track_count_in_db,
    )
    from popularity_helpers import compute_adaptive_weights
    from popularity_helpers import _base_title, _has_subtitle_variant, _similar
    from popularity_helpers import is_valid_version, create_or_update_playlist_for_artist, enrich_genres_aggressively
    from sptnr import parse_datetime_flexible
    from db_utils import get_db_connection
    from scan_history import log_album_scan
    from singledetection import infer_album_context

    if not _clients_configured:
        configure_popularity_helpers()

    if not _spotify_enabled or _spotify_client is None:
        return

    if not _listenbrainz_enabled or _listenbrainz_client is None:
        return

    if not artist_id or not artist_name:
        return

    if not force:
        # Check if the artist already has a playlist
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT playlist_id FROM playlists WHERE artist = ? AND name = ?", (artist_id, artist_name))
        result = cursor.fetchone()
        conn.close()
        if result:
            print(f"Playlist for {artist_name} already exists.")
            return

    # Fetch all albums for the artist
    albums = fetch_artist_albums(artist_id)
    if not albums:
        print(f"No albums found for {artist_name}")
        return

    # Rate each album
    for album in albums:
        album_id = album["id"]
        album_name = album["name"]
        album_release_date = album["release_date"]
        album_tracks = fetch_album_tracks(album_id)
        if not album_tracks:
            print(f"No tracks found for {album_name}")
            continue

        # Rate each track
        for track in album_tracks:
            track_id = track["id"]
            track_name = track["name"]
            track_release_date = track["release_date"]
            track_playcount = track["playcount"]
            track_score = score_by_age(track_playcount, track_release_date)
            track_weight = SPOTIFY_WEIGHT * track_score
            track_weight += LASTFM_WEIGHT * get_lastfm_track_info(artist_name, track_name)
            track_weight += LISTENBRAINZ_WEIGHT * get_listenbrainz_score(track_id, artist_name, track_name)
            track_weight += AGE_WEIGHT * (time.time() - track_release_date)
            track_weight = track_weight / 4

            # Save the track to the database
            track_data = {
                "track_id": track_id,
                "track_name": track_name,
                "album_id": album_id,
                "album_name": album_name,
                "release_date": track_release_date,
                "playcount": track_playcount,
                "score": track_score,
                "weight": track_weight,
            }
            save_to_db(track_data)

    print(f"Finished rating artist {artist_name}")
    return

__all__ = [
    "configure_popularity_helpers",
    "get_spotify_artist_id",
    "get_spotify_artist_single_track_ids",
    "search_spotify_track",
    "get_lastfm_track_info",
    "get_listenbrainz_score",
    "score_by_age",
    "SPOTIFY_WEIGHT",
    "LASTFM_WEIGHT",
    "LISTENBRAINZ_WEIGHT",
    "AGE_WEIGHT",
    "fetch_artist_albums",
    "fetch_album_tracks",
    "save_to_db",
    "build_artist_index",
    "load_artist_map",
    "get_album_last_scanned_from_db",
    "get_album_track_count_in_db",
]
