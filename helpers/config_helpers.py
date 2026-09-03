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
    """Read a ``POPULARLR_<suffix>`` env var, or return None.

    Reads WITHOUT removing the variable: ``os.environ.pop`` previously
    consumed the value on the first config load, so any later
    ``clear_config_cache()`` (config save from another path, partial saves,
    runtime reloads) silently lost the env overrides and fell back to the
    file's values.  Env overrides are now deterministic — they win on every
    load until the variable is unset, matching how the rest of the app
    treats environment configuration.
    """
    val = os.environ.get(f"{_POPULARLR_PREFIX}{suffix}", "").strip()
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

def get_config() -> dict[str, Any]:
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
        "db_pool_timeout": s.db_pool_timeout,
        "music_root": s.music_root,
        "music_folder": s.music_folder,
        "downloads_folder": s.downloads_folder,
        "config_path": s.config_path,
        "log_path": s.log_path,
        "nav_url": s.nav_url,
        "nav_user": s.nav_user,
        "nav_pass": s.nav_pass,
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

    # ── Layer 4: Sync flat Pydantic keys to nested api_integrations ──
    # Required so standard Docker environment variables map to the nested
    # structures expected by the pipeline APIs (Discogs and Last.fm)
    api_int = cfg.setdefault("api_integrations", {})
    
    if cfg.get("lastfm_api_key") and not api_int.get("lastfm", {}).get("api_key"):
        api_int.setdefault("lastfm", {})["api_key"] = cfg["lastfm_api_key"]
    if cfg.get("lastfm_api_secret") and not api_int.get("lastfm", {}).get("api_secret"):
        api_int.setdefault("lastfm", {})["api_secret"] = cfg["lastfm_api_secret"]
        
    if cfg.get("discogs_token") and not api_int.get("discogs", {}).get("token"):
        api_int.setdefault("discogs", {})["token"] = cfg["discogs_token"]

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
    """Return Navidrome connection settings.

    Delegates to :func:`get_navidrome_first_user`, which resolves the first
    USABLE Navidrome account in priority order:

      1. First user in ``navidrome_users`` with base_url + user + pass set —
         this is where the config UI and setup wizard write real credentials.
      2. The legacy top-level ``navidrome`` dict — but ONLY when it carries
         real connection fields.  Since the config UI also persists
         operational flags there (``auto_public_playlists``,
         ``playlist_cover_art``), a bare ``navidrome:`` section with no
         ``base_url`` must NOT be returned — otherwise callers build a
         Navidrome client with ``base_url=None`` and every request fails
         with "Navidrome base_url is empty".
      3. The flat settings/env keys (``nav_url``/``nav_user``/``nav_pass``,
         i.e. ``POPULARLR_NAV_URL`` etc).

    Returns:
        Dict with ``base_url``/``user``/``pass`` keys, or ``{}`` when no
        usable Navidrome connection is configured.
    """
    return get_navidrome_first_user()


