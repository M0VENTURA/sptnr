"""Comprehensive Spotify metadata enrichment facade.

Provides a unified entry point for enriching track metadata by
coordinating Spotify and Last.fm API clients. Handles client
configuration, lazy initialization, and fallback logic.

Key Functions:
    - configure_metadata_clients(): Set custom API clients.
    - get_spotify_client() / get_lastfm_client(): Lazy client initialization.
    - fetch_comprehensive_metadata(): Full metadata enrichment pipeline.

Architecture:
    Acts as a facade over ``SpotifyService`` and ``LastFmClient``.
    Clients are lazily initialised from config.yaml on first use.
    Callers in the enrichment pipeline call ``fetch_comprehensive_metadata()``
    rather than managing individual API clients directly.
"""
from __future__ import annotations
from services.enrichment.spotify_service import SpotifyService

_spotify_client = None
_lastfm_client = None


def configure_metadata_clients(spotify_client=None, lastfm_client=None):
    global _spotify_client, _lastfm_client
    if spotify_client is not None:
        _spotify_client = spotify_client
    if lastfm_client is not None:
        _lastfm_client = lastfm_client


def get_spotify_client():
    global _spotify_client
    if _spotify_client is not None:
        return _spotify_client
    try:
        from helpers.config_helpers import get_config
        cfg = get_config() or {}
        sp = cfg.get("api_integrations", {}).get("spotify", {}) or cfg.get("spotify", {})
        if sp.get("client_id") and sp.get("client_secret"):
            _spotify_client = SpotifyService(sp["client_id"], sp["client_secret"])
    except Exception:
        pass
    return _spotify_client


def get_lastfm_client():
    global _lastfm_client
    if _lastfm_client is not None:
        return _lastfm_client
    try:
        from api_clients.lastfm import LastFmClient
        from helpers.config_helpers import get_config
        cfg = get_config() or {}
        lf = cfg.get("api_integrations", {}).get("lastfm") or cfg.get("api_integrations", {}).get("last_fm") or cfg.get("lastfm", {})
        if lf.get("api_key"):
            _lastfm_client = LastFmClient(api_key=lf["api_key"])
    except Exception:
        pass
    return _lastfm_client


def fetch_comprehensive_metadata(db_track_id: str, spotify_track_id: str, force_refresh: bool = False) -> bool:
    client = get_spotify_client()
    if client is None or not spotify_track_id:
        return False
    try:
        metadata = client.get_track_metadata(spotify_track_id)
        audio = client.get_audio_features(spotify_track_id)
        if not metadata:
            return False
        from db.utils import get_db_connection
        conn = get_db_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE tracks
                SET spotify_metadata = %s,
                    spotify_audio_features = %s,
                    last_spotify_lookup = NOW()
                WHERE id = %s
                """,
                (metadata, audio, db_track_id),
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception:
        return False

