#!/usr/bin/env python3
"""
Shared popularity helpers for Spotify/Last.fm/ListenBrainz lookups and weights.
Functions are used by both the main scanner (start.py) and popularity.py.
"""

from __future__ import annotations

import os
import yaml
import math
import logging
import json
import time
import difflib
from contextlib import contextmanager
from typing import Any, Tuple, List, Dict, Optional
from datetime import datetime
from collections import defaultdict
from statistics import mean, stdev, median

from api_clients.spotify import SpotifyClient
from api_clients.lastfm import LastFmClient
from api_clients.audiodb_and_listenbrainz import (
    score_by_age as _score_by_age,
    get_recording_popularity_batch as _lb_get_recording_popularity_batch,
    get_listenbrainz_popularity as _lb_get_listenbrainz_popularity,
    get_listenbrainz_score as _lb_get_listenbrainz_score,
)
from api_clients import timeout_safe_session
from helpers.helpers import strip_cover_attribution
from helpers.db_utils import _is_postgres_connection, get_db_connection

# ============================================================================
# Shared z-score and popularity utilities
# ============================================================================

Z_SCORE_MIDPOINT = 50.0
Z_SCORE_TO_POPULARITY_SCALE = 16.7
_TRACKS_COLUMN_CACHE: Dict[str, set[str]] = {}
_TRACKS_COLUMN_TYPES_CACHE: Dict[str, Dict[str, str]] = {}

_PG_INT_TYPES = {
    "smallint",
    "integer",
    "bigint",
    "int2",
    "int4",
    "int8",
}
_PG_FLOAT_TYPES = {
    "real",
    "double precision",
    "numeric",
    "decimal",
    "float4",
    "float8",
}
_PG_BOOL_TYPES = {"boolean", "bool"}

DEFAULT_LISTENBRAINZ_BATCH_SIZE = 100


