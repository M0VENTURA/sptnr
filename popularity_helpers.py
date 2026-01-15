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
from api_clients import timeout_safe_session

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
            http_session=timeout_safe_session,
            worker_threads=_worker_threads(cfg),
        )
    else:
        _spotify_client = None

    lastfm_cfg = api_cfg.get("lastfm") or {}
    if lastfm_client is not None:
        _lastfm_client = lastfm_client
    else:
        _lastfm_client = LastFmClient(lastfm_cfg.get("api_key", ""), http_session=timeout_safe_session)

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
    """
    Get Spotify artist ID with database caching.
    
    First checks the artist_stats table for a cached Spotify ID.
    If not found, looks up via Spotify API and stores in database.
    
    Args:
        artist_name: Name of the artist to lookup
        
    Returns:
        Spotify artist ID or None if not found
    """
    _ensure_clients_from_config()
    if not _spotify_enabled or _spotify_client is None:
        return None
    
    # Check database cache first
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT spotify_artist_id FROM artist_stats WHERE artist_name = ? AND spotify_artist_id IS NOT NULL",
            (artist_name,)
        )
        row = cursor.fetchone()
        conn.close()
        
        if row and row[0]:
            logging.debug(f"Found cached Spotify artist ID for '{artist_name}': {row[0]}")
            return row[0]
    except Exception as e:
        logging.debug(f"Database lookup failed for artist '{artist_name}': {e}")
        # Fall through to API lookup
    
    # Not in cache, lookup via Spotify API
    artist_id = _spotify_client.get_artist_id(artist_name)
    
    # Store in database cache if found
    if artist_id:
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute(
                """UPDATE artist_stats 
                   SET spotify_artist_id = ?, spotify_id_cached_at = ? 
                   WHERE artist_name = ?""",
                (artist_id, datetime.now().isoformat(), artist_name)
            )
            # If artist_stats doesn't have this artist yet, insert it
            if cursor.rowcount == 0:
                cursor.execute(
                    """INSERT OR IGNORE INTO artist_stats 
                       (artist_name, spotify_artist_id, spotify_id_cached_at) 
                       VALUES (?, ?, ?)""",
                    (artist_name, artist_id, datetime.now().isoformat())
                )
            conn.commit()
            conn.close()
            logging.debug(f"Cached Spotify artist ID for '{artist_name}': {artist_id}")
        except Exception as e:
            logging.debug(f"Failed to cache Spotify artist ID for '{artist_name}': {e}")
    
    return artist_id


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
