"""Popularity provider source wrappers.

This module performs provider data acquisition and light provider-specific
normalization. It should not decide star ratings or single status.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from api_clients.lastfm import LastFmClient
from api_clients.listenbrainz import (
    get_recording_popularity_batch as lb_get_recording_popularity_batch,
    get_listenbrainz_popularity as lb_get_listenbrainz_popularity,
    get_listenbrainz_score as lb_get_listenbrainz_score,
)
from services.popularity.popularity_matching import (
    choose_best_provider_counts,
    get_artist_lookup_candidates,
    normalize_for_aggregation,
)

logger = logging.getLogger(__name__)

DEFAULT_LISTENBRAINZ_BATCH_SIZE = 100
_lastfm_artist_catalog_cache: dict[str, list[dict]] = {}


def extract_recording_mbid(track: dict) -> Optional[str]:
    """Return recording MBID suitable for ListenBrainz popularity calls."""
    return track.get("recording_mbid") or track.get("musicbrainz_recording_mbid") or track.get("mbid")


def get_listenbrainz_batch_for_tracks(tracks: List[dict]) -> Dict[str, Dict[str, Optional[int]]]:
    """Fetch ListenBrainz popularity in chunks for track dicts."""
    mbids = [extract_recording_mbid(track) for track in tracks]
    mbids = [mbid for mbid in mbids if mbid]
    output: Dict[str, Dict[str, Optional[int]]] = {}
    for index in range(0, len(mbids), DEFAULT_LISTENBRAINZ_BATCH_SIZE):
        chunk = mbids[index:index + DEFAULT_LISTENBRAINZ_BATCH_SIZE]
        try:
            output.update(lb_get_recording_popularity_batch(chunk))
        except Exception as exc:
            logger.debug("ListenBrainz batch lookup failed: %s", exc)
    return output


def get_listenbrainz_popularity_for_track(track: dict) -> Dict[str, Optional[int]]:
    """Fetch ListenBrainz popularity for one track dict."""
    mbid = extract_recording_mbid(track)
    if not mbid:
        return {"total_listen_count": None, "total_user_count": None}
    try:
        return lb_get_listenbrainz_popularity(mbid)
    except Exception:
        return {"total_listen_count": None, "total_user_count": None}


def get_listenbrainz_score_for_track(track: dict) -> int:
    """Backward-compatible per-track ListenBrainz raw listen count."""
    mbid = extract_recording_mbid(track)
    if not mbid:
        return 0
    try:
        return int(lb_get_listenbrainz_score(mbid) or 0)
    except Exception:
        return 0


def get_lastfm_track_info(
    artist: str,
    title: str,
    track_mbid: str | None = None,
    album_artist: str | None = None,
    lastfm_client: LastFmClient | None = None,
) -> dict:
    """Fetch Last.fm track info using conservative artist candidates."""
    if lastfm_client is None:
        return {"track_play": 0, "listeners": 0}
    results = []
    for candidate in get_artist_lookup_candidates(artist, album_artist=album_artist):
        try:
            results.append(lastfm_client.get_track_info(candidate, title, track_mbid=track_mbid))
        except Exception:
            continue
    return choose_best_provider_counts(results) if results else {"track_play": 0, "listeners": 0}


import logging
logger = logging.getLogger(__name__)

# Module-level cache: artist_key (casefold) → max listener count from top tracks.
_lastfm_artist_max_cache: dict[str, int] = {}


def get_lastfm_artist_max_listeners(
    artist: str,
    api_key: str | None = None,
) -> int:
    """Fetch the peak listener count across an artist's top tracks on Last.fm.

    Results are cached in-memory so repeated calls for the same artist
    (across multiple albums) cost at most one API request.

    Returns 0 if Last.fm is not configured or the artist has no data.
    """
    if not api_key:
        from helpers.config_helpers import get_config
        cfg = get_config()
        api_key = cfg.get("api_integrations", {}).get("lastfm", {}).get("api_key", "")
    if not api_key:
        return 0

    artist_key = artist.casefold().strip()
    cached = _lastfm_artist_max_cache.get(artist_key)
    if cached is not None:
        return cached

    from api_clients.lastfm_http import LastFmHttpClient
    client = LastFmHttpClient(api_key=api_key)

    try:
        data = client.get_json(
            "artist.getTopTracks",
            timeout=(5, 10),
            artist=artist,
            limit=100,
        )
        if not data or "error" in data:
            _lastfm_artist_max_cache[artist_key] = 0
            return 0

        tracks = data.get("toptracks", {}).get("track", [])
        if isinstance(tracks, dict):
            tracks = [tracks]

        max_listeners = 0
        for track in tracks:
            if isinstance(track, dict):
                try:
                    listeners = int(track.get("listeners", 0) or 0)
                    if listeners > max_listeners:
                        max_listeners = listeners
                except (ValueError, TypeError):
                    continue

        _lastfm_artist_max_cache[artist_key] = max_listeners
        return max_listeners
    except Exception as exc:
        logger.debug("Failed to get Last.fm top tracks for '%s': %s", artist, exc)
        _lastfm_artist_max_cache[artist_key] = 0
        return 0


def get_aggregated_lastfm_popularity(artist: str, track_title: str, lastfm_client=None) -> dict:
    """Aggregate Last.fm counts across locally matched title variants."""
    if lastfm_client is None:
        return {"listeners": 0, "track_play": 0, "matched_tracks": []}
    artist_key = artist.casefold().strip()
    try:
        if artist_key not in _lastfm_artist_catalog_cache and hasattr(lastfm_client, "get_artist_top_tracks"):
            _lastfm_artist_catalog_cache[artist_key] = lastfm_client.get_artist_top_tracks(artist)
        catalog = _lastfm_artist_catalog_cache.get(artist_key, [])
    except Exception:
        catalog = []

    target = normalize_for_aggregation(track_title)
    matched = []
    listeners = 0
    playcount = 0
    for item in catalog or []:
        item_title = item.get("name") or item.get("title") or ""
        if normalize_for_aggregation(item_title) == target:
            matched.append(item)
            listeners += int(item.get("listeners", 0) or 0)
            playcount += int(item.get("playcount", item.get("track_play", 0)) or 0)
    if matched:
        return {"listeners": listeners, "track_play": playcount, "matched_tracks": matched}
    try:
        info = lastfm_client.get_track_info(artist, track_title)
        return {"listeners": int(info.get("listeners", 0) or 0), "track_play": int(info.get("track_play", 0) or 0), "matched_tracks": []}
    except Exception:
        return {"listeners": 0, "track_play": 0, "matched_tracks": []}


def get_aggregated_listenbrainz_popularity(
    title: str,
    artist: str,
    primary_mbid: Optional[str] = None,
    lb_client=None,
    mb_client=None,
) -> dict:
    """Aggregate ListenBrainz stats across split MBIDs when possible."""
    logger.debug("[POPULARITY_SOURCES] Fetching aggregated ListenBrainz popularity")
    mbids: set[str] = set()
    if primary_mbid:
        mbids.add(primary_mbid)
    if mb_client and hasattr(mb_client, "search_recordings_by_artist"):
        try:
            for rec in mb_client.search_recordings_by_artist(title, artist):
                if rec.get("id"):
                    mbids.add(rec["id"])
        except Exception:
            pass
    if not mbids:
        return {"total_listen_count": 0, "total_user_count": 0, "mbids": []}
    try:
        batch = lb_get_recording_popularity_batch(list(mbids))
        listen_count = sum(int((batch.get(mbid) or {}).get("total_listen_count") or 0) for mbid in mbids)
        user_count = sum(int((batch.get(mbid) or {}).get("total_user_count") or 0) for mbid in mbids)
        return {"total_listen_count": listen_count, "total_user_count": user_count, "mbids": sorted(mbids)}
    except Exception:
        return {"total_listen_count": 0, "total_user_count": 0, "mbids": sorted(mbids)}


def get_metadata_sources_info(single_sources: list[str]) -> dict:
    """Extract metadata-source flags from a list of source identifiers.

    Args:
        single_sources: List of source names (e.g. ``["discogs", "spotify"]``).

    Returns:
        Dict with boolean flags for each source and a display-ready source list.
    """
    has_discogs = "discogs" in single_sources or "discogs_video" in single_sources
    has_spotify = "spotify" in single_sources
    has_musicbrainz = "musicbrainz" in single_sources
    has_lastfm = "lastfm" in single_sources
    has_version_count = "version_count" in single_sources

    has_metadata = has_discogs or has_spotify or has_musicbrainz or has_lastfm

    sources_list: list[str] = []
    if has_discogs:
        sources_list.append("Discogs")
    if has_spotify:
        sources_list.append("Spotify")
    if has_musicbrainz:
        sources_list.append("MusicBrainz")
    if has_lastfm:
        sources_list.append("Last.fm")
    if has_version_count:
        sources_list.append("Version Count")

    return {
        "has_discogs": has_discogs,
        "has_spotify": has_spotify,
        "has_musicbrainz": has_musicbrainz,
        "has_lastfm": has_lastfm,
        "has_version_count": has_version_count,
        "has_metadata": has_metadata,
        "sources_list": sources_list,
    }
