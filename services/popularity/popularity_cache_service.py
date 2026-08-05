"""Bulk popularity cache service.

Fetches Last.fm and ListenBrainz popularity for ALL tracks of an artist in a
small number of API calls (``artist.getTopTracks`` + batched ListenBrainz
lookups) and persists them into ``track_popularity_cache``.

Non-forced scans read from this cache instead of making per-track API calls,
which avoids rate limiting and keeps scores consistent between runs.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from db.repositories import popularity_cache as cache_repo
from services.popularity.popularity_matching import normalize_for_aggregation
from services.popularity.popularity_sources import (
    extract_recording_mbid,
    get_listenbrainz_batch_for_tracks,
)

logger = logging.getLogger(__name__)

CACHE_FRESH_HOURS = 24 * 7  # 1 week


def _is_fresh(row: Dict[str, Any]) -> bool:
    """Return True when a cached row is fresh enough to reuse."""
    try:
        updated = row.get("updated_at")
        if not updated:
            return False
        # Postgres returns naive datetimes for TIMESTAMP columns.
        if isinstance(updated, datetime):
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            else:
                updated = updated.astimezone(timezone.utc)
        else:
            return False
        age = (datetime.now(timezone.utc) - updated).total_seconds()
        return age < CACHE_FRESH_HOURS * 3600
    except Exception:
        return False


def _row_to_entry(row: Dict[str, Any]) -> Dict[str, Any]:
    """Convert a cache row into an entry dict, or {} when all-zero."""
    entry = {
        "lastfm_listeners": int(row.get("lastfm_listeners") or 0),
        "lastfm_playcount": int(row.get("lastfm_playcount") or 0),
        "listenbrainz_listens": int(row.get("listenbrainz_listens") or 0),
        "listenbrainz_users": int(row.get("listenbrainz_users") or 0),
    }
    if not any(entry.values()):
        return {}
    return entry


# One artist.getTopTracks call per artist per process — albums of the same
# artist share the result instead of repeating the API call.
_lf_top_tracks_cache: Dict[str, Dict[str, Dict[str, int]]] = {}

# Original (cased) track titles for the same map — used when persisting the
# full catalogue so rows keep the artist's real title casing, not a lowercased
# cache key.
_lf_top_tracks_titles: Dict[str, Dict[str, str]] = {}


def _get_lastfm_client() -> Any:
    """Build a LastFmClient from config, or None when no API key is set."""
    try:
        from helpers.config_helpers import get_config
        cfg = get_config().get("api_integrations", {}).get("lastfm", {})
        api_key = cfg.get("api_key", "")
        if api_key:
            from api_clients.lastfm import LastFmClient
            return LastFmClient(api_key)
    except Exception as exc:
        logger.debug("[popularity_cache] LastFmClient build failed: %s", exc)
    return None


def _lf_top_tracks_map(lastfm_client: Any, artist: str) -> Dict[str, Dict[str, int]]:
    """One ``artist.getTopTracks`` call -> {normalized title: listeners/playcount}.

    Versions of the same song that Last.fm lists separately — "Herzblut" vs
    "Herzblut (feat. Melissa Bonny)" — collapse onto the SAME normalised key
    and their counts are SUMMED, so a track whose local title matches only the
    plain title still picks up the high-listen feat. single.  The title kept
    for display is the version with the most listeners.
    """
    if artist in _lf_top_tracks_cache:
        return _lf_top_tracks_cache[artist]
    try:
        top_tracks = lastfm_client.get_artist_top_tracks(artist, limit=200) or []
    except Exception as exc:
        logger.debug("[popularity_cache] artist.getTopTracks failed for %s: %s", artist, exc)
        return {}
    out: Dict[str, Dict[str, int]] = {}
    titles: Dict[str, str] = {}
    acc: Dict[str, Dict[str, int]] = {}
    best: Dict[str, tuple[int, str]] = {}
    for track in top_tracks:
        if not isinstance(track, dict):
            continue
        name = track.get("name")
        if not name:
            continue
        key = _norm(name)
        if not key:
            continue
        listeners = int(track.get("listeners") or 0)
        playcount = int(track.get("playcount") or 0)
        entry = acc.setdefault(key, {"lastfm_listeners": 0, "lastfm_playcount": 0})
        entry["lastfm_listeners"] += listeners
        entry["lastfm_playcount"] += playcount
        if key not in best or listeners > best[key][0]:
            best[key] = (listeners, name)
    for key, counts in acc.items():
        out[key] = dict(counts)
        titles[key] = best.get(key, (0, key))[1]
    _lf_top_tracks_cache[artist] = out
    _lf_top_tracks_titles[artist] = titles
    return out


def _norm(title: str) -> str:
    return normalize_for_aggregation(title or "")


def prefetch_artist_popularity(
    artist: str,
    tracks: List[Dict[str, Any]],
    lastfm_client: Any = None,
    lb_data: Optional[Dict[str, Dict[str, Optional[int]]]] = None,
    force: bool = False,
    cache_full_catalogue: bool = False,
) -> Dict[str, Dict[str, Any]]:
    """Populate the cache for an artist's tracks and return fresh entries.

    Steps:
      1. Quick check: reuse fresh rows already in ``track_popularity_cache``
         and return immediately when every requested title is covered —
         subsequent scans make ZERO API calls.
      2. One ``artist.getTopTracks`` call fills Last.fm data, but only for
         titles the cache lacks.
      3. One ListenBrainz batch call (keyed by recording MBID) fills LB data,
         but only for titles the cache lacks.
      4. Only rows whose counts CHANGED (or are new) are written back —
         unchanged rows keep their ``updated_at`` so they stay fresh.

    When ``cache_full_catalogue`` is True and the artist has NO cached rows
    at all, the entire ``artist.getTopTracks`` result is persisted (one call,
    up to 200 titles) so future album scans of the artist never need per-track
    Last.fm lookups.

    Args:
        artist: artist name.
        tracks: ``[{"title": ..., "recording_mbid": ...}]`` — all tracks to cache.
        lastfm_client: client exposing ``get_artist_top_tracks``; built from
            config when omitted.
        lb_data: optional precomputed ListenBrainz batch keyed by recording MBID
            (avoids a duplicate batch call when the caller already fetched it).
        force: when True, ignore cached values and refetch from APIs.
        cache_full_catalogue: when True and the artist has no cached rows,
            persist the full top-tracks list (quick, single bulk call).

    Returns ``{normalised_title: {lastfm_listeners, ...}}`` with only non-empty
    entries — callers merge into their own track loop.
    """
    if not artist or not tracks:
        return {}

    titles = [t.get("title") for t in tracks if t.get("title")]
    if not titles:
        return {}

    if lastfm_client is None:
        lastfm_client = _get_lastfm_client()

    # Existing rows are always read — they seed the result AND provide the
    # baseline for the changed-only upsert below.  Rows are re-keyed by their
    # NORMALISED title so versions of the same song ("Herzblut" vs
    # "Herzblut (feat. X)") collapse onto one entry.
    cached = cache_repo.get_cached_popularity_for_titles(artist, titles)
    cached_by_norm: Dict[str, Dict[str, Any]] = {}
    for _row in cached.values():
        _key = _norm(str(_row.get("title") or ""))
        if not _key:
            continue
        _existing = cached_by_norm.get(_key)
        if _existing is None:
            cached_by_norm[_key] = _row
        elif int(_row.get("lastfm_listeners") or 0) > int(_existing.get("lastfm_listeners") or 0):
            cached_by_norm[_key] = _row
    cached = cached_by_norm

    entries: Dict[str, Dict[str, Any]] = {}

    # 1. Quick check: seed from fresh cached values (skipped when forced).
    if not force:
        for title, row in cached.items():
            entry = _row_to_entry(row)
            if entry and _is_fresh(row):
                entries[title] = entry

    def _missing(field: str) -> bool:
        return any(
            not (entries.get(_norm(t.get("title"))) or {}).get(field)
            for t in tracks
            if t.get("title")
        )

    # 2. Last.fm: one bulk call, only when some title lacks LF data.
    if lastfm_client is not None and _missing("lastfm_listeners"):
        lf_map = _lf_top_tracks_map(lastfm_client, artist)
        for track in tracks:
            title = track.get("title")
            if not title:
                continue
            key = _norm(title)
            if key not in entries and key in lf_map:
                entries[key] = dict(lf_map[key])

        # Full-catalogue fast-path: artist has no cached data yet — persist
        # the ENTIRE top-tracks result from the single call already made,
        # so later scans of any album by this artist hit the cache instead
        # of doing per-track Last.fm lookups.
        if cache_full_catalogue and not force and not cached:
            title_by_key_lf = _lf_top_tracks_titles.get(artist) or {}
            for key, counts in lf_map.items():
                if key in entries:
                    continue
                entries[key] = dict(counts)
            logger.info(
                "[popularity_cache] Cached full catalogue for '%s' (%d titles, no prior data)",
                artist, len(lf_map),
            )

    # 3. ListenBrainz: batch fill, only for titles lacking LB data.
    lb_needed = [
        track for track in tracks
        if track.get("title") and not (entries.get(_norm(track["title"])) or {}).get("listenbrainz_listens")
    ]
    if lb_needed:
        batch = lb_data if lb_data is not None else get_listenbrainz_batch_for_tracks(lb_needed)
        for track in lb_needed:
            mbid = extract_recording_mbid(track)
            data = (batch or {}).get(mbid) if mbid else None
            if not data:
                continue
            key = _norm(track["title"])
            entry = entries.setdefault(key, {})
            entry["listenbrainz_listens"] = int(data.get("total_listen_count") or 0)
            entry["listenbrainz_users"] = int(data.get("total_user_count") or 0)

    # 4. Persist ONLY new/changed rows — unchanged counts keep their
    #    ``updated_at`` and stay fresh longer.
    title_by_key = {_norm(t["title"]): t["title"] for t in tracks if t.get("title")}
    lf_titles = _lf_top_tracks_titles.get(artist) or {}
    rows_to_upsert = []
    for key, entry in entries.items():
        if not entry:
            continue
        prev = cached.get(key) or {}
        entry_lf_listeners = int(entry.get("lastfm_listeners") or 0)
        entry_lf_playcount = int(entry.get("lastfm_playcount") or 0)
        entry_lb_listens = int(entry.get("listenbrainz_listens") or 0)
        entry_lb_users = int(entry.get("listenbrainz_users") or 0)
        if (
            int(prev.get("lastfm_listeners") or 0) == entry_lf_listeners
            and int(prev.get("lastfm_playcount") or 0) == entry_lf_playcount
            and int(prev.get("listenbrainz_listens") or 0) == entry_lb_listens
            and int(prev.get("listenbrainz_users") or 0) == entry_lb_users
        ):
            continue  # unchanged — no write
        rows_to_upsert.append({
            "artist": artist,
            "title": title_by_key.get(key) or lf_titles.get(key) or key,
            "lastfm_listeners": entry_lf_listeners,
            "lastfm_playcount": entry_lf_playcount,
            "listenbrainz_listens": entry_lb_listens,
            "listenbrainz_users": entry_lb_users,
            "source": "bulk",
        })
    if rows_to_upsert:
        try:
            cache_repo.upsert_track_popularity_bulk(rows_to_upsert)
        except Exception as exc:
            logger.debug("[popularity_cache] upsert failed for %s: %s", artist, exc)

    return entries
