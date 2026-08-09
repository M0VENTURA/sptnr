"""
Configuration helpers for Popularr.

Handles loading YAML config + caching.
"""

from __future__ import annotations
from typing import Any
import yaml
import os
import re

# -----------------------------------------------------------------------------
# Cache
# -----------------------------------------------------------------------------

_CONFIG_CACHE = None


# -----------------------------------------------------------------------------
# Core loader
# -----------------------------------------------------------------------------

def get_matching_config() -> dict[str, Any]:
    cfg = get_config()

    matching = cfg.get("matching", {})

    return {
        "min_accept_score": matching.get(
            "min_accept_score",
            0.45,
        ),
        "duration_tolerance_seconds": matching.get(
            "duration_tolerance_seconds",
            5,
        ),
        "early_accept_length_tolerance": matching.get(
            "early_accept_length_tolerance",
            2,
        ),
        "top_n_candidates": matching.get(
            "top_n_candidates",
            50,
        ),
    }

def _read_yaml(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
            return yaml.safe_load(content) or {}, content
    except FileNotFoundError:
        return {}, ""
    except yaml.YAMLError:
        return {}, ""


# ---------------------------------------------------------------------------
# Environment variable overrides
# ---------------------------------------------------------------------------
# Any ``POPULARLR_*`` env var overrides a config.yaml key at load time.
# This lets operators set critical values without a config file.
#
# Mapping rules (examples):
#   POPULARLR_NAV_URL            → navidrome_users[0].base_url
#   POPULARLR_NAV_USER           → navidrome_users[0].user
#   POPULARLR_NAV_PASS           → navidrome_users[0].pass
#   POPULARLR_SPOTIFY_CLIENT_ID  → api_integrations.spotify.client_id
#   POPULARLR_LASTFM_API_KEY     → api_integrations.lastfm.api_key
#   POPULARLR_DISCOGS_TOKEN      → api_integrations.discogs.token
#   POPULARLR_DOWNLOADS_FOLDER   → downloads.folder
#   POPULARLR_AUTO_IMPORT        → watcher.auto_import_enabled
#   POPULARLR_SCAN_INTERVAL      → watcher.scan_interval
#   POPULARLR_ESSENTIA_ENABLED   → essentia.tag_moods
#
# Boolean env vars accept "1", "true", "yes" (case-insensitive) for True,
# everything else is False.
# ---------------------------------------------------------------------------

_POPULARLR_PREFIX = "POPULARLR_"
_TRUE_STRINGS = frozenset({"1", "true", "yes"})


def _apply_env_overrides(cfg: dict) -> None:
    """Mutate ``cfg`` in-place with values from ``POPULARLR_*`` env vars."""

    # ── Navidrome (first user) ───────────────────────────────────────
    nav_url = _pop_env("NAV_URL")
    nav_user = _pop_env("NAV_USER")
    nav_pass = _pop_env("NAV_PASS")
    if nav_url or nav_user or nav_pass:
        users = cfg.setdefault("navidrome_users", [])
        if not users:
            users.append({})
        first = users[0]
        if nav_url:
            first["base_url"] = nav_url
        if nav_user:
            first["user"] = nav_user
        if nav_pass:
            first["pass"] = nav_pass
        if not first.get("display_name"):
            first["display_name"] = first.get("user", "Admin")

    # ── API integrations ─────────────────────────────────────────────
    _set_if("SPOTIFY_CLIENT_ID", cfg, "api_integrations", "spotify", "client_id")
    _set_if("SPOTIFY_CLIENT_SECRET", cfg, "api_integrations", "spotify", "client_secret")
    _set_if("LASTFM_API_KEY", cfg, "api_integrations", "lastfm", "api_key")
    _set_if("DISCOGS_TOKEN", cfg, "api_integrations", "discogs", "token")
    _set_if("AUDIODB_API_KEY", cfg, "api_integrations", "audiodb", "api_key")

    # ── Downloads ────────────────────────────────────────────────────
    _set_if("DOWNLOADS_FOLDER", cfg, "downloads", "folder")

    # ── Watcher / automation ─────────────────────────────────────────
    _set_bool_if("AUTO_IMPORT", cfg, "watcher", "auto_import_enabled")
    _set_bool_if("AUTO_POPULARITY_SCAN", cfg, "watcher", "auto_popularity_scan")
    _set_int_if("SCAN_INTERVAL", cfg, "watcher", "scan_interval")

    # ── Essentia ─────────────────────────────────────────────────────
    _set_bool_if("ESSENTIA_ENABLED", cfg, "essentia", "tag_moods")
    _set_if("ESSENTIA_MODELS_DIR", cfg, "essentia", "models_dir")
    _set_if("ESSENTIA_SCRIPT_PATH", cfg, "essentia", "script_path")


def _pop_env(suffix: str) -> str | None:
    """Read and remove a ``POPULARLR_<suffix>`` env var, or return None."""
    val = os.environ.pop(f"{_POPULARLR_PREFIX}{suffix}", "").strip()
    return val if val else None


def _set_if(env_suffix: str, cfg: dict, *keys: str) -> None:
    """Set ``cfg[keys[0]][keys[1]]... = env_val`` if the env var exists."""
    val = _pop_env(env_suffix)
    if val is not None:
        d = cfg
        for k in keys[:-1]:
            d = d.setdefault(k, {})
        d[keys[-1]] = val


def _set_bool_if(env_suffix: str, cfg: dict, *keys: str) -> None:
    """Like ``_set_if`` but converts the value to a bool."""
    raw = _pop_env(env_suffix)
    if raw is not None:
        d = cfg
        for k in keys[:-1]:
            d = d.setdefault(k, {})
        d[keys[-1]] = raw.lower() in _TRUE_STRINGS


def _set_int_if(env_suffix: str, cfg: dict, *keys: str) -> None:
    """Like ``_set_if`` but converts the value to an int."""
    raw = _pop_env(env_suffix)
    if raw is not None:
        try:
            d = cfg
            for k in keys[:-1]:
                d = d.setdefault(k, {})
            d[keys[-1]] = int(raw)
        except ValueError:
            pass


# -----------------------------------------------------------------------------
# Public API
# -----------------------------------------------------------------------------

def get_config() -> dict:
    """
    Load config.yaml with caching, with Pydantic Settings as defaults.

    Pydantic ``Settings`` (loaded from ``POPULARLR_*`` env vars) provide
    the base values.  The YAML file overrides those defaults.  Finally,
    legacy ``POPULARLR_*`` env vars (via ``_apply_env_overrides``) can
    still override everything.

    This layered approach:
    1. Settings (env vars + code defaults) — type-safe, auto-complete
    2. ``config.yaml`` — file-based overrides
    3. Legacy ``_apply_env_overrides`` — backward compatibility
    """

    global _CONFIG_CACHE

    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE

    # ── Layer 1: Pydantic Settings (env vars + code defaults) ──────
    from helpers.settings import get_settings
    s = get_settings()

    cfg: dict[str, Any] = {
        "pg_host": s.pg_host,
        "pg_port": s.pg_port,
        "pg_user": s.pg_user,
        "pg_password": s.pg_password,
        "pg_database": s.pg_database,
        "database_url": s.database_url,
        "db_pool_size": s.db_pool_size,
        "db_pool_overflow": s.db_pool_overflow,
        "music_root": s.music_root,
        "music_folder": s.music_folder,
        "downloads_folder": s.downloads_folder,
        "config_path": s.config_path,
        "log_path": s.log_path,
        "db_path": s.db_path,
        "nav_url": s.nav_url,
        "nav_user": s.nav_user,
        "nav_pass": s.nav_pass,
        "spotify_client_id": s.spotify_client_id,
        "spotify_client_secret": s.spotify_client_secret,
        "lastfm_api_key": s.lastfm_api_key,
        "lastfm_api_secret": s.lastfm_api_secret,
        "discogs_token": s.discogs_token,
        "essentia_enabled": s.essentia_enabled,
        "essentia_script_path": s.essentia_script_path,
        "essentia_models_dir": s.essentia_models_dir,
        "auto_import": s.auto_import,
        "bind": s.bind,
        "workers": s.workers,
        "log_level": s.log_level,
        "state_dir": s.state_dir,
        "use_local_assets": s.use_local_assets,
    }

    # ── Layer 2: YAML file ─────────────────────────────────────────
    config_path = os.environ.get("CONFIG_PATH", s.config_path)
    yaml_cfg, _ = _read_yaml(config_path)
    _deep_merge(cfg, yaml_cfg)

    # ── Layer 3: Legacy env var overrides ──────────────────────────
    _apply_env_overrides(cfg)

    _CONFIG_CACHE = cfg

    return _CONFIG_CACHE


def _deep_merge(base: dict, override: dict) -> None:
    """Merge ``override`` into ``base`` recursively (in-place)."""
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def clear_config_cache():
    """Force reload on next get_config()"""
    global _CONFIG_CACHE
    _CONFIG_CACHE = None


def get_navidrome_config():
    """Return Navidrome section (first user from navidrome_users, or legacy navidrome key)."""
    cfg = get_config()
    nav = cfg.get("navidrome", {})
    if nav:
        return nav
    users = cfg.get("navidrome_users", [])
    if users:
        return users[0]
    return {}


# -----------------------------------------------------------------------------
# Optional (used in app.txt)
# -----------------------------------------------------------------------------

def get_all_services_status():
    """
    Placeholder — returns enabled services.
    (Extend later if needed)
    """
    cfg = get_config()

    return {
        k: v for k, v in cfg.items()
        if isinstance(v, dict)
    }


def is_service_enabled(service_name: str) -> bool:
    cfg = get_config()
    service = cfg.get(service_name, {})

    if isinstance(service, dict):
        return service.get("enabled", False)

    return False


# -----------------------------------------------------------------------------
# Config persistence & helpers
# ---------------------------------------------------------------------------

_CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/config.yaml")


def get_weights() -> dict[str, float]:
    """Get popularity scoring weights from config.

    Returns:
        Dict with keys ``spotify``, ``lastfm``, ``listenbrainz``, ``age``.
    """
    cfg = get_config()
    weights = cfg.get("weights", {})
    return {
        "spotify": float(weights.get("spotify", 0.4)),
        "lastfm": float(weights.get("lastfm", 0.3)),
        "listenbrainz": float(weights.get("listenbrainz", 0.2)),
        "age": float(weights.get("age", 0.1)),
    }


def get_watcher_settings() -> dict[str, Any]:
    """Get watcher service settings from config.

    Returns:
        Dict with watcher settings or defaults.
    """
    cfg = get_config()
    watcher = cfg.get("watcher", {})
    return {
        "scan_interval": int(watcher.get("scan_interval", 30)),
        "navidrome_sync_wait": int(watcher.get("navidrome_sync_wait", 600)),
        "auto_import_enabled": bool(watcher.get("auto_import_enabled", True)),
        "auto_popularity_scan": bool(watcher.get("auto_popularity_scan", True)),
        "downloads_watcher_enabled": bool(watcher.get("downloads_watcher_enabled", True)),
    }


def save_config(config_data: dict) -> bool:
    """Persist a config dict back to the YAML file and clear the cache.

    Args:
        config_data: Complete configuration dictionary to save.

    Returns:
        True on success, False on failure.
    """
    try:
        os.makedirs(os.path.dirname(_CONFIG_PATH), exist_ok=True)
        with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.safe_dump(config_data, f, default_flow_style=False, sort_keys=False)
        clear_config_cache()
        return True
    except Exception:
        return False


def save_partial_config(partial_data: dict) -> bool:
    """Merge partial configuration into the existing config and persist.

    Reads the current config from the YAML file (bypassing the in-memory
    cache), deep-merges *partial_data* into it, writes the result back,
    and clears the cache so the next ``get_config()`` call picks it up.

    Args:
        partial_data: A subset of configuration keys to merge in.

    Returns:
        True on success, False on failure.
    """
    try:
        # Read the current on-disk config (or start with an empty dict)
        existing, _ = _read_yaml(_CONFIG_PATH)
        _deep_merge(existing, partial_data)
        os.makedirs(os.path.dirname(_CONFIG_PATH), exist_ok=True)
        with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.safe_dump(existing, f, default_flow_style=False, sort_keys=False)
        clear_config_cache()
        return True
    except Exception:
        return False


# Minimum candidate score for a Soulseek result to be accepted as a valid match.
# Scores below this threshold trigger fallback queries (e.g. using album_artist).
_SLSKD_MIN_ACCEPT_SCORE = 0.45

# Automatic-search performance tuning: track-length-aware candidate pruning.
# Only the top-N candidates (after length-based prioritisation) receive full
# scoring.  This prevents candidate explosion on popular/ambiguous queries.
_AUTO_SEARCH_TOP_N_CANDIDATES = 50

# Early-acceptance length tolerance: when artist and title match exactly and
# the candidate duration is within this many seconds, the match is accepted
# immediately without scoring the remaining candidates.
_AUTO_SEARCH_EARLY_ACCEPT_LENGTH_TOLERANCE = 2


# Shared constants for orphan-token detection used in both
# _score_soulseek_candidate and _filename_matches_queue_item.
_ORPHAN_AUDIO_EXT_TOKENS = frozenset(
    {"mp3", "flac", "wav", "ogg", "aac", "m4a", "wma", "opus", "aiff"}
)
_ORPHAN_NUM_RE = re.compile(r'^\d{1,4}$')

# Artist names that indicate a compilation/various-artists release.  When both
# the queue item's artist and album_artist are one of these values the
# individual track artist is unknown and any non-empty file artist is accepted
# by the metadata matcher.
_GENERIC_COMPILATION_ARTISTS = frozenset({
    'various artists', 'various artist', 'various', 'va', 'v/a',
    'unknown artist', 'unknown',
    'soundtrack', 'ost',
})

# Strips "feat."/"ft."/"featuring" suffixes from artist strings when building
# fallback search queries so that "KNEECAP feat. Fawzi" becomes "KNEECAP".
_FEAT_SUFFIX_RE = re.compile(
    r'\s+(?:feat\.?|ft\.?|featuring)\s+.*$',
    re.IGNORECASE,
)

# Strip leading disc-track prefixes (e.g. "1-15 - ", "16. ", "07 ") and trailing
# Soulseek UID suffixes (e.g. "_639091010921933965") so that the core title
# and artist can be matched against the cleaned basename.
_TRACK_NUMBER_PREFIX_RE = re.compile(
    r'^(?:\d+-\d+|\d+)\s*[\.\s\-]*\s*',
)
_SOULSEEK_UID_SUFFIX_RE = re.compile(
    r'_\d{12,}$',
)




# Music-directory filesystem index cache (used by check_target_folder_exists)
# so large libraries are not walked repeatedly during batch pre-scans.
_MUSIC_DIR_FILES_CACHE_TTL_SECONDS = 60.0
_music_dir_files_cache: list[str] | None = None
_music_dir_files_cache_ts: float = 0.0

# Downloads-directory file list cache (used by _cleanup_sibling_downloads)
# to avoid repeated recursive walks.
_DOWNLOADS_DIR_FILES_CACHE_TTL_SECONDS = 30.0
_downloads_dir_files_cache: list[str] | None = None
_downloads_dir_files_cache_ts: float = 0.0

# Background pre-scan task tuning
_PRE_SCAN_INTERVAL_SECONDS = 120
_PRE_SCAN_BATCH_SIZE = 50

# Maximum seconds to wait for a Soulseek search to complete.  Polling stops as
# soon as slskd reports is_complete=True, so this is only a safety ceiling for
# searches that never finish (e.g. slskd unreachable mid-search).
_SLSKD_SEARCH_MAX_WAIT_SECONDS = 150

# Reduced maximum wait for fallback queries.  Fallback searches are lower-
# specificity alternatives tried when the primary query found nothing; a
# shorter ceiling limits the worst-case per-track search time.
_SLSKD_FALLBACK_SEARCH_MAX_WAIT_SECONDS = 60


# Cached SlskdClient instance — config does not change at runtime so we build
# the object once and reuse it for the lifetime of the process.
_slskd_client_cache = None


def get_metadata_config():
    """Return metadata extraction configuration."""
    return {
        "enabled": True,
        "enable_flac": True,
        "enable_musicbrainz_tags": True,
        "enable_extended_tags": True,
    }


def get_queue_matching_config_legacy() -> dict:
    """Legacy queue matching config getter (deprecated).
    
    Deprecated: Use get_queue_matching_config_v2() instead.
    """
    cfg = get_config()

    matching = cfg.get("queue_matching", {}) or {}

    return {
        "detect_live_tracks": bool(
            matching.get(
                "detect_live_tracks",
                True,
            )
        ),

        "detect_remix_tracks": bool(
            matching.get(
                "detect_remix_tracks",
                True,
            )
        ),

        "detect_compilations": bool(
            matching.get(
                "detect_compilations",
                True,
            )
        ),

        "title_variant_tokens": set(
            matching.get(
                "title_variant_tokens",
                [
                    "acoustic",
                    "demo",
                    "edit",
                    "instrumental",
                    "intro",
                    "live",
                    "mix",
                    "orchestral",
                    "radio",
                    "remaster",
                    "remastered",
                    "remix",
                    "version",
                ],
            )
        ),

        "soft_variant_tokens": set(
            matching.get(
                "soft_variant_tokens",
                [
                    "version",
                    "edit",
                    "radio",
                ],
            )
        ),

        "compilation_artists": set(
            matching.get(
                "compilation_artists",
                [
                    "various artists",
                    "various artist",
                    "various",
                    "va",
                    "v/a",
                    "unknown artist",
                    "unknown",
                    "soundtrack",
                    "ost",
                ],
            )
        ),
    }

# -----------------------------------------------------------------------------
# Popularity Scoring Configuration
# -----------------------------------------------------------------------------

def get_popularity_weights() -> dict[str, float]:
    """Get popularity scoring weights from config.
    
    Returns:
        Dict with keys 'lastfm', 'listenbrainz', 'age' with float values that sum to 1.0.
        
    Default Values:
        - lastfm: 0.55 (55% weight)
        - listenbrainz: 0.35 (35% weight)
        - age: 0.10 (10% weight)
    """
    cfg = get_config()
    weights = cfg.get("popularity", {}).get("weights", {})
    
    defaults = {
        "lastfm": 0.55,
        "listenbrainz": 0.35,
        "age": 0.10,
    }
    
    result = {
        key: float(weights.get(key, default))
        for key, default in defaults.items()
    }
    
    # Normalize to sum to 1.0
    total = sum(result.values())
    if total > 0:
        result = {k: v / total for k, v in result.items()}
    
    return result


def get_standout_config() -> dict[str, Any]:
    """Get standout track detection and star rating configuration.
    
    Returns:
        Dict containing standout detection thresholds and star rating criteria.
        
    Default Values:
        - album_zscore_threshold: 0.8 (minimum z-score for standout detection)
        - artist_zscore_threshold: 2.2 (minimum z-score for artist-level outliers)
        - artist_top_percentile: 0.10 (top 10% of artist catalog)
        - artist_min_tracks: 10 (minimum tracks required for artist stats)
        - star_5: {"album_z": 1.0, "artist_z": 1.2, "artist_pct": 0.10}
        - star_4: {"album_z": 0.5, "artist_z": 1.0, "artist_pct": 0.20}
        - star_3: {"album_z": -0.5}
        - star_2: {"album_z": -1.2}
        - star_1: {"album_z": -1.2, "default": True}
    """
    cfg = get_config()
    sd_config = cfg.get("single_detection", {})
    
    defaults = {
        "album_zscore_threshold": 0.8,
        "artist_zscore_threshold": 2.2,
        "artist_top_percentile": 0.10,
        "artist_min_tracks": 10,
        "popularity_5star_z_threshold": 2.0,
        "lb_unreliable_5star_threshold": 0.50,
        "listener_5star_z_threshold": 1.0,
        "standout_gap_z": 0.75,
        "star_5": {"album_z": 1.0, "artist_z": 1.2, "artist_pct": 0.10},
        "star_4": {"album_z": 0.5, "artist_z": 1.0, "artist_pct": 0.20},
        "star_3": {"album_z": -0.5},
        "star_2": {"album_z": -1.2},
        "star_1": {"album_z": -1.2, "default": True},
    }

    # Merge user config with defaults
    result = defaults.copy()
    for key in ("album_zscore_threshold", "artist_zscore_threshold",
                "artist_top_percentile", "artist_min_tracks",
                "popularity_5star_z_threshold", "lb_unreliable_5star_threshold",
                "listener_5star_z_threshold",
                "standout_gap_z"):
        if key in sd_config:
            result[key] = sd_config[key]

    for star_key in ("star_5", "star_4", "star_3", "star_2", "star_1"):
        if star_key in sd_config and isinstance(sd_config[star_key], dict):
            result.setdefault(star_key, {}).update(sd_config[star_key])

    return result


# -----------------------------------------------------------------------------
# Genre Aggregation Configuration
# -----------------------------------------------------------------------------

def get_genre_weights() -> dict[str, float]:
    """Get genre source weighting for aggregation.
    
    Returns:
        Dict mapping genre source names to their weights.
        
    Default Values:
        - musicbrainz: 0.40 (40% weight - most authoritative)
        - discogs: 0.25 (25% weight)
        - audiodb: 0.20 (20% weight)
        - essentia: 0.20 (20% weight - audio analysis)
        - lastfm: 0.10 (10% weight)
        - spotify: 0.05 (5% weight)
    """
    cfg = get_config()
    genre_config = cfg.get("genres", {}).get("weights", {})
    
    defaults = {
        "musicbrainz": 0.40,
        "discogs": 0.25,
        "audiodb": 0.20,
        "essentia": 0.20,
        "lastfm": 0.10,
        "spotify": 0.05,
    }
    
    return {
        key: float(genre_config.get(key, default))
        for key, default in defaults.items()
    }


def get_genre_synonyms() -> dict[str, str]:
    """Get genre synonym mappings for normalization.
    
    Returns:
        Dict mapping variant genre names to canonical forms.
        
    Default Mappings:
        - "hip hop" → "hip-hop"
        - "r&b" → "rnb"
        - "rhythm and blues" → "rnb"
    """
    cfg = get_config()
    user_synonyms = cfg.get("genres", {}).get("synonyms", {})
    
    defaults = {
        "hip hop": "hip-hop",
        "r&b": "rnb",
        "rhythm and blues": "rnb",
    }
    
    # User synonyms override defaults
    return {**defaults, **user_synonyms}


# -----------------------------------------------------------------------------
# Queue Matching Configuration
# -----------------------------------------------------------------------------

def get_queue_matching_config_v2() -> dict[str, Any]:
    """Get queue matching thresholds, variant tokens, and compilation settings.
    
    Consolidates legacy ``get_queue_matching_config_legacy()`` into v2.
    The legacy function is deprecated and will be removed.
    
    Config section: ``queue.matching`` in config.yaml
    
    Returns:
        Dict containing matching thresholds, variant configurations,
        compilation detection settings, and track variant tokens.
        
    Default Values:
        - threshold: 0.65
        - partial_match: 0.7
        - strict_duration_sec: 2
        - tolerance_duration_sec: 5
        - soft_variants: {"edit", "radio", "version", "mix"}
        - hard_variants: {"live", "acoustic", "remix", "demo", "instrumental"}
        - detect_live_tracks: True
        - detect_remix_tracks: True
        - detect_compilations: True
        - title_variant_tokens: {acoustic, demo, edit, instrumental, intro,
          live, mix, orchestral, radio, remaster, remastered, remix, version}
        - soft_variant_tokens: {version, edit, radio}
        - compilation_artists: {various artists, various, va, v/a, soundtrack, ost, ...}
    """
    cfg = get_config()
    queue_config = cfg.get("queue", {}).get("matching", {})
    
    return {
        # Matching thresholds
        "threshold": float(queue_config.get("threshold", 0.65)),
        "partial_match": float(queue_config.get("partial_match", 0.7)),
        "strict_duration_sec": int(queue_config.get("strict_duration_sec", 2)),
        "tolerance_duration_sec": int(queue_config.get("tolerance_duration_sec", 5)),
        
        # Variant tokens (used by matching + normalization)
        "soft_variants": set(queue_config.get("soft_variants", ["edit", "radio", "version", "mix"])),
        "hard_variants": set(queue_config.get("hard_variants", ["live", "acoustic", "remix", "demo", "instrumental"])),
        
        # --- Merged from legacy ---
        "detect_live_tracks": bool(queue_config.get("detect_live_tracks", True)),
        "detect_remix_tracks": bool(queue_config.get("detect_remix_tracks", True)),
        "detect_compilations": bool(queue_config.get("detect_compilations", True)),
        
        "title_variant_tokens": set(
            queue_config.get(
                "title_variant_tokens",
                [
                    "acoustic", "demo", "edit", "instrumental", "intro",
                    "live", "mix", "orchestral", "radio",
                    "remaster", "remastered", "remix", "version",
                ],
            )
        ),
        
        "soft_variant_tokens": set(
            queue_config.get(
                "soft_variant_tokens",
                ["version", "edit", "radio"],
            )
        ),
        
        "compilation_artists": set(
            queue_config.get(
                "compilation_artists",
                [
                    "various artists", "various artist", "various",
                    "va", "v/a", "unknown artist", "unknown",
                    "soundtrack", "ost",
                ],
            )
        ),
    }


def get_slskd_timeouts() -> dict[str, Any]:
    """Get slskd transfer timeout configuration.
    
    Returns:
        Dict containing timeout values for various transfer states.
        
    Default Values:
        - min_retry_delay_minutes: 60
        - long_retry_delay_minutes: 1440 (24 hours)
        - remotely_queued_timeout_minutes: 60
        - active_state_timeout_minutes: 240 (4 hours)
        - inter_item_delay_seconds: 5
        - state_timeouts: {...} (per-state timeouts)
    """
    cfg = get_config()
    slskd_config = cfg.get("slskd", {}).get("timeouts", {})
    
    return {
        "min_retry_delay_minutes": int(slskd_config.get("min_retry_delay_minutes", 60)),
        "long_retry_delay_minutes": int(slskd_config.get("long_retry_delay_minutes", 1440)),
        "remotely_queued_timeout_minutes": int(slskd_config.get("remotely_queued_timeout_minutes", 60)),
        "active_state_timeout_minutes": int(slskd_config.get("active_state_timeout_minutes", 240)),
        "inter_item_delay_seconds": int(slskd_config.get("inter_item_delay_seconds", 5)),
        "state_timeouts": slskd_config.get("state_timeouts", {
            "Requested": 30,
            "Queued, Remotely": 60,
            "Queued, Locally": 60,
            "Initializing": 120,
            "InProgress": 240,
            "Queued": 60,
            "In Progress": 240,
            "Downloading": 240,
        }),
    }


# -----------------------------------------------------------------------------
# Last.fm Service Configuration
# -----------------------------------------------------------------------------

def get_lastfm_config() -> dict[str, Any]:
    """Get Last.fm service configuration.
    
    Returns:
        Dict containing Last.fm API settings and cache configuration.
        
    Default Values:
        - min_artist_plays: 20
        - min_similarity_score: 0.46
        - max_similar_per_artist: 5
        - max_albums_per_artist: 5
        - recent_months: 3
        - cache_ttl_hours: 24
        - max_retries: 3
        - retry_backoff: 1.5
        - rate_limit_delay: 0.5
    """
    cfg = get_config()
    lastfm_config = cfg.get("lastfm", {})
    
    return {
        "min_artist_plays": int(lastfm_config.get("min_artist_plays", 20)),
        "min_similarity_score": float(lastfm_config.get("min_similarity_score", 0.46)),
        "max_similar_per_artist": int(lastfm_config.get("max_similar_per_artist", 5)),
        "max_albums_per_artist": int(lastfm_config.get("max_albums_per_artist", 5)),
        "recent_months": int(lastfm_config.get("recent_months", 3)),
        "cache_ttl_hours": int(lastfm_config.get("cache_ttl_hours", 24)),
        "max_retries": int(lastfm_config.get("max_retries", 3)),
        "retry_backoff": float(lastfm_config.get("retry_backoff", 1.5)),
        "rate_limit_delay": float(lastfm_config.get("rate_limit_delay", 0.5)),
    }


# -----------------------------------------------------------------------------
# Download Matching Configuration
# -----------------------------------------------------------------------------

def get_download_matching_config() -> dict[str, Any]:
    """Get download matching engine configuration.
    
    Returns:
        Dict containing download matching thresholds and settings.
        
    Note:
        These settings control how downloaded files are matched to existing
        library tracks and releases.
    """
    cfg = get_config()
    download_config = cfg.get("downloads", {}).get("matching", {})
    
    return {
        "min_accept_score": float(download_config.get("min_accept_score", 0.45)),
        "duration_tolerance_seconds": int(download_config.get("duration_tolerance_seconds", 5)),
        "early_accept_length_tolerance": int(download_config.get("early_accept_length_tolerance", 2)),
        "top_n_candidates": int(download_config.get("top_n_candidates", 50)),
    }


# -----------------------------------------------------------------------------
# Filesystem Configuration
# -----------------------------------------------------------------------------

def get_supported_audio_formats() -> set[str]:
    """Get set of supported audio file extensions.
    
    Returns:
        Set of lowercase file extensions (e.g., {'.mp3', '.flac'}).
        
    Default Formats:
        .mp3, .flac, .m4a, .ogg, .wav, .aac, .wma
    """
    cfg = get_config()
    user_formats = cfg.get("filesystem", {}).get("audio_formats", [])
    
    defaults = {".mp3", ".flac", ".m4a", ".ogg", ".wav", ".aac", ".wma"}
    
    if user_formats:
        # User formats completely override defaults
        return {ext.lower() if ext.startswith('.') else f'.{ext.lower()}' for ext in user_formats}
    
    return defaults


# -----------------------------------------------------------------------------
# Artist Biography Configuration
# -----------------------------------------------------------------------------

def get_musician_terms() -> set[str]:
    """Get terms used to identify musician entities in Wikidata.
    
    Returns:
        Set of occupation/description terms that indicate a musical artist.
        
    Note:
        Used for entity disambiguation when multiple Wikidata results
        are returned for an artist name search.
    """
    cfg = get_config()
    user_terms = cfg.get("wikidata", {}).get("musician_terms", [])
    
    defaults = {
        "singer", "musician", "band", "rapper", "composer", "songwriter",
        "guitarist", "drummer", "bassist", "pianist", "vocalist", "producer",
        "dj", "disc jockey", "recording artist", "musical group", "rock group",
        "pop group", "hip-hop", "hip hop", "jazz", "blues", "country artist",
        "folk singer", "opera", "conductor", "orchestra",
    }
    
    if user_terms:
        return set(user_terms) | defaults
    
    return defaults


# =============================================================================
# Missing config getters for config.html page sections
#
# These bridge the gap between what config_helpers.py exposes and what
# config.html renders.  Every top-level section on the config page should
# have a corresponding ``get_*`` function here.
# =============================================================================


# ---------------------------------------------------------------------------
# API Integrations (api_integrations.*)
# ---------------------------------------------------------------------------

def get_api_integrations() -> dict[str, Any]:
    """Get API integration settings (Spotify, Last.fm, Discogs, etc.).

    Config section: ``api_integrations`` in config.yaml

    Returns:
        Dict of ``{service_name: {enabled, api_key, ...}}``.
    """
    cfg = get_config()
    return cfg.get("api_integrations", {})


def get_api_integration(service: str) -> dict[str, Any]:
    """Get settings for a single API integration.

    Args:
        service: Service name e.g. ``"spotify"``, ``"lastfm"``.

    Returns:
        Dict with ``enabled``, ``api_key``, and service-specific fields.
    """
    return get_api_integrations().get(service, {})


# ---------------------------------------------------------------------------
# qBittorrent (qbittorrent.*)
# ---------------------------------------------------------------------------

def get_qbittorrent_config() -> dict[str, Any]:
    """Get qBittorrent torrent client configuration.

    Config section: ``qbittorrent`` in config.yaml

    Returns:
        Dict with keys ``enabled``, ``web_url``, ``username``,
        ``password``, ``downloads_folder``.
    """
    cfg = get_config()
    qb = cfg.get("qbittorrent", {})
    return {
        "enabled": bool(qb.get("enabled", False)),
        "web_url": str(qb.get("web_url", "") or ""),
        "username": str(qb.get("username", "") or ""),
        "password": str(qb.get("password", "") or ""),
        "downloads_folder": str(qb.get("downloads_folder", "") or ""),
    }


# ---------------------------------------------------------------------------
# slskd / Soulseek basic (slskd.*)
# ---------------------------------------------------------------------------

def get_slskd_config() -> dict[str, Any]:
    """Get slskd (Soulseek) base configuration.

    Config section: ``slskd`` in config.yaml

    Returns:
        Dict with keys ``enabled``, ``web_url``, ``api_key``.
        For timeout settings see ``get_slskd_timeouts()``.
    """
    cfg = get_config()
    s = cfg.get("slskd", {})
    return {
        "enabled": bool(s.get("enabled", False)),
        "web_url": str(s.get("web_url", "") or ""),
        "api_key": str(s.get("api_key", "") or ""),
    }


# ---------------------------------------------------------------------------
# Essentia mood/genre scan (essentia.*)
# ---------------------------------------------------------------------------

def get_essentia_config() -> dict[str, Any]:
    """Get Essentia ML mood/genre scan configuration.

    Config section: ``essentia`` in config.yaml

    Returns:
        Dict with script paths, thresholds, and feature toggles.
    """
    cfg = get_config()
    e = cfg.get("essentia", {})
    return {
        "script_path": str(e.get("script_path", "") or ""),
        "models_dir": str(e.get("models_dir", "") or ""),
        "mood_threshold": float(e.get("mood_threshold", 0.005)),
        "per_file_timeout": int(e.get("per_file_timeout", 300)),
        "json_output_dir": str(e.get("json_output_dir", "") or ""),
        "tag_moods": bool(e.get("tag_moods", True)),
        "parse_json_features": bool(e.get("parse_json_features", True)),
        "delete_json_after_import": bool(e.get("delete_json_after_import", True)),
        "tag_genres": bool(e.get("tag_genres", False)),
        "num_genres": int(e.get("num_genres", 3)),
        "genre_threshold": float(e.get("genre_threshold", 15.0)),
        "genre_format": str(e.get("genre_format", "parent_child")),
    }


# ---------------------------------------------------------------------------
# Downloads base settings (downloads.*) — folder, export, format, conversion
# ---------------------------------------------------------------------------

def get_downloads_config() -> dict[str, Any]:
    """Get base download folder/format settings.

    Config section: ``downloads`` in config.yaml

    Returns:
        Dict with folder paths, naming format, and conversion options.
        For matching thresholds see ``get_download_matching_config()``.
    """
    cfg = get_config()
    d = cfg.get("downloads", {})
    return {
        "folder": str(d.get("folder", "") or ""),
        "external_export_path": str(d.get("external_export_path", "") or ""),
        "file_name_format": str(
            d.get("file_name_format",
                  "{album_artist}/{year} - {album}/{track_number}. {artist} - {title}")
            or ""
        ),
        "quality_filter": d.get("quality_filter", {}),
        "conversion": d.get("conversion", {}),
    }


# ---------------------------------------------------------------------------
# Download quality filter
# ---------------------------------------------------------------------------

def get_download_quality_filter() -> dict[str, Any]:
    """Get download quality filter settings.

    Config section: ``downloads.quality_filter`` in config.yaml

    Returns:
        Dict with ``enabled``, ``reject_others``, ``priorities``,
        ``bitrate_tolerance``.
    """
    cfg = get_config()
    qf = cfg.get("downloads", {}).get("quality_filter", {})
    return {
        "enabled": bool(qf.get("enabled", False)),
        "reject_others": bool(qf.get("reject_others", True)),
        "priorities": qf.get("priorities", []),
        "bitrate_tolerance": int(qf.get("bitrate_tolerance", 5)),
    }


# ---------------------------------------------------------------------------
# Download conversion settings
# ---------------------------------------------------------------------------

def get_download_conversion_config() -> dict[str, Any]:
    """Get download conversion (FLAC→MP3) settings.

    Config section: ``downloads.conversion`` in config.yaml

    Returns:
        Dict with ``enabled``, ``mode``, ``mp3_bitrate_kbps``,
        ``original_handling``, ``original_subfolder``.
    """
    cfg = get_config()
    cv = cfg.get("downloads", {}).get("conversion", {})
    return {
        "enabled": bool(cv.get("enabled", False)),
        "mode": str(cv.get("mode", "flac_to_mp3")),
        "mp3_bitrate_kbps": int(cv.get("mp3_bitrate_kbps", 320)),
        "original_handling": str(cv.get("original_handling", "move_to_original")),
        "original_subfolder": str(cv.get("original_subfolder", "Original")),
    }


# ---------------------------------------------------------------------------
# Generic feature flags (features.*)
# ---------------------------------------------------------------------------

def get_features_config() -> dict[str, Any]:
    """Get all feature flags and toggles.

    Config section: ``features`` in config.yaml

    Returns:
        Dict of feature flags including sync, scheduler, daily-release,
        and mature-track settings.
    """
    cfg = get_config()
    return cfg.get("features", {})


def get_feature(key: str, default: Any = None) -> Any:
    """Get a single feature flag value.

    Args:
        key: Dot-separated path e.g. ``"sync_ratings_to_all_users"``
             or ``"retry_scheduler.interval_seconds"``.
        default: Fallback value.
    """
    cfg = get_features_config()
    parts = key.split(".")
    val: Any = cfg
    for part in parts:
        if isinstance(val, dict):
            val = val.get(part)
        else:
            return default
    return val if val is not None else default


# ---------------------------------------------------------------------------
# Navidrome base config (navidrome.*)  — not per-user, the shared part
# ---------------------------------------------------------------------------

def get_navidrome_base_config() -> dict[str, Any]:
    """Get base Navidrome server settings (not per-user).

    Config section: ``navidrome`` in config.yaml minus per-user overrides.

    Returns:
        Dict of shared Navidrome settings (host, port, protocol).
    """
    n = get_navidrome_config()
    return dict(n)


# ---------------------------------------------------------------------------
# Upcoming releases sources (upcoming_releases_sources or similar)
# ---------------------------------------------------------------------------

def get_upcoming_releases_sources() -> list[dict[str, Any]]:
    """Get Wikipedia scraping sources for upcoming album releases.

    Config section: ``upcoming_releases.sources`` in config.yaml — the same
    key the config page (``static/js/config.js``) reads and writes. The old
    top-level ``upcoming_releases_sources`` key is honoured as a fallback.

    Each source dict should contain:
        - ``key``: Unique source key (e.g. ``"2026_rock"``)
        - ``name``: Display name
        - ``url``: Wikipedia URL
        - ``columns``: Comma-separated column order (day, artist, album, genre)
        - ``enabled``: Whether the source should be scraped (default True)

    Returns:
        List of source dicts.
    """
    cfg = get_config()
    sources = (cfg.get("upcoming_releases") or {}).get("sources")
    if not isinstance(sources, list):
        sources = cfg.get("upcoming_releases_sources", [])
    return sources


# ---------------------------------------------------------------------------
# Search strip keywords (search.strip_keywords or normalization config)
# ---------------------------------------------------------------------------

def get_search_strip_keywords() -> list[str]:
    """Get keywords that trigger parenthetical-content removal during API searches.

    Config section: ``search.strip_keywords`` in config.yaml

    Returns:
        List of keyword strings (e.g. ``["remastered", "radio edit"]``).
    """
    cfg = get_config()
    return cfg.get("search", {}).get("strip_keywords", [
        "remastered", "remaster", "radio edit", "single version",
        "album version", "bonus track", "bonus",
    ])


# ---------------------------------------------------------------------------
# Navidrome users (navidrome_users[])
# ---------------------------------------------------------------------------

def get_navidrome_users() -> list[dict[str, Any]]:
    """Get all configured Navidrome user accounts.

    Each user may include display_name, base_url, user (username), pass,
    spotify_username, spotify_client_id, spotify_client_secret,
    lastfm_username, listenbrainz_user_token.

    Returns:
        List of user dicts.
    """
    cfg = get_config()
    return cfg.get("navidrome_users", [])


# =============================================================================
# MusicBrainz User-Agent
# =============================================================================

def get_musicbrainz_user_agent() -> str:
    """Get the MusicBrainz API User-Agent string.

    Returns:
        User-Agent string identifying the application to MusicBrainz.
        MusicBrainz requires a unique UA for API access.

    Default:
        "Popularr/1.0 +https://github.com/M0VENTURA/Popularr"
    """
    cfg = get_config()
    return str(
        cfg.get("musicbrainz", {})
        .get("user_agent", "Popularr/1.0 +https://github.com/M0VENTURA/Popularr")
    )


# =============================================================================
# AudioDB Configuration
# =============================================================================

def get_audiodb_config() -> dict[str, Any]:
    """Get AudioDB API configuration.

    Config section: ``api_integrations.audiodb`` in config.yaml

    Returns:
        Dict with keys ``api_key`` (str) and ``enabled`` (bool).

    Default API Key:
        "195003" (AudioDB demo/public key)
    """
    cfg = get_config()
    audiodb = cfg.get("api_integrations", {}).get("audiodb", {})
    return {
        "api_key": str(audiodb.get("api_key", "195003") or ""),
        "enabled": bool(audiodb.get("enabled", True)),
    }


# =============================================================================
# Database & State Directories
# =============================================================================

def get_state_directory() -> str:
    """Get the directory for runtime state/progress files.

    Returns:
        Absolute path string.

    Default:
        Value of env var ``SCAN_STATE_DIR``, or ``/database``.
    """
    return os.environ.get("SCAN_STATE_DIR", "/database")


def get_database_path() -> str:
    """Get the SQLite database file path (legacy).

    Returns:
        Absolute path string.

    Default:
        ``/database/popularr.db``
    """
    return os.path.join(get_state_directory(), "popularr.db")


def get_api_rate_limiter_state_file() -> str:
    """Get the file path for API rate limiter state persistence.

    Returns:
        Absolute path string.

    Default:
        ``/database/api_rate_limiter_state.json``
    """
    return os.path.join(get_state_directory(), "api_rate_limiter_state.json")


def get_navidrome_progress_file() -> str:
    """Get the file path for Navidrome scan progress tracking.

    Returns:
        Absolute path string (may be overridden by env var).

    Default:
        ``/database/navidrome_scan_progress.json``
    """
    return os.environ.get(
        "NAVIDROME_PROGRESS_FILE",
        os.path.join(get_state_directory(), "navidrome_scan_progress.json"),
    )


# =============================================================================
# Pre-scan Batch Configuration
# =============================================================================

def get_pre_scan_config() -> dict[str, Any]:
    """Get pre-scan task tuning parameters.

    Config section: ``filesystem.pre_scan`` in config.yaml

    Returns:
        Dict with keys ``interval_seconds``, ``batch_size``.

    Default Values:
        - interval_seconds: 120
        - batch_size: 50
    """
    cfg = get_config()
    pre_scan = cfg.get("filesystem", {}).get("pre_scan", {})
    return {
        "interval_seconds": int(pre_scan.get("interval_seconds", 120)),
        "batch_size": int(pre_scan.get("batch_size", 50)),
    }


# =============================================================================
# Filesystem Cache Configuration
# =============================================================================

def get_filesystem_cache_config() -> dict[str, Any]:
    """Get filesystem cache TTL configuration.

    Config section: ``filesystem.cache`` in config.yaml

    Returns:
        Dict with keys ``music_dir_cache_ttl``, ``downloads_dir_cache_ttl``.

    Default Values:
        - music_dir_cache_ttl: 60.0 seconds
        - downloads_dir_cache_ttl: 30.0 seconds
    """
    cfg = get_config()
    cache = cfg.get("filesystem", {}).get("cache", {})
    return {
        "music_dir_cache_ttl": float(cache.get("music_dir_cache_ttl", 60.0)),
        "downloads_dir_cache_ttl": float(cache.get("downloads_dir_cache_ttl", 30.0)),
    }


# =============================================================================
# Thread Pool Configuration
# =============================================================================

def get_thread_pool_config() -> dict[str, Any]:
    """Get thread pool size configuration.

    Config section: ``system.thread_pools`` in config.yaml

    Returns:
        Dict with keys ``max_workers`` (default: 20).

    Default Values:
        - max_workers: 20
    """
    cfg = get_config()
    pool = cfg.get("system", {}).get("thread_pools", {})
    return {
        "max_workers": int(pool.get("max_workers", 20)),
    }


# =============================================================================
# Scan Pipeline Configuration
# =============================================================================

def get_scan_pipeline_config() -> dict[str, Any]:
    """Get scan pipeline tuning parameters.

    Config section: ``scanning.pipeline`` in config.yaml

    Returns:
        Dict with keys ``batch_size``, ``page_size``, ``max_workers``,
        ``library_sync_batch_size``.

    Default Values:
        - batch_size: 50
        - page_size: 500
        - max_workers: 10
        - library_sync_batch_size: 1000
    """
    cfg = get_config()
    pipeline = cfg.get("scanning", {}).get("pipeline", {})
    return {
        "batch_size": int(pipeline.get("batch_size", 50)),
        "page_size": int(pipeline.get("page_size", 500)),
        "max_workers": int(pipeline.get("max_workers", 10)),
        "library_sync_batch_size": int(pipeline.get("library_sync_batch_size", 1000)),
    }


# =============================================================================
# Matching & Threshold Configuration
# =============================================================================

def get_matching_thresholds() -> dict[str, Any]:
    """Get general matching threshold defaults.

    Config section: ``matching`` in config.yaml

    Returns:
        Dict with keys ``fuzzy_threshold``, ``score_threshold``.

    Default Values:
        - fuzzy_threshold: 0.80
        - score_threshold: 0.60
    """
    cfg = get_config()
    matching = cfg.get("matching", {})
    return {
        "fuzzy_threshold": float(matching.get("fuzzy_threshold", 0.80)),
        "score_threshold": float(matching.get("score_threshold", 0.60)),
    }


# =============================================================================
# Download Quality / Search Configuration
# =============================================================================

def get_search_quality_config() -> dict[str, Any]:
    """Get download search quality defaults.

    Config section: ``downloads.quality`` in config.yaml

    Returns:
        Dict with keys ``min_bitrate``, ``min_sample_rate``,
        ``min_length_seconds``.

    Default Values:
        - min_bitrate: 320
        - min_sample_rate: 44100
        - min_length_seconds: 30
    """
    cfg = get_config()
    quality = cfg.get("downloads", {}).get("quality", {})
    return {
        "min_bitrate": int(quality.get("min_bitrate", 320)),
        "min_sample_rate": int(quality.get("min_sample_rate", 44100)),
        "min_length_seconds": int(quality.get("min_length_seconds", 30)),
    }


# =============================================================================
# Queue Worker Configuration
# =============================================================================

def get_logging_config() -> dict[str, Any]:
    """Get logging configuration.

    Config section: ``logging`` in config.yaml (with a legacy fallback to the
    top-level ``log_level`` key / ``LOG_LEVEL`` env var).

    Returns:
        Dict with key ``level`` — one of ``debug``, ``info``, ``warning``,
        ``error``.  Defaults to ``info`` (debug off).
    """
    cfg = get_config()
    logging_cfg = cfg.get("logging", {}) or {}
    raw = logging_cfg.get("level") or cfg.get("log_level")
    if not raw:
        raw = os.environ.get("LOG_LEVEL") or os.environ.get("SPTNR_LOG_LEVEL") or "info"
    level = str(raw).strip().lower()
    valid = {"debug", "info", "warning", "error", "critical"}
    if level not in valid:
        level = "info"
    return {"level": level}


def get_queue_worker_config() -> dict[str, Any]:
    """Get background queue worker configuration.

    Config section: ``queue.worker`` in config.yaml

    Returns:
        Dict with keys ``interval_seconds``, ``batch_size``,
        ``max_in_flight``.

    Default Values:
        - interval_seconds: 30
        - batch_size: 50
        - max_in_flight: 15
    """
    cfg = get_config()
    worker = cfg.get("queue", {}).get("worker", {})
    return {
        "interval_seconds": int(worker.get("interval_seconds", 30)),
        "batch_size": int(worker.get("batch_size", 50)),
        "max_in_flight": int(worker.get("max_in_flight", 15)),
    }


# =============================================================================
# End of config helpers
# =============================================================================