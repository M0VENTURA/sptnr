"""MusicBrainz WS/2 search client for upcoming, new, and recently-added releases.

Implements the three-scan strategy required by the upcoming-releases feature:
1. Newly released albums  – date range in the past
2. Upcoming releases      – date range in the future
3. Recently added entries – added range in the MusicBrainz database

Results are filtered to artists that exist in the local collection or in the
recommended-artist cache, deduplicated by release-group MBID, and normalised
to match the ``upcoming_releases`` table field layout so they can be merged
transparently with existing DB-sourced releases.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import unicodedata
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

import requests

logger = logging.getLogger(__name__)

_USER_AGENT = "sptnr/2.0.0 ( https://github.com/M0VENTURA/sptnr )"
_MB_RELEASE_SEARCH_URL = "https://musicbrainz.org/ws/2/release"

# Cache directory lives under the configured config path so it persists across
# restarts but is easy to clear.
_CACHE_DIR = os.path.join(os.environ.get("CONFIG_PATH", "/config"), ".mb_upcoming_cache")

_CACHE_TTL_SECONDS = 24 * 60 * 60  # 24 hours
_RATE_LIMIT_SECONDS = 1.0
_MAX_RETRIES = 3
_BASE_BACKOFF_SECONDS = 1.0


def _ensure_cache_dir() -> None:
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
    except Exception as exc:
        logger.debug("Could not create MB upcoming cache dir: %s", exc)


def _cache_path(query: str, offset: int) -> str:
    key = hashlib.sha256(f"{query}:{offset}".encode("utf-8")).hexdigest()
    return os.path.join(_CACHE_DIR, f"{key}.json")


def _read_cache(query: str, offset: int) -> Optional[Dict[str, Any]]:
    path = _cache_path(query, offset)
    if not os.path.exists(path):
        return None
    try:
        if time.time() - os.path.getmtime(path) > _CACHE_TTL_SECONDS:
            return None
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)  # type: ignore[return-value]
    except Exception as exc:
        logger.debug("Could not read MB upcoming cache: %s", exc)
        return None


def _write_cache(query: str, offset: int, data: Dict[str, Any]) -> None:
    _ensure_cache_dir()
    path = _cache_path(query, offset)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    except Exception as exc:
        logger.debug("Could not write MB upcoming cache: %s", exc)


def _normalise_artist(name: str) -> str:
    """Normalise artist name for set-based filtering."""
    if not name:
        return ""
    text = unicodedata.normalize("NFKD", name)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    # collapse non-alphanumerics to spaces
    import re

    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return " ".join(text.split())


def _fetch_page(query: str, offset: int = 0, limit: int = 100) -> Optional[Dict[str, Any]]:
    """Fetch a single page of MusicBrainz release search results.

    Implements 24-hour caching, 1-second rate limiting between requests, and
    exponential-backoff retry on 5xx / 429.
    """
    cached = _read_cache(query, offset)
    if cached is not None:
        return cached

    params: Dict[str, Any] = {
        "query": query,
        "fmt": "json",
        "limit": limit,
        "offset": offset,
    }
    headers = {"User-Agent": _USER_AGENT}

    for attempt in range(_MAX_RETRIES):
        try:
            resp = requests.get(
                _MB_RELEASE_SEARCH_URL,
                params=params,
                headers=headers,
                timeout=(10, 30),
            )
            if resp.status_code in (429, 500, 502, 503, 504):
                if attempt < _MAX_RETRIES - 1:
                    wait = _BASE_BACKOFF_SECONDS * (2 ** attempt)
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after:
                        try:
                            wait = float(retry_after)
                        except (TypeError, ValueError):
                            pass
                    logger.debug(
                        "MusicBrainz upcoming search %s (attempt %d/%d), waiting %.1fs",
                        resp.status_code,
                        attempt + 1,
                        _MAX_RETRIES,
                        wait,
                    )
                    time.sleep(wait)
                    continue
            resp.raise_for_status()
            data = resp.json()
            _write_cache(query, offset, data)
            time.sleep(_RATE_LIMIT_SECONDS)
            return data  # type: ignore[return-value]
        except requests.exceptions.RequestException as exc:
            if attempt < _MAX_RETRIES - 1:
                wait = _BASE_BACKOFF_SECONDS * (2 ** attempt)
                logger.debug(
                    "MusicBrainz upcoming search error (attempt %d/%d), waiting %.1fs: %s",
                    attempt + 1,
                    _MAX_RETRIES,
                    wait,
                    exc,
                )
                time.sleep(wait)
            else:
                logger.warning(
                    "MusicBrainz upcoming search failed after %d attempts: %s",
                    _MAX_RETRIES,
                    exc,
                )
    return None


def _search_releases(query: str, max_results: int = 200) -> List[Dict[str, Any]]:
    """Paginate through a release search up to *max_results* items."""
    results: List[Dict[str, Any]] = []
    offset = 0
    page_size = 100
    max_pages = max(1, (max_results + page_size - 1) // page_size)

    for _ in range(max_pages):
        data = _fetch_page(query, offset=offset, limit=page_size)
        if not data:
            break
        releases = data.get("releases") or []
        if not releases:
            break
        results.extend(releases)
        if len(releases) < page_size:
            break
        offset += page_size

    return results


def _parse_date(raw: Optional[str]) -> Optional[str]:
    """Normalise a MusicBrainz partial date to ISO-8601 YYYY-MM-DD."""
    if not raw:
        return None
    raw = raw.strip()
    if len(raw) >= 10 and raw[4] == "-" and raw[7] == "-":
        return raw[:10]
    if len(raw) >= 7 and raw[4] == "-":
        return raw[:7] + "-01"
    if len(raw) >= 4 and raw[:4].isdigit():
        return raw[:4] + "-01-01"
    return None


def _extract_artists(release: Dict[str, Any]) -> List[str]:
    """Return the list of credited artist names for a release."""
    artists: List[str] = []
    for credit in release.get("artist-credit") or []:
        if not isinstance(credit, dict):
            continue
        artist_obj = credit.get("artist") or {}
        name = artist_obj.get("name") or credit.get("name") or ""
        if name:
            artists.append(name.strip())
    return artists


def _extract_label(release: Dict[str, Any]) -> Optional[str]:
    for li in release.get("label-info") or []:
        label = li.get("label") or {}
        name = label.get("name")
        if name:
            return name.strip()
    return None


def _build_queries(
    lookback_days: int = 7,
    lookahead_days: int = 180,
    added_lookback_days: int = 3,
) -> List[Tuple[str, str]]:
    """Build the three required WS/2 search queries.

    Returns a list of (query_string, source_label) tuples.
    """
    today = datetime.now().date().isoformat()

    queries: List[Tuple[str, str]] = []

    # 1. Newly released albums (past)
    start_new = (datetime.now().date() - timedelta(days=lookback_days)).isoformat()
    queries.append(
        (
            f"date:[{start_new} TO {today}] AND status:official AND type:album",
            "MusicBrainz New Release",
        )
    )

    # 2. Upcoming releases (future)
    end_upcoming = (datetime.now().date() + timedelta(days=lookahead_days)).isoformat()
    queries.append(
        (
            f"date:[{today} TO {end_upcoming}] AND status:official",
            "MusicBrainz Upcoming",
        )
    )

    # 3. Recently added to the MusicBrainz database
    start_added = (datetime.now().date() - timedelta(days=added_lookback_days)).isoformat()
    queries.append(
        (
            f"added:[{start_added} TO {today}]",
            "MusicBrainz Recently Added",
        )
    )

    return queries


def fetch_musicbrainz_upcoming_releases(
    collection_artists: Set[str],
    recommended_artists: Set[str],
    lookback_days: int = 7,
    lookahead_days: int = 180,
    added_lookback_days: int = 3,
    max_results_per_query: int = 200,
) -> List[Dict[str, Any]]:
    """Run the three MusicBrainz scans and filter to catalogue / recommended artists.

    Args:
        collection_artists:  Set of artist names present in the local library.
        recommended_artists: Set of artist names from similar-artist caches.
        lookback_days:       Days in the past for newly-released detection.
        lookahead_days:      Days in the future for upcoming detection.
        added_lookback_days: Days in the past for recently-added detection.
        max_results_per_query: Maximum raw releases to fetch per query.

    Returns:
        A list of release dicts that match the ``upcoming_releases`` field layout.
    """
    if not collection_artists and not recommended_artists:
        return []

    queries = _build_queries(lookback_days, lookahead_days, added_lookback_days)
    raw_results: List[Tuple[Dict[str, Any], str]] = []

    for query, source_label in queries:
        logger.debug("MusicBrainz upcoming query: %s", query)
        releases = _search_releases(query, max_results=max_results_per_query)
        logger.debug(
            "MusicBrainz upcoming query returned %d raw releases", len(releases)
        )
        raw_results.extend((r, source_label) for r in releases)

    # Deduplicate by release-group MBID, keeping the earliest release date.
    by_rg: Dict[str, Dict[str, Any]] = {}
    for release, source_label in raw_results:
        rg = release.get("release-group") or {}
        rg_id = rg.get("id")
        if not rg_id:
            continue

        release_date = _parse_date(release.get("date"))
        if not release_date:
            continue

        # Skip disqualified secondary types
        secondary = [str(s).lower() for s in (rg.get("secondary-types") or []) if s]
        if any(t in secondary for t in ("live", "remix", "compilation")):
            continue

        artists = _extract_artists(release)
        if not artists:
            continue

        title = (rg.get("title") or release.get("title") or "").strip()
        if not title:
            continue

        existing = by_rg.get(rg_id)
        if existing is None or release_date < existing["release_date"]:
            by_rg[rg_id] = {
                "release_group_mbid": rg_id,
                "release_mbid": release.get("id"),
                "title": title,
                "artists": artists,
                "release_date": release_date,
                "status": release.get("status"),
                "country": release.get("country"),
                "label": _extract_label(release),
                "source_label": source_label,
                "musicbrainz_url": f"https://musicbrainz.org/release-group/{rg_id}",
            }

    if not by_rg:
        return []

    # Build normalised filter sets
    norm_collection = {_normalise_artist(a) for a in collection_artists if a}
    norm_recommended = {_normalise_artist(a) for a in recommended_artists if a}

    results: List[Dict[str, Any]] = []
    now_iso = datetime.now().isoformat()

    for item in by_rg.values():
        matched_artist: Optional[str] = None
        in_collection = False
        in_recommended = False

        for artist in item["artists"]:
            norm = _normalise_artist(artist)
            if norm in norm_collection:
                matched_artist = artist
                in_collection = True
                break
            if norm in norm_recommended:
                matched_artist = artist
                in_recommended = True
                break

        if not matched_artist:
            continue

        release_year: Optional[int] = None
        if len(item["release_date"]) >= 4 and item["release_date"][:4].isdigit():
            release_year = int(item["release_date"][:4])

        results.append(
            {
                "artist_name": matched_artist,
                "album_name": item["title"],
                "release_date": item["release_date"],
                "release_year": release_year,
                "source": item["source_label"],
                "artist_in_collection": in_collection,
                "artist_in_recommended": in_recommended,
                "album_in_collection": False,
                "is_new_release": item["source_label"] == "MusicBrainz New Release",
                "release_group_mbid": item["release_group_mbid"],
                "mbid_match_status": "matched",
                "mbid_source": "musicbrainz_live_search",
                "mbid_confidence": "high",
                "mbid_match_score": 1.0,
                "mbid_last_checked_at": now_iso,
                "mbid_manual_override": False,
                "notes": item["source_label"],
                "url": item["musicbrainz_url"],
                "status": item["status"],
                "country": item["country"],
                "label": item["label"],
                "musicbrainz_url": item["musicbrainz_url"],
            }
        )

    results.sort(key=lambda r: r["release_date"])
    return results