def _get_tracks_table_columns(cursor) -> set[str]:
    """Return cached set of columns currently present on the tracks table."""
    cache_key = "postgres"
    cached = _TRACKS_COLUMN_CACHE.get(cache_key)
    if cached:
        return cached

    cursor.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = 'tracks' AND table_schema = 'public'
        """
    )
    columns = {
        (row.get("column_name") if hasattr(row, "get") else row[0])
        for row in cursor.fetchall()
    }

    _TRACKS_COLUMN_CACHE[cache_key] = columns
    return columns


def _get_tracks_table_column_types(cursor) -> Dict[str, str]:
    """Return cached mapping of tracks column -> PostgreSQL type name."""
    cache_key = "postgres"
    cached = _TRACKS_COLUMN_TYPES_CACHE.get(cache_key)
    if cached:
        return cached

    cursor.execute(
        """
        SELECT column_name, data_type, udt_name
        FROM information_schema.columns
        WHERE table_name = 'tracks' AND table_schema = 'public'
        """
    )
    column_types: Dict[str, str] = {}
    for row in cursor.fetchall():
        column_name = row.get("column_name") if hasattr(row, "get") else row[0]
        data_type = row.get("data_type") if hasattr(row, "get") else row[1]
        udt_name = row.get("udt_name") if hasattr(row, "get") else row[2]
        normalized_type = (data_type or udt_name or "").lower()
        if not normalized_type and udt_name:
            normalized_type = str(udt_name).lower()
        column_types[column_name] = normalized_type

    _TRACKS_COLUMN_TYPES_CACHE[cache_key] = column_types
    return column_types


def _coerce_track_value_for_pg_type(column: str, value, pg_type: str):
    """Normalize track values to match PostgreSQL column types."""
    if value is None:
        return None

    normalized_type = (pg_type or "").lower()
    stripped = value.strip() if isinstance(value, str) else value

    if normalized_type in _PG_INT_TYPES:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str):
            if stripped == "":
                return None
            try:
                return int(float(stripped))
            except ValueError:
                logging.debug(
                    "save_to_db coercion: invalid integer for column %s: %r; storing NULL",
                    column,
                    value,
                )
                return None

    if normalized_type in _PG_FLOAT_TYPES:
        if isinstance(value, bool):
            return float(int(value))
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            if stripped == "":
                return None
            try:
                return float(stripped)
            except ValueError:
                logging.debug(
                    "save_to_db coercion: invalid float for column %s: %r; storing NULL",
                    column,
                    value,
                )
                return None

    if normalized_type in _PG_BOOL_TYPES:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(int(value))
        if isinstance(value, str):
            if stripped == "":
                return None
            lowered = stripped.lower()
            if lowered in {"1", "t", "true", "y", "yes", "on"}:
                return True
            if lowered in {"0", "f", "false", "n", "no", "off"}:
                return False
            logging.debug(
                "save_to_db coercion: invalid boolean for column %s: %r; storing NULL",
                column,
                value,
            )
            return None

    return value


def calculate_track_zscore(score: float, mean_value: float, stddev: float) -> float:
    """Calculate z-score for a track relative to a reference distribution."""
    if stddev and stddev > 0:
        return (score - mean_value) / stddev
    return 0.0


def zscore_to_popularity(z_score: float) -> float:
    """Convert z-score to 0-100 popularity scale."""
    score = Z_SCORE_MIDPOINT + (z_score * Z_SCORE_TO_POPULARITY_SCALE)
    return min(100.0, max(0.0, score))


@contextmanager
def get_db_connection_context(conn=None):
    """
    Context manager for safe database connection handling.
    Automatically closes connections that were created by this manager.
    """
    should_close = conn is None

    if should_close:
        try:
            conn = get_db_connection()
        except Exception as e:
            logging.error("Failed to get database connection: %s", e)
            raise

    try:
        yield conn
    finally:
        if should_close and conn:
            try:
                conn.close()
            except Exception as e:
                logging.warning("Error closing database connection: %s", e)


CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/config.yaml")

_DEFAULT_WEIGHTS = {
    "lastfm": 0.70,
    "listenbrainz": 0.20,
    "age": 0.10,
}

_DEFAULT_FEATURES = {
    "scan_worker_threads": 4,
}

_spotify_client: Optional[SpotifyClient] = None
_lastfm_client: Optional[LastFmClient] = None

_spotify_enabled = True
_listenbrainz_enabled = True
_clients_configured = False

DB_LOCK_MAX_RETRIES = 5
DB_LOCK_BASE_DELAY_SECONDS = 0.25


def _is_db_locked_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "database is locked" in msg or "database table is locked" in msg


def _run_with_db_lock_retry(operation, operation_name: str):
    """Run a DB operation with bounded retry on transient SQLite lock errors."""
    for attempt in range(DB_LOCK_MAX_RETRIES):
        try:
            return operation()
        except Exception as e:
            if _is_db_locked_error(e) and attempt < DB_LOCK_MAX_RETRIES - 1:
                wait_time = DB_LOCK_BASE_DELAY_SECONDS * (attempt + 1)
                logging.debug(
                    "%s hit DB lock, retrying in %.2fs (%s/%s)",
                    operation_name,
                    wait_time,
                    attempt + 1,
                    DB_LOCK_MAX_RETRIES,
                )
                time.sleep(wait_time)
                continue
            raise


def _load_config() -> dict:
    config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")
    if not os.path.exists(config_path):
        return {}
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def _resolve_weights(cfg: dict) -> Tuple[float, float, float]:
    """Resolve popularity weights from config."""
    weights = cfg.get("weights") if isinstance(cfg, dict) else None
    weights = weights or {}

    lastfm = float(weights.get("lastfm", _DEFAULT_WEIGHTS["lastfm"]))
    listenbrainz = float(weights.get("listenbrainz", _DEFAULT_WEIGHTS["listenbrainz"]))
    age = float(weights.get("age", _DEFAULT_WEIGHTS["age"]))

    return lastfm, listenbrainz, age


LASTFM_WEIGHT, LISTENBRAINZ_WEIGHT, AGE_WEIGHT = _resolve_weights(_load_config())
SPOTIFY_WEIGHT: float = 0.0  # legacy alias


def _worker_threads(cfg: dict) -> int:
    features = cfg.get("features") if isinstance(cfg, dict) else None
    features = features or {}
    try:
        return int(features.get("scan_worker_threads", _DEFAULT_FEATURES["scan_worker_threads"]))
    except Exception:
        return _DEFAULT_FEATURES["scan_worker_threads"]


def configure_popularity_helpers(
    *,
    spotify_client: Optional[SpotifyClient] = None,
    lastfm_client: Optional[LastFmClient] = None,
    config: Optional[dict] = None,
) -> None:
    """Configure shared clients and refresh weights based on provided config."""
    global _spotify_client, _lastfm_client
    global _spotify_enabled, _listenbrainz_enabled, _clients_configured
    global LASTFM_WEIGHT, LISTENBRAINZ_WEIGHT, AGE_WEIGHT

    cfg = config if config is not None else _load_config()

    LASTFM_WEIGHT, LISTENBRAINZ_WEIGHT, AGE_WEIGHT = _resolve_weights(cfg)

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
        _lastfm_client = LastFmClient(
            lastfm_cfg.get("api_key", ""),
            http_session=timeout_safe_session,
        )

    listenbrainz_cfg = api_cfg.get("listenbrainz") or {}
    _listenbrainz_enabled = bool(listenbrainz_cfg.get("enabled", True))

    _clients_configured = True


def _ensure_clients_from_config() -> None:
    if not _clients_configured:
        configure_popularity_helpers()


# -----------------------------------------------------------------------------
# Spotify helpers
# -----------------------------------------------------------------------------

def get_spotify_artist_id(artist_name: str) -> Optional[str]:
    """
    Get Spotify artist ID with database caching.
    """
    _ensure_clients_from_config()
    if not _spotify_enabled or _spotify_client is None:
        return None

    try:
        conn = get_db_connection()
        placeholder = "%s"
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT spotify_artist_id FROM tracks WHERE artist = {placeholder} AND spotify_artist_id IS NOT NULL LIMIT 1",
            (artist_name,),
        )
        row = cursor.fetchone()
        conn.close()

        cached_id = row["spotify_artist_id"] if row else None
        if cached_id:
            logging.info("✓ Using cached Spotify artist ID for '%s': %s", artist_name, cached_id)
            return cached_id
    except Exception as e:
        logging.debug("Failed to lookup cached Spotify artist ID for '%s': %s", artist_name, e)

    logging.info("Querying Spotify API for artist ID: '%s'", artist_name)
    return _spotify_client.get_artist_id(artist_name)


def get_spotify_artist_single_track_ids(artist_id: str) -> set[str]:
    _ensure_clients_from_config()
    if not _spotify_enabled or _spotify_client is None:
        return set()
    return _spotify_client.get_artist_singles(artist_id) or set()


# -----------------------------------------------------------------------------
# Last.fm title normalization + helpers
# -----------------------------------------------------------------------------

def normalize_title_for_lastfm(title: str) -> str:
    """
    Normalize titles for Last.fm API searches by standardizing special characters.
    """
    if not title:
        return title

    import re

    special_chars = "'?!\"«»–—−′″…¿¡"
    if any(c in title for c in special_chars):
        problem_chars = {c: ord(c) for c in title if c in special_chars}
        logging.debug("normalize_title_for_lastfm input '%s': Found special chars: %s", title, problem_chars)

    title = re.sub(r"[\u2018\u2019\u0060\u0027\u2032\u2033]", "", title)
    title = title.replace("“", "").replace("”", "")
    title = title.replace("«", "").replace("»", "")
    title = title.replace("–", "-").replace("—", "-").replace("−", "-")
    title = title.replace("…", "...")
    title = title.replace("¿", "?").rstrip("?")
    title = title.replace("¡", "!").rstrip("!")
    title = re.sub(r"\s+", " ", title).strip()
    return title


def search_spotify_track(title: str, artist: str, album: Optional[str] = None):
    _ensure_clients_from_config()
    if not _spotify_enabled or _spotify_client is None:
        return []
    normalized_title = normalize_title_for_lastfm(strip_cover_attribution(title))
    return _spotify_client.search_track(normalized_title, artist, album)


def get_lastfm_track_info(artist: str, title: str) -> dict:
    _ensure_clients_from_config()
    if _lastfm_client is None:
        return {"track_play": 0, "listeners": 0}

    stripped_title = strip_cover_attribution(title)
    normalized_title = normalize_title_for_lastfm(stripped_title)

    if stripped_title != normalized_title:
        logging.debug("Title normalization: '%s' → '%s'", stripped_title, normalized_title)

    result = _lastfm_client.get_track_info(artist, normalized_title)
    lookup_artist = result.get("lookup_artist", artist)

    if lookup_artist != artist:
        logging.debug("Last.fm artist fallback: '%s' -> '%s' for '%s'", artist, lookup_artist, normalized_title)

    if result.get("listeners", 0) == 0 and result.get("track_play", 0) == 0:
        logging.debug("Exact match failed for '%s' by '%s', trying fuzzy search...", normalized_title, lookup_artist)
        search_results = _lastfm_client.search_track(lookup_artist, normalized_title, limit=10)

        if search_results:
            best_match = None
            best_ratio = 0.0

            for track in search_results:
                track_name = track.get("name", "")
                track_normalized = normalize_title_for_lastfm(track_name)
                ratio = difflib.SequenceMatcher(
                    None,
                    normalized_title.lower(),
                    track_normalized.lower(),
                ).ratio()

                if ratio > best_ratio:
                    best_ratio = ratio
                    best_match = track_name

            if best_ratio > 0.85 and best_match:
                logging.info(
                    "🔍 Fuzzy matched '%s' → '%s' by '%s' (similarity: %.2f)",
                    title,
                    best_match,
                    lookup_artist,
                    best_ratio,
                )
                result = _lastfm_client.get_track_info(lookup_artist, best_match)
            else:
                logging.debug(
                    "No fuzzy match above threshold (best: %.2f) for '%s' by '%s'",
                    best_ratio,
                    title,
                    lookup_artist,
                )

    return result


# -----------------------------------------------------------------------------
# ListenBrainz helpers
# -----------------------------------------------------------------------------

def _extract_recording_mbid(track: dict) -> Optional[str]:
    """
    Return only a recording MBID suitable for ListenBrainz popularity calls.
    Avoid generic 'mbid' unless your schema guarantees it is always a recording MBID.
    """
    return (
        track.get("recording_mbid")
        or track.get("musicbrainz_recording_mbid")
    )


def get_listenbrainz_batch_for_tracks(tracks: List[dict]) -> Dict[str, Dict[str, Optional[int]]]:
    """
    Fetch ListenBrainz popularity in batch for a list of track dicts.
    Chunks requests to avoid truncation.
    """
    _ensure_clients_from_config()

    if not _listenbrainz_enabled:
        logging.debug("[LB] Skipped batch fetch because ListenBrainz is disabled in config")
        return {}

    recording_mbids: List[str] = []
    for track in tracks:
        mbid = _extract_recording_mbid(track)
        if mbid:
            recording_mbids.append(mbid)

    if not recording_mbids:
        logging.debug("[LB] No recording MBIDs available for batch fetch")
        return {}

    # De-duplicate while preserving order
    seen = set()
    unique_mbids: List[str] = []
    for mbid in recording_mbids:
        if mbid not in seen:
            seen.add(mbid)
            unique_mbids.append(mbid)

    logging.debug("[LB] Fetching popularity for %s recording MBIDs", len(unique_mbids))

    combined: Dict[str, Dict[str, Optional[int]]] = {}
    for i in range(0, len(unique_mbids), DEFAULT_LISTENBRAINZ_BATCH_SIZE):
        chunk = unique_mbids[i:i + DEFAULT_LISTENBRAINZ_BATCH_SIZE]
        chunk_result = _lb_get_recording_popularity_batch(chunk)
        combined.update(chunk_result)
        logging.debug(
            "[LB] Retrieved chunk %s-%s of %s MBIDs",
            i + 1,
            min(i + len(chunk), len(unique_mbids)),
            len(unique_mbids),
        )

    logging.debug("[LB] Received popularity rows for %s recording MBIDs", len(combined))
    return combined


def get_listenbrainz_popularity_for_track(track: dict) -> Dict[str, Optional[int]]:
    """
    Fetch ListenBrainz popularity for a single track dict.
    """
    _ensure_clients_from_config()

    if not _listenbrainz_enabled:
        logging.debug("[LB] Skipped single-track lookup because ListenBrainz is disabled in config")
        return {"total_listen_count": None, "total_user_count": None}

    mbid = _extract_recording_mbid(track)
    if not mbid:
        logging.debug("[LB] Track '%s' has no recording MBID", track.get("title", ""))
        return {"total_listen_count": None, "total_user_count": None}

    result = _lb_get_listenbrainz_popularity(mbid)

    logging.debug(
        "[LB] %s (%s) -> listens=%s users=%s",
        track.get("title", "<unknown>"),
        mbid,
        result.get("total_listen_count"),
        result.get("total_user_count"),
    )

    return result


def get_listenbrainz_score_for_track(track: dict) -> int:
    """
    Backward-compatible per-track ListenBrainz score helper.
    Returns raw total listen count.
    """
    _ensure_clients_from_config()

    if not _listenbrainz_enabled:
        return 0

    mbid = _extract_recording_mbid(track)
    if not mbid:
        return 0

    return _lb_get_listenbrainz_score(mbid)


def calculate_lastfm_popularity_score(listeners: int, artist_max_listeners: int = 0) -> float:
    """
    Calculate a normalized Last.fm popularity score (0-100) from listener count.
    """
    if listeners is None or listeners <= 0:
        return 0.0

    if artist_max_listeners > 0:
        return min(100.0, (listeners / artist_max_listeners) * 100.0)

    try:
        score = 12.5 * math.log10(listeners)
        return min(100.0, max(0.0, score))
    except (ValueError, TypeError):
        return 0.0


def calculate_listenbrainz_popularity_score(listen_count: int) -> float:
    """
    Calculate a normalized ListenBrainz popularity score (0-100) from global listen count.
    """
    if listen_count is None or listen_count <= 0:
        return 0.0

    try:
        score = 12.5 * math.log10(listen_count)
        return min(100.0, max(0.0, score))
    except (ValueError, TypeError):
        return 0.0


def calculate_combined_popularity_score(
    *,
    lastfm_listeners: int = 0,
    lastfm_artist_max_listeners: int = 0,
    listenbrainz_listens: int = 0,
    age_source_value: float = 0.0,
    release_date: Optional[str] = None,
) -> Dict[str, float]:
    """
    Blend Last.fm + ListenBrainz + age into one weighted score.

    Returns a dict so callers can inspect the components in debug logs.
    """
    _ensure_clients_from_config()

    lastfm_score = calculate_lastfm_popularity_score(
        lastfm_listeners,
        artist_max_listeners=lastfm_artist_max_listeners,
    )

    lb_score = calculate_listenbrainz_popularity_score(listenbrainz_listens)

    age_score = 0.0
    if release_date:
        age_score, _ = _score_by_age(age_source_value, release_date)

    total_weight = LASTFM_WEIGHT + LISTENBRAINZ_WEIGHT + AGE_WEIGHT
    if total_weight > 0:
        weighted = (
            (lastfm_score * LASTFM_WEIGHT)
            + (lb_score * LISTENBRAINZ_WEIGHT)
            + (age_score * AGE_WEIGHT)
        ) / total_weight
    else:
        weighted = 0.0

    return {
        "lastfm_score": lastfm_score,
        "listenbrainz_score": lb_score,
        "age_score": age_score,
        "weighted_score": weighted,
    }


# -----------------------------------------------------------------------------
# Popularity adjustment helpers
# -----------------------------------------------------------------------------

def apply_mean_popularity_adjustment(
    track_popularity: float,
    artist_name: str,
    release_year: int | None = None,
    conn=None,
) -> float:
    """
    Apply median+MAD-based popularity adjustment with optional time decay for pre-2005 releases.
    """
    if track_popularity <= 0:
        return 0.0

    MIN_SPREAD = 10.0

    with get_db_connection_context(conn) as db_conn:
        try:
            placeholder = "%s"
            cursor = db_conn.cursor()

            cursor.execute(
                f"""
                SELECT median_popularity, popularity_mad
                FROM artist_stats
                WHERE artist_name = {placeholder}
                """,
                (artist_name,),
            )

            row = cursor.fetchone()
            if not row:
                return track_popularity

            artist_median = row["median_popularity"]
            artist_mad = row["popularity_mad"]

            if artist_median is None or artist_median <= 0:
                return track_popularity

            artist_spread = max(artist_mad if artist_mad else 0, MIN_SPREAD)

            if artist_spread > 0:
                z_score = (track_popularity - artist_median) / artist_spread
            else:
                z_score = 0

            if release_year and release_year < 2005:
                years_before_2005 = 2005 - release_year
                decay_factor = max(0.2, 1.0 - (years_before_2005 * 0.04))
                z_score *= decay_factor
                logging.debug(
                    "Applied time decay to '%s' release (%s): decay_factor=%.2f z_score=%.2f",
                    artist_name,
                    release_year,
                    decay_factor,
                    z_score,
                )

            adjusted_score = zscore_to_popularity(z_score)

            logging.debug(
                "Median+MAD popularity adjustment for '%s': original=%.1f, z_score=%.2f, adjusted=%.1f (artist_median=%.1f, MAD=%.1f, spread=%.1f)",
                artist_name,
                track_popularity,
                z_score,
                adjusted_score,
                artist_median,
                artist_mad if artist_mad is not None else 0.0,
                artist_spread,
            )

            return adjusted_score

        except Exception as e:
            logging.debug("Error applying median+MAD popularity adjustment for '%s': %s", artist_name, e)
            return track_popularity


def apply_album_deviation_adjustment(
    track_popularity: float,
    artist_name: str,
    album_name: str,
    artist_mean_popularity: float | None = None,
    conn=None,
) -> float:
    """
    Apply album-level z-score deviation adjustment for tracks in lower-popularity albums.
    """
    if track_popularity <= 0:
        return track_popularity

    with get_db_connection_context(conn) as db_conn:
        try:
            placeholder = "%s"
            cursor = db_conn.cursor()

            cursor.execute(
                f"""
                SELECT popularity
                FROM tracks
                WHERE artist = {placeholder} AND album = {placeholder} AND popularity > 0
                ORDER BY popularity
                """,
                (artist_name, album_name),
            )

            rows = cursor.fetchall()
            if not rows or len(rows) < 2:
                return track_popularity

            album_popularities = [row["popularity"] for row in rows]

            try:
                album_mean = mean(album_popularities)
                album_stddev = stdev(album_popularities) if len(album_popularities) > 1 else 0.0
            except (ValueError, ZeroDivisionError):
                return track_popularity

            if album_stddev == 0:
                return track_popularity

            album_zscore = calculate_track_zscore(track_popularity, album_mean, album_stddev)

            if album_mean < 40:
                album_weight = 0.40
            elif album_mean < 60:
                album_weight = 0.30
            else:
                album_weight = 0.15

            album_zscore_pop = zscore_to_popularity(album_zscore)
            adjusted_score = (track_popularity * (1.0 - album_weight)) + (album_zscore_pop * album_weight)

            logging.debug(
                "Album deviation adjustment for '%s' - '%s': original=%.1f, album_mean=%.1f, album_stddev=%.2f, album_zscore=%.2f, weight=%.0f%%, adjusted=%.1f",
                artist_name,
                album_name,
                track_popularity,
                album_mean,
                album_stddev,
                album_zscore,
                album_weight * 100,
                adjusted_score,
            )

            return adjusted_score

        except Exception as e:
            logging.debug("Error applying album deviation adjustment for '%s' - '%s': %s", artist_name, album_name, e)
            return track_popularity


# --- Shared DB/API/Helper Functions (moved from start.py) ---

_nav_client_cache = None


def _get_nav_client():
    """Get or create NavidromeClient instance with caching."""
    global _nav_client_cache

    if _nav_client_cache is not None:
        return _nav_client_cache

    try:
        from start import nav_client
        if nav_client is not None:
            _nav_client_cache = nav_client
            return nav_client
    except (ImportError, AttributeError):
        pass

    from api_clients.navidrome import NavidromeClient

    config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}

        nav_users = config.get("navidrome_users")
        if nav_users and len(nav_users) > 0:
            user_config = nav_users[0]
            base_url = user_config.get("base_url")
            username = user_config.get("user")
            password = user_config.get("pass")
        else:
            nav_config = config.get("navidrome", {})
            base_url = nav_config.get("base_url")
            username = nav_config.get("user")
            password = nav_config.get("pass")

        if base_url and username and password:
            _nav_client_cache = NavidromeClient(base_url, username, password)
            return _nav_client_cache
    except Exception as e:
        logging.error("Failed to create NavidromeClient: %s", e)

    return None


def fetch_artist_albums(artist_id):
    """Fetch albums for an artist (wrapper using NavidromeClient)."""
    nav_client = _get_nav_client()
    if nav_client is None:
        raise RuntimeError("NavidromeClient not available - check your configuration")
    return nav_client.fetch_artist_albums(artist_id)


def fetch_album_tracks(album_id):
    """
    Fetch all tracks for an album using Subsonic API (wrapper using NavidromeClient).
    :param album_id: Album ID in Navidrome
    :return: Dict with 'tracks' (list of track objects) and 'artist' (album artist name)
    """
    nav_client = _get_nav_client()
    if nav_client is None:
        raise RuntimeError("NavidromeClient not available - check your configuration")
    return nav_client.fetch_album_tracks(album_id)


# Columns stored as PostgreSQL BOOLEAN type (accept Python True/False directly).
# All other boolean-like fields use INTEGER/BIGINT and require int conversion.
_PG_BOOLEAN_COLUMNS = {"is_single"}

# Fields whose existing non-empty DB value must never be overwritten by an
# empty/unknown incoming value, regardless of which scan is writing.
_PRESERVE_IF_EMPTY = {
    "spotify_album_type",
    "musicbrainz_albumtype",
}

_NAVIDROME_OWNED_FIELDS = {
    "navidrome_genres",
    "navidrome_genre",
    "writer",
    "composer",
    "lyricist",
    "arranger",
    "mixer",
    "producer",
    "work",
    "isrc",
    "titlesort",
    "albumsort",
    "artistsort",
    "albumartistsort",
    "lyricistsort",
    "artistssort",
    "albumartistssort",
    "artists",
    "albumartists",
    "releasetype",
    "releasestatus",
    "releasecountry",
    "media",
    "label",
    "recordlabel",
    "tracktotal",
    "disctotal",
    "compilation",
    "grouping",
    "albumversion",
    "discsubtitle",
    "script",
    "releasedate",
    "originalyear",
    "originaldate",
    "copyright",
    "barcode",
    "catalognumber",
    "asin",
    "subtitle",
    "lyrics",
    "language",
    "movement",
    "movementname",
    "movementtotal",
    "key",
    "explicitstatus",
    "conductor",
    "remixer",
    "engineer",
    "director",
    "djmixer",
    "performer",
    "composersort",
    "encodedby",
    "encodersettings",
    "website",
    "license",
    "replaygain_track_gain",
    "replaygain_track_peak",
    "replaygain_album_gain",
    "replaygain_album_peak",
    "r128_track_gain",
    "r128_album_gain",
}


def _has_meaningful_value(value) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        stripped = value.strip()
        return stripped not in {"", "[]", "{}", "null", "None"}
    if isinstance(value, (list, dict, tuple, set)):
        return len(value) > 0
    if isinstance(value, (int, float)):
        return value != 0
    return True


def save_to_db(track_data):
    """
    Save or update a track in the database.

    This function implements duplicate prevention by checking if a track with the same
    (artist, album, title, duration) already exists. If it does, it updates the existing
    track instead of creating a duplicate with a different ID.
    """
    is_navidrome_sync = bool(track_data.get("_navidrome_sync"))

    conn = get_db_connection()
    cursor = conn.cursor()
    placeholder = "%s"

    if track_data.get("genres"):
        logging.debug(
            "[GENRE] Saving track '%s' with genres: '%s'",
            track_data.get("title"),
            track_data.get("genres"),
        )

    sanitized_data = {}
    for key, value in track_data.items():
        if isinstance(value, list):
            sanitized_data[key] = ", ".join(str(v) for v in value) if value else ""
        elif isinstance(value, bool) and key not in _PG_BOOLEAN_COLUMNS:
            sanitized_data[key] = int(value)
        else:
            sanitized_data[key] = value

    sanitized_data.pop("_navidrome_sync", None)

    try:
        existing_track_columns = _get_tracks_table_columns(cursor)
        existing_track_column_types = _get_tracks_table_column_types(cursor)
        dropped_keys = [key for key in list(sanitized_data.keys()) if key not in existing_track_columns]
        for key in dropped_keys:
            sanitized_data.pop(key, None)
        if dropped_keys:
            logging.warning(
                "save_to_db dropped unknown tracks column(s): %s",
                ", ".join(sorted(dropped_keys)),
            )

        for key in list(sanitized_data.keys()):
            column_type = existing_track_column_types.get(key, "")
            sanitized_data[key] = _coerce_track_value_for_pg_type(
                key,
                sanitized_data[key],
                column_type,
            )
    except Exception as schema_err:
        logging.debug("save_to_db could not inspect tracks schema before upsert: %s", schema_err)

    if not is_navidrome_sync:
        track_id_for_merge = sanitized_data.get("id")
        merge_fields = [f for f in _NAVIDROME_OWNED_FIELDS if f in sanitized_data]
        if track_id_for_merge and merge_fields:
            try:
                select_cols = ", ".join(merge_fields)
                _run_with_db_lock_retry(
                    lambda: cursor.execute(
                        f"SELECT {select_cols} FROM tracks WHERE id = {placeholder}",
                        (track_id_for_merge,),
                    ),
                    "save_to_db navidrome field merge lookup",
                )
                existing_row = cursor.fetchone()
                if existing_row:
                    for field in merge_fields:
                        existing_value = existing_row[field] if hasattr(existing_row, "keys") else None
                        incoming_value = sanitized_data.get(field)
                        if _has_meaningful_value(existing_value):
                            sanitized_data[field] = existing_value
                        elif not _has_meaningful_value(incoming_value):
                            sanitized_data[field] = existing_value
            except Exception:
                pass

    _preserve_track_id = sanitized_data.get("id")
    _valid_columns = existing_track_columns if "existing_track_columns" in locals() else set()
    _preserve_fields = [
        f for f in _PRESERVE_IF_EMPTY
        if f in sanitized_data
        and not _has_meaningful_value(sanitized_data.get(f))
        and (not _valid_columns or f in _valid_columns)
    ]
    if _preserve_track_id and _preserve_fields:
        try:
            _pf_cols = ", ".join(_preserve_fields)
            _run_with_db_lock_retry(
                lambda: cursor.execute(
                    f"SELECT {_pf_cols} FROM tracks WHERE id = {placeholder}",
                    (_preserve_track_id,),
                ),
                "save_to_db preserve-if-empty lookup",
            )
            _pf_row = cursor.fetchone()
            if _pf_row:
                for field in _preserve_fields:
                    existing_value = _pf_row[field] if hasattr(_pf_row, "keys") else None
                    if _has_meaningful_value(existing_value):
                        sanitized_data[field] = existing_value
        except Exception:
            pass

    track_id = sanitized_data.get("id")
    artist = sanitized_data.get("artist")
    album = sanitized_data.get("album")
    title = sanitized_data.get("title")
    duration = sanitized_data.get("duration")
    file_path = sanitized_data.get("file_path")

    if artist and album and title:
        queue_entry = None
        queue_like_pattern = "__queued_for_download__queue_id_%"

        incoming_mbid = sanitized_data.get("mbid")
        if incoming_mbid:
            _run_with_db_lock_retry(
                lambda: cursor.execute(
                    f"""
                    SELECT id, beets_mbid, mbid, file_path, last_scanned
                    FROM tracks
                    WHERE (mbid = {placeholder} OR suggested_mbid = {placeholder})
                        AND file_path LIKE {placeholder}
                        AND id != {placeholder}
                    LIMIT 1
                    """,
                    (incoming_mbid, incoming_mbid, queue_like_pattern, track_id),
                ),
                "save_to_db queue entry MBID lookup",
            )
            queue_entry = cursor.fetchone()

            if queue_entry:
                logging.info("Matched queue entry by MBID %s: %s - %s", incoming_mbid, artist, title)

        if not queue_entry:
            _run_with_db_lock_retry(
                lambda: cursor.execute(
                    f"""
                    SELECT id, beets_mbid, mbid, file_path, last_scanned
                    FROM tracks
                    WHERE artist = {placeholder} AND album = {placeholder} AND title = {placeholder}
                        AND file_path LIKE {placeholder}
                        AND id != {placeholder}
                    LIMIT 1
                    """,
                    (artist, album, title, queue_like_pattern, track_id),
                ),
                "save_to_db queue entry metadata lookup",
            )
            queue_entry = cursor.fetchone()

        if queue_entry:
            queue_id = queue_entry["id"] if hasattr(queue_entry, "keys") else queue_entry[0]
            logging.info(
                "Found queue entry %s for %s - %s, updating to Navidrome ID %s",
                queue_id,
                artist,
                title,
                track_id,
            )
            _run_with_db_lock_retry(
                lambda: cursor.execute(f"DELETE FROM tracks WHERE id = {placeholder}", (queue_id,)),
                "save_to_db delete queue entry",
            )
            existing = None
        else:
            if file_path:
                _run_with_db_lock_retry(
                    lambda: cursor.execute(
                        f"""
                        SELECT id, beets_mbid, mbid, file_path, last_scanned
                        FROM tracks
                        WHERE file_path = {placeholder} AND id != {placeholder}
                        LIMIT 1
                        """,
                        (file_path, track_id),
                    ),
                    "save_to_db duplicate file_path lookup",
                )
                existing = cursor.fetchone()
            else:
                existing = None

        should_content_dedupe = not file_path or str(file_path).startswith("__queued_for_download__")
        if not existing and should_content_dedupe:
            if duration:
                _run_with_db_lock_retry(
                    lambda: cursor.execute(
                        f"""
                        SELECT id, beets_mbid, mbid, file_path, last_scanned
                        FROM tracks
                        WHERE artist = {placeholder} AND album = {placeholder} AND title = {placeholder}
                            AND ABS(COALESCE(duration, 0) - {placeholder}) <= 2
                            AND id != {placeholder}
                        LIMIT 1
                        """,
                        (artist, album, title, duration, track_id),
                    ),
                    "save_to_db duplicate duration lookup",
                )
            else:
                _run_with_db_lock_retry(
                    lambda: cursor.execute(
                        f"""
                        SELECT id, beets_mbid, mbid, file_path, last_scanned
                        FROM tracks
                        WHERE artist = {placeholder} AND album = {placeholder} AND title = {placeholder}
                            AND id != {placeholder}
                        LIMIT 1
                        """,
                        (artist, album, title, track_id),
                    ),
                    "save_to_db duplicate content lookup",
                )

            existing = cursor.fetchone()

        if file_path and not str(file_path).startswith("__queued_for_download__") and not existing:
            _run_with_db_lock_retry(
                lambda: cursor.execute(
                    f"""
                    SELECT id, mbid, musicbrainz_album_mbid
                    FROM tracks
                    WHERE artist = {placeholder} AND album = {placeholder} AND title = {placeholder}
                      AND (file_path IS NULL OR file_path = '')
                      AND id != {placeholder}
                    LIMIT 1
                    """,
                    (artist, album, title, track_id),
                ),
                "save_to_db missing placeholder lookup",
            )
            missing_placeholder = cursor.fetchone()
            if missing_placeholder:
                is_dict = hasattr(missing_placeholder, "keys")
                missing_id = missing_placeholder["id"] if is_dict else missing_placeholder[0]
                missing_mbid = (missing_placeholder["mbid"] if is_dict else missing_placeholder[1]) or ""
                missing_album_mbid = (
                    missing_placeholder["musicbrainz_album_mbid"] if is_dict else missing_placeholder[2]
                ) or ""

                if missing_mbid and not sanitized_data.get("mbid"):
                    sanitized_data["mbid"] = missing_mbid
                if missing_album_mbid and not sanitized_data.get("musicbrainz_album_mbid"):
                    sanitized_data["musicbrainz_album_mbid"] = missing_album_mbid

                _run_with_db_lock_retry(
                    lambda: cursor.execute(f"DELETE FROM tracks WHERE id = {placeholder}", (missing_id,)),
                    "save_to_db delete missing placeholder",
                )
                logging.info(
                    "[DEDUP] Removed missing placeholder %r for '%s - %s' (real file found: %s)",
                    missing_id,
                    artist,
                    title,
                    file_path,
                )

        if existing:
            existing_id = existing["id"]
            existing_beets_mbid = existing["beets_mbid"]
            existing_mbid = existing["mbid"]
            existing_file_path = existing["file_path"]

            new_beets_mbid = sanitized_data.get("beets_mbid")
            new_mbid = sanitized_data.get("mbid")
            new_file_path = sanitized_data.get("file_path")

            existing_score = 0
            new_score = 0

            if existing_beets_mbid:
                existing_score += 1000
            if existing_mbid:
                existing_score += 500
            if existing_file_path:
                existing_score += 200

            if new_beets_mbid:
                new_score += 1000
            if new_mbid:
                new_score += 500
            if new_file_path:
                new_score += 200

            if new_score > existing_score:
                logging.debug(
                    "Duplicate found: Keeping new track ID %s, deleting %s (artist=%s, title=%s)",
                    track_id,
                    existing_id,
                    artist,
                    title,
                )
                _run_with_db_lock_retry(
                    lambda: cursor.execute(f"DELETE FROM tracks WHERE id = {placeholder}", (existing_id,)),
                    "save_to_db delete older duplicate",
                )
            else:
                logging.debug(
                    "Duplicate found: Keeping existing track ID %s, updating instead of inserting %s (artist=%s, title=%s)",
                    existing_id,
                    track_id,
                    artist,
                    title,
                )
                sanitized_data["id"] = existing_id

    columns = ", ".join(sanitized_data.keys())
    placeholders_str = ", ".join([placeholder] * len(sanitized_data))
    update_clause = ", ".join([f"{k}=excluded.{k}" for k in sanitized_data.keys()])
    sql = f"INSERT INTO tracks ({columns}) VALUES ({placeholders_str}) ON CONFLICT(id) DO UPDATE SET {update_clause}"

    if "genres" in sanitized_data:
        logging.debug(
            "[GENRE] Saving to DB - id=%s, title=%s, genres='%s'",
            sanitized_data.get("id"),
            sanitized_data.get("title"),
            sanitized_data.get("genres"),
        )
        has_backslash = "\\" in sanitized_data.get("genres", "")
        logging.debug(
            "[GENRE] Genre string length: %s, Contains backslash: %s",
            len(sanitized_data.get("genres", "")),
            has_backslash,
        )

    _run_with_db_lock_retry(
        lambda: cursor.execute(sql, list(sanitized_data.values())),
        "save_to_db upsert track",
    )
    _run_with_db_lock_retry(
        lambda: conn.commit(),
        "save_to_db commit",
    )
    conn.close()

    if "genres" in sanitized_data and sanitized_data.get("genres"):
        logging.debug(
            "[GENRE] Successfully saved track ID %s with genres to database",
            sanitized_data.get("id"),
        )


def build_artist_index(verbose: bool = False):
    """Build artist index from Navidrome (wrapper using NavidromeClient)."""
    nav_client = _get_nav_client()
    if nav_client is None:
        raise RuntimeError("NavidromeClient not available - check your configuration")

    artist_map_from_api = nav_client.build_artist_index()
    max_retries = 3

    for attempt in range(max_retries):
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            placeholder = "%s"

            for artist_name, info in artist_map_from_api.items():
                artist_id = info.get("id")
                cursor.execute(
                    f"""
                    INSERT INTO artist_stats (artist_id, artist_name, album_count, track_count, last_updated)
                    VALUES ({placeholder}, {placeholder}, {placeholder}, {placeholder}, {placeholder})
                    ON CONFLICT (artist_id) DO UPDATE SET
                        album_count = EXCLUDED.album_count,
                        track_count = EXCLUDED.track_count
                    """,
                    (artist_id, artist_name, 0, 0, None),
                )
                if verbose:
                    print(f"   📝 Added artist to index: {artist_name} (ID: {artist_id})")
                    logging.info("Added artist to index: %s (ID: %s)", artist_name, artist_id)

            conn.commit()
            conn.close()
            break
        except Exception as e:
            if "locked" in str(e).lower() and attempt < max_retries - 1:
                logging.debug(
                    "Database locked during artist index build, retrying (%s/%s)...",
                    attempt + 1,
                    max_retries,
                )
                time.sleep(1.0 * (attempt + 1))
                continue
            else:
                logging.error("Failed to build artist index after %s attempts: %s", max_retries, e)
                raise

    logging.info("✅ Cached %s artists in DB", len(artist_map_from_api))
    print(f"✅ Cached {len(artist_map_from_api)} artists in DB")
    return artist_map_from_api


def load_artist_map():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT artist_id, artist_name, album_count, track_count, last_updated FROM artist_stats")
    rows = cursor.fetchall()
    conn.close()
    return {
        row["artist_name"]: {
            "id": row["artist_id"],
            "album_count": row["album_count"],
            "track_count": row["track_count"],
            "last_updated": row["last_updated"],
        }
        for row in rows
    }


def get_album_last_scanned_from_db(artist_name: str, album_name: str) -> Optional[str]:
    try:
        conn = get_db_connection()
        placeholder = "%s"
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT MAX(last_scanned) AS max_last_scanned FROM tracks WHERE artist = {placeholder} AND album = {placeholder}",
            (artist_name, album_name),
        )
        row = cursor.fetchone()
        conn.close()
        val = row["max_last_scanned"] if row else None
        return val if val else None
    except Exception as e:
        logging.debug("get_album_last_scanned_from_db failed for '%s / %s': %s", artist_name, album_name, e)
        return None


def get_album_track_count_in_db(artist_name: str, album_name: str) -> int:
    try:
        conn = get_db_connection()
        placeholder = "%s"
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT COUNT(*) AS track_count FROM tracks WHERE artist = {placeholder} AND album = {placeholder}",
            (artist_name, album_name),
        )
        row = cursor.fetchone()
        conn.close()
        return (row["track_count"] if row else 0) or 0
    except Exception as e:
        logging.debug("get_album_track_count_in_db failed for '%s / %s': %s", artist_name, album_name, e)
        return 0


def update_artist_id_for_artist(artist_name: str, artist_id: str) -> int:
    """
    Update all tracks for an artist with the cached Spotify artist ID.
    """
    try:
        conn = get_db_connection()
        placeholder = "%s"
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE tracks SET spotify_artist_id = {placeholder} WHERE artist = {placeholder} AND spotify_artist_id IS NULL",
            (artist_id, artist_name),
        )
        updated = cursor.rowcount
        conn.commit()
        conn.close()
        logging.debug("Updated %s tracks with Spotify artist ID for '%s'", updated, artist_name)
        return updated
    except Exception as e:
        logging.error("Failed to update artist ID for '%s': %s", artist_name, e)
        return 0


def update_discogs_artist_id_for_artist(artist_name: str, discogs_artist_id: str) -> int:
    """
    Update all tracks for an artist with the Discogs artist ID.
    """
    try:
        conn = get_db_connection()
        placeholder = "%s"
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE tracks SET discogs_artist_id = {placeholder} WHERE artist = {placeholder} AND discogs_artist_id IS NULL",
            (discogs_artist_id, artist_name),
        )
        updated = cursor.rowcount
        conn.commit()
        conn.close()
        logging.debug("Updated %s tracks with Discogs artist ID for '%s'", updated, artist_name)
        return updated
    except Exception as e:
        logging.error("Failed to update Discogs artist ID for '%s': %s", artist_name, e)
        return 0


def fetch_comprehensive_metadata(db_track_id: str, spotify_track_id: str, force_refresh: bool = False) -> bool:
    """
    Fetch comprehensive Spotify metadata for a track and store in database.
    """
    _ensure_clients_from_config()
    if not _spotify_enabled or _spotify_client is None or not spotify_track_id:
        return False

    conn = get_db_connection()

    try:
        from spotify_metadata_fetcher import SpotifyMetadataFetcher

        fetcher = SpotifyMetadataFetcher(_spotify_client, conn)

        result = fetcher.fetch_and_store_track_metadata(
            track_id=spotify_track_id,
            db_track_id=db_track_id,
            force_refresh=force_refresh,
        )

        return result
    except Exception as e:
        logging.debug("Failed to fetch comprehensive metadata for track %s: %s", spotify_track_id, e)
        return False
    finally:
        conn.close()


def get_spotify_client() -> Optional[SpotifyClient]:
    """
    Get the configured Spotify client.
    """
    _ensure_clients_from_config()
    return _spotify_client if _spotify_enabled else None


def get_lastfm_client() -> Optional[LastFmClient]:
    """
    Get the configured Last.fm client.
    """
    _ensure_clients_from_config()
    return _lastfm_client


def detect_via_iterative_zscore(
    current_track_score: float,
    artist: str,
    album: str,
    conn=None,
    verbose: bool = False,
) -> bool:
    """
    Detect if a track is a standout using iterative z-score method.
    """
    if not current_track_score or current_track_score <= 0:
        if verbose:
            from helpers.logging_config import log_debug
            log_debug(f"detect_via_iterative_zscore: current_track_score invalid: {current_track_score}")
        return False

    with get_db_connection_context(conn) as db_conn:
        try:
            placeholder = "%s"
            cursor = db_conn.cursor()
            cursor.execute(
                f"""
                SELECT id, title, popularity_score
                FROM tracks
                WHERE artist = {placeholder} AND album = {placeholder} AND popularity_score > 0
                ORDER BY popularity_score DESC
                """,
                (artist, album),
            )

            album_tracks = cursor.fetchall()
            if not album_tracks or len(album_tracks) < 2:
                return False

            album_data = [(row["id"], row["title"], row["popularity_score"]) for row in album_tracks]
            identified_standouts = set()

            iteration = 0
            max_iterations = 5

            while iteration < max_iterations:
                iteration += 1
                remaining_scores = [score for _, _, score in album_data if score > 0]
                if not remaining_scores or len(remaining_scores) < 2:
                    break

                try:
                    album_mean = mean(remaining_scores)
                    album_stdev = stdev(remaining_scores) if len(remaining_scores) > 1 else 0
                except (ValueError, ZeroDivisionError):
                    break

                if album_stdev == 0:
                    break

                top_score = max(remaining_scores)
                top_z = calculate_track_zscore(top_score, album_mean, album_stdev)
                if top_z < 1.0:
                    break

                found_standout = False
                for track_id, _, score in album_data:
                    if abs(score - top_score) < 0.01 and track_id not in identified_standouts:
                        artist_z = _check_artist_zscore(cursor, artist, track_id)
                        if artist_z >= 0.5 or artist_z == -999:
                            identified_standouts.add(track_id)
                            found_standout = True
                            if abs(score - current_track_score) < 0.01:
                                return True
                            album_data = [(tid, tit, ts) for tid, tit, ts in album_data if tid != track_id]
                        break

                if not found_standout:
                    break

            return False
        except Exception as e:
            if verbose:
                logging.debug("Iterative zscore error: %s", e)
            return False


def _check_artist_zscore(cursor, artist: str, track_id: int, conn=None) -> float:
    """Get z-score for a track within its artist catalog. Returns -999 on failure."""
    try:
        placeholder = "%s"
        cursor.execute(f"SELECT popularity_score FROM tracks WHERE id = {placeholder}", (track_id,))
        row = cursor.fetchone()
        if not row:
            return -999

        track_score = row["popularity_score"]
        if not track_score:
            return -999

        cursor.execute(
            f"""
            SELECT mean_popularity, popularity_stddev
            FROM artist_stats
            WHERE artist = {placeholder}
            """,
            (artist,),
        )
        stats_row = cursor.fetchone()
        if not stats_row:
            return -999

        artist_mean = stats_row["mean_popularity"]
        artist_stdev_val = stats_row["popularity_stddev"]
        if not artist_mean or artist_mean <= 0:
            return -999
        artist_stdev = artist_stdev_val if artist_stdev_val else 1
        if artist_stdev == 0:
            return -999

        return calculate_track_zscore(track_score, artist_mean, artist_stdev)
    except Exception as e:
        logging.debug("Artist zscore error: %s", e)
        return -999


def get_top_standout_tracks_with_gap(
    artist: str,
    album: str,
    conn=None,
    gap_threshold: float = 0.5,
    is_compilation: bool = False,
    verbose: bool = False,
) -> set:
    """
    Identify tracks at the top of an album with a clear gap from lower tracks.
    """
    with get_db_connection_context(conn) as db_conn:
        try:
            cursor = db_conn.cursor()
            cursor.execute(
                """
                SELECT id, title, popularity_score
                FROM tracks
                WHERE COALESCE(NULLIF(album_artist, ''), artist) = %s AND album = %s AND popularity_score > 0
                ORDER BY popularity_score DESC
                """,
                (artist, album),
            )

            album_tracks = cursor.fetchall()
            if not album_tracks or len(album_tracks) < 2:
                return set()

            album_data = [(row["id"], row["title"], row["popularity_score"]) for row in album_tracks]
            scores = [score for _, _, score in album_data]
            try:
                album_mean = mean(scores)
                album_stdev = stdev(scores) if len(scores) > 1 else 0
            except (ValueError, ZeroDivisionError):
                return set()
            if album_stdev == 0:
                return set()

            top_standouts = set()
            prev_z = None
            for track_id, _, score in album_data:
                current_z = calculate_track_zscore(score, album_mean, album_stdev)
                if prev_z is None:
                    if current_z >= 0.8:
                        top_standouts.add(track_id)
                        prev_z = current_z
                    else:
                        break
                else:
                    if current_z < 0.5:
                        break
                    gap = prev_z - current_z
                    if gap < gap_threshold:
                        top_standouts.add(track_id)
                        prev_z = current_z
                    else:
                        break

            album_lower = album.lower()
            greatest_hits_patterns = [
                "greatest hits", "best of", "the best", "collection", "anthology",
                "essentials", " hits", "singles", "the very best", "gold", "platinum",
                "ultimate collection", "complete", "definitive",
            ]
            is_greatest_hits = any(pattern in album_lower for pattern in greatest_hits_patterns)

            total_tracks = len(album_data)
            standout_count = len(top_standouts)
            if standout_count > total_tracks / 2 and not is_compilation and not is_greatest_hits:
                if verbose:
                    logging.debug(
                        "Top standouts: %s/%s tracks qualify (>50%%), returning empty set - no clear standouts",
                        standout_count,
                        total_tracks,
                    )
                return set()
            elif standout_count > total_tracks / 2 and (is_compilation or is_greatest_hits):
                if verbose:
                    album_type = "compilation" if is_compilation else "greatest hits"
                    logging.debug(
                        "Top standouts: %s/%s tracks qualify (>50%%) but this is a %s album - allowing standouts",
                        standout_count,
                        total_tracks,
                        album_type,
                    )

            return top_standouts
        except Exception as e:
            try:
                db_conn.rollback()
            except Exception:
                pass
            if verbose:
                logging.debug("Top standouts detection error: %s", e)
            return set()


__all__ = [
    "configure_popularity_helpers",
    "get_spotify_artist_id",
    "get_spotify_artist_single_track_ids",
    "search_spotify_track",
    "get_lastfm_track_info",
    "get_listenbrainz_batch_for_tracks",
    "get_listenbrainz_popularity_for_track",
    "get_listenbrainz_score_for_track",
    "calculate_lastfm_popularity_score",
    "calculate_lastfm_zscore_popularity",
    "calculate_listenbrainz_popularity_score",
    "calculate_combined_popularity_score",
    "score_by_age",
    "apply_mean_popularity_adjustment",
    "apply_album_deviation_adjustment",
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
    "update_artist_id_for_artist",
    "fetch_comprehensive_metadata",
    "get_spotify_client",
    "get_lastfm_client",
    "detect_via_iterative_zscore",
    "get_top_standout_tracks_with_gap",
]