def needs_setup(cfg: dict | None = None) -> bool:
    """True while first-run setup hasn't been completed (no usable Navidrome).

    Setup is considered complete once a Navidrome user with ``base_url`` +
    ``user`` + ``pass`` exists, either in the multi-user ``navidrome_users``
    list, the legacy single-user ``navidrome`` dict, or the flat settings/env
    keys (``nav_url`` / ``nav_user`` / ``nav_pass``).  This mirrors the
    legacy ``_needs_setup`` helper so the auth gate and the setup wizard
    agree on whether the first-run flow is still pending.
    """
    cfg = cfg if cfg is not None else get_config()
    nav_users = cfg.get("navidrome_users", [])
    if isinstance(nav_users, list) and nav_users:
        first = nav_users[0]
        return not all([first.get("base_url"), first.get("user"), first.get("pass")])
    nav = cfg.get("navidrome", {}) or {}
    if nav:
        return not all([nav.get("base_url"), nav.get("user"), nav.get("pass")])
    # Settings/env-provided flat keys (POPULARLR_NAV_URL / _USER / _PASS).
    return not all([cfg.get("nav_url"), cfg.get("nav_user"), cfg.get("nav_pass")])


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
# shorter ceiling limits the worst-case per-track search time.  Kept SHORT
# (20s) — the fallback explosion guard caps queries per track, and each
# fallback waiting 60s still burned 10-60 min/track for un-locatable
# (often pre-release) tracks in the search log.
_SLSKD_FALLBACK_SEARCH_MAX_WAIT_SECONDS = 20


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
        "artist_top_percentile_large": 0.25,
        "artist_catalog_large_threshold": 30,
        "artist_medium_bump_percentile": 0.20,
        "artist_min_tracks": 10,
        "popularity_5star_z_threshold": 2.0,
        "lb_unreliable_5star_threshold": 0.50,
        "listener_5star_z_threshold": 1.0,
        "standout_gap_z": 0.75,
        "star_epsilon_score_points": 0.5,
        "live_4star_requires_single": True,
        "star_5": {"album_z": 1.0, "artist_z": 1.2, "artist_pct": 0.10},
        "star_4": {"album_z": 0.5, "artist_z": 1.0, "artist_pct": 0.20},
        "star_3": {"album_z": -0.5},
        "star_2": {"album_z": -1.2},
        "star_1": {"album_z": -1.2, "default": True},
    }

    # Merge user config with defaults
    result = defaults.copy()
    for key in ("album_zscore_threshold", "artist_zscore_threshold",
                "artist_top_percentile", "artist_top_percentile_large",
                "artist_catalog_large_threshold",
                "artist_medium_bump_percentile",
                "artist_min_tracks",
                "popularity_5star_z_threshold", "lb_unreliable_5star_threshold",
                "listener_5star_z_threshold",
                "standout_gap_z", "star_epsilon_score_points",
                "live_4star_requires_single"):
        if key in sd_config:
            result[key] = sd_config[key]

    for star_key in ("star_5", "star_4", "star_3", "star_2", "star_1"):
        if star_key in sd_config and isinstance(sd_config[star_key], dict):
            result.setdefault(star_key, {}).update(sd_config[star_key])

    # The 3-step album scaling model (era rules + boundaries) is not a star
    # tier — pass the whole block through so era gating in finalise_stage
    # honours config.html values.  Without this, every saved era setting
    # (catalog top %, album top N, max 5★ slots, era boundaries) was silently
    # dropped and the scan always used the hardcoded defaults.
    if isinstance(sd_config.get("album_scaling"), dict):
        result["album_scaling"] = sd_config["album_scaling"]

    return result


# -----------------------------------------------------------------------------
# Tag Writing Configuration
# -----------------------------------------------------------------------------

def get_tagging_config() -> dict[str, Any]:
    """Get file-tag writing policy from the ``tagging`` config block.

    Lets Popularr act as a read-only database scanner / UI when the user
    runs an external tagger (Beets, MusicBrainz Picard) or the music folder
    is on a read-only/network mount:

    ```yaml
    tagging:
      write_tags_to_file: true        # master toggle: touch audio files at all
      skip_unchanged_ratings: true    # skip rating tag writes + Navidrome syncs
                                      # when the scan's star rating is unchanged
                                      # (biggest disk-write source in scans)
      write_options:
        ratings_only: false           # only write POPM/RATING, never text frames
        fill_missing_only: false      # only fill frames that are currently empty
        embed_lyrics: false           # write lyrics to USLT/SYLT frames
      preserve_file_timestamps: true  # restore mtime/atime after a write
    ```

    Defaults preserve the legacy behaviour (writes enabled, timestamps
    preserved); the config page exposes all of them.
    """
    cfg = get_config() or {}
    tagging = cfg.get("tagging") or {}
    if not isinstance(tagging, dict):
        tagging = {}
    opts = tagging.get("write_options") or {}
    if not isinstance(opts, dict):
        opts = {}
    try:
        write_enabled = bool(tagging.get("write_tags_to_file", True))
    except Exception:
        write_enabled = True
    return {
        "write_tags_to_file": write_enabled,
        "skip_unchanged_ratings": bool(tagging.get("skip_unchanged_ratings", True)),
        "sync_album_tags_on_scan": bool(tagging.get("sync_album_tags_on_scan", True)),
        "ratings_only": bool(opts.get("ratings_only", False)),
        "fill_missing_only": bool(opts.get("fill_missing_only", False)),
        "embed_lyrics": bool(opts.get("embed_lyrics", False)),
        "preserve_file_timestamps": bool(tagging.get("preserve_file_timestamps", True)),
    }


# -----------------------------------------------------------------------------
# Metadata Update Configuration
# -----------------------------------------------------------------------------

_DEFAULT_METADATA_UPDATE_FIELDS = {
    "album_name": False,
    "year": False,
    "album_artist": False,
    "genres": False,
    "cover_art": False,
    "lyrics": False,
}


def get_metadata_update_config() -> dict[str, Any]:
    """Get the "Updating Metadata" scan-behaviour config block.

    Controls whether the popularity scan rewrites metadata and, when it
    does, whether the write goes to the DATABASE only or also to the audio
    FILES (``update_on_files`` per field).

    ```yaml
    metadata_update:
      album_name_source: album        # album | release
      album_name_update_target: db    # db | files
      update_on_files:
        album_name: false
        year: false
        album_artist: false
        genres: false
        cover_art: false
        lyrics: false
    
