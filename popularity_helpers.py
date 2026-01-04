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
    if not os.path.exists(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH, "r") as f:
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
]
