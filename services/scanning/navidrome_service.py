"""Navidrome scanning service helpers.

Coordinates multiple Navidrome API calls for scan workflow operations.
Intentionally separate from ``api_clients.navidrome`` (raw HTTP layer).

Key Functions:
    - fetch_all_tracks_concurrently(): Parallel fetching of all tracks
      from Navidrome using ThreadPoolExecutor.
    - Other scan coordination helpers for batch processing.

Architecture:
    These are scan workflow helpers, not HTTP endpoint wrappers. They
    compose multiple API calls and handle coordination concerns like
    pagination, concurrency, and error handling.
"""

from __future__ import annotations

import concurrent.futures
import logging
import time
from typing import Any

from api_clients.navidrome import NavidromeClient

logger = logging.getLogger(__name__)


from helpers.config_helpers import get_scan_pipeline_config
_scan_cfg = get_scan_pipeline_config()


def fetch_all_tracks_concurrently(
    client: NavidromeClient,
    total_tracks: int,
    page_size: int | None = None,
    max_workers: int | None = None,
) -> list[dict[str, Any]]:
    if page_size is None:
        page_size = _scan_cfg["page_size"]
    if max_workers is None:
        max_workers = _scan_cfg["max_workers"]
    """Fetch all songs from Navidrome concurrently when get_songs exists.

    This function is defensive because not every client implementation has a
    ``get_songs`` method. When unavailable, it logs and returns an empty list.
    """
    if not hasattr(client, "get_songs"):
        logger.warning("NavidromeClient.get_songs is not available; concurrent fetch skipped")
        return []

    offsets = range(0, int(total_tracks or 0), int(page_size or 500))
    all_tracks: list[dict[str, Any]] = []

    def fetch_page(offset: int):
        return client.get_songs(offset=offset, size=page_size)  # type: ignore[attr-defined]

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_offset = {executor.submit(fetch_page, offset): offset for offset in offsets}

        for future in concurrent.futures.as_completed(future_to_offset):
            offset = future_to_offset[future]
            try:
                page_data = future.result()
                if page_data:
                    all_tracks.extend(page_data)
            except Exception as exc:
                logger.warning("Failed to fetch Navidrome page at offset %s: %s", offset, exc)

    return all_tracks


def build_artist_index_from_albums(client: NavidromeClient, page_size: int = 500) -> dict[str, dict[str, Any]]:
    """Build a scan-oriented artist index from the album list.

    Album-derived artists are more relevant for import workflows than the raw
    getArtists tree because the scanner imports albums and tracks.
    """
    albums = client.get_albums(artist_id=None, page_size=page_size)
    if not albums:
        return {}

    artist_map: dict[str, dict[str, Any]] = {}

    for album in albums:
        artist_name = str(album.get("artist") or "").strip()
        artist_id = str(album.get("artistId") or "").strip()

        if not artist_name or not artist_id:
            continue

        if artist_name not in artist_map:
            artist_map[artist_name] = {
                "id": artist_id,
                "album_count": 0,
                "track_count": 0,
                "last_updated": None,
            }

        artist_map[artist_name]["album_count"] += 1
        artist_map[artist_name]["track_count"] += int(album.get("songCount", 0) or 0)

    logger.info("Built album-derived Navidrome index for %s artists", len(artist_map))
    return artist_map


def build_artist_index(client: NavidromeClient) -> dict[str, dict[str, Any]]:
    """Build artist index using album list first, then getArtists fallback."""
    artist_map = build_artist_index_from_albums(client, page_size=500)
    if artist_map:
        return artist_map

    fallback: dict[str, dict[str, Any]] = {}

    for artist in client.get_artists():
        artist_id = artist.get("id")
        artist_name = artist.get("name")

        if artist_id and artist_name:
            fallback[str(artist_name)] = {
                "id": artist_id,
                "album_count": int(artist.get("albumCount", 0) or 0),
                "track_count": 0,
                "last_updated": None,
            }

    logger.info("Built fallback Navidrome index for %s artists", len(fallback))
    return fallback


def get_library_stats(client: NavidromeClient, cache_seconds: int = 3600) -> dict[str, int]:
    """Return cached library stats derived from the scan-oriented artist index."""
    now = time.time()

    if client._stats_cache and now - client._last_stats_time < cache_seconds:
        return client._stats_cache  # type: ignore[return-value]

    try:
        artist_map = build_artist_index(client)
        total_albums = sum(int(info.get("album_count", 0) or 0) for info in artist_map.values())
        total_tracks = sum(int(info.get("track_count", 0) or 0) for info in artist_map.values())

        client._stats_cache = {
            "total_albums": total_albums,
            "total_tracks": total_tracks,
            "total_songs": total_tracks,
        }
        client._last_stats_time = now
        return client._stats_cache  # type: ignore[return-value]

    except Exception as exc:
        logger.error("Failed to get Navidrome library stats: %s", exc, exc_info=True)
        return {"total_albums": 0, "total_tracks": 0, "total_songs": 0}
