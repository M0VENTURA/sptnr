#!/usr/bin/env python3
"""
Centralized configuration loader for SPTNR.
Loads settings from config.yaml and provides helper functions to access them.
"""

import os
import yaml
from typing import Optional, Dict, Any

_CONFIG_CACHE: Optional[Dict[str, Any]] = None
_CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/config.yaml")


def load_config(force_reload: bool = False) -> Dict[str, Any]:
    """
    Load configuration from config.yaml.
    
    Args:
        force_reload: If True, reload config from disk even if cached
        
    Returns:
        Dict containing configuration, or empty dict if file doesn't exist
    """
    global _CONFIG_CACHE
    
    if not force_reload and _CONFIG_CACHE is not None:
        return _CONFIG_CACHE
    
    config_path = os.environ.get("CONFIG_PATH", "/config/config.yaml")
    if not os.path.exists(config_path):
        _CONFIG_CACHE = {}
        return _CONFIG_CACHE
    
    try:
        with open(config_path, "r") as f:
            _CONFIG_CACHE = yaml.safe_load(f) or {}
            return _CONFIG_CACHE
    except Exception:
        _CONFIG_CACHE = {}
        return _CONFIG_CACHE


def get_api_key(service: str, key_name: str = "api_key") -> str:
    """
    Get API key for a service from config.yaml.
    
    Args:
        service: Service name (e.g., 'spotify', 'lastfm', 'discogs')
        key_name: Key name in the service config (default: 'api_key')
        
    Returns:
        API key string, or empty string if not found
    """
    config = load_config()
    api_integrations = config.get("api_integrations", {})
    service_config = api_integrations.get(service, {})
    return service_config.get(key_name, "")


def is_api_enabled(service: str) -> bool:
    """
    Check if an API service is enabled in config.yaml.
    
    Args:
        service: Service name (e.g., 'spotify', 'lastfm', 'discogs')
        
    Returns:
        True if enabled, False otherwise
    """
    config = load_config()
    api_integrations = config.get("api_integrations", {})
    service_config = api_integrations.get(service, {})
    return bool(service_config.get("enabled", False))


def get_weights() -> Dict[str, float]:
    """
    Get scoring weights from config.yaml.
    
    Returns:
        Dict with keys: 'spotify', 'lastfm', 'listenbrainz', 'age'
    """
    config = load_config()
    weights = config.get("weights", {})
    return {
        "spotify": float(weights.get("spotify", 0.4)),
        "lastfm": float(weights.get("lastfm", 0.3)),
        "listenbrainz": float(weights.get("listenbrainz", 0.2)),
        "age": float(weights.get("age", 0.1)),
    }


def get_feature(feature_name: str, default: Any = None) -> Any:
    """
    Get a feature setting from config.yaml.
    
    Args:
        feature_name: Name of the feature setting
        default: Default value if not found
        
    Returns:
        Feature value or default
    """
    config = load_config()
    features = config.get("features", {})
    return features.get(feature_name, default)


def get_watcher_settings() -> Dict[str, Any]:
    """
    Get watcher service settings from config.yaml.
    
    Returns:
        Dict with watcher settings or defaults
    """
    config = load_config()
    watcher = config.get("watcher", {})
    return {
        "scan_interval": int(watcher.get("scan_interval", 30)),
        "navidrome_sync_wait": int(watcher.get("navidrome_sync_wait", 600)),
        "auto_import_enabled": bool(watcher.get("auto_import_enabled", True)),
        "auto_popularity_scan": bool(watcher.get("auto_popularity_scan", True)),
        "downloads_watcher_enabled": bool(watcher.get("downloads_watcher_enabled", True)),
    }


def get_retry_scheduler_settings() -> Dict[str, Any]:
    """
    Get download retry scheduler settings from config.yaml.
    
    Returns:
        Dict with retry scheduler settings or defaults
    """
    config = load_config()
    retry_scheduler = config.get("features", {}).get("retry_scheduler", {})
    return {
        "interval_seconds": int(retry_scheduler.get("interval_seconds", 60)),
        "auto_start": bool(retry_scheduler.get("auto_start", True)),
    }


def update_retry_scheduler_settings(settings: Dict[str, Any]) -> bool:
    """
    Update download retry scheduler settings in config.yaml.
    
    Args:
        settings: Dict with retry scheduler settings to update
        
    Returns:
        True if successful, False otherwise
    """
    try:
        config = load_config()
        
        # Ensure nested dicts exist
        if "features" not in config:
            config["features"] = {}
        if "retry_scheduler" not in config["features"]:
            config["features"]["retry_scheduler"] = {}
        
        # Update settings
        config["features"]["retry_scheduler"].update(settings)
        
        # Save config
        return save_config(config)
    except Exception:
        return False


def get_download_retry_settings() -> Dict[str, Any]:
    """
    Get download retry settings from config.yaml.
    
    Returns:
        Dict with retry settings or defaults
    """
    config = load_config()
    retry = config.get("downloads", {}).get("retry", {})
    return {
        "enabled": bool(retry.get("enabled", True)),
        "max_retries": int(retry.get("max_retries", 3)),
        "retry_interval_minutes": int(retry.get("retry_interval_minutes", 10)),
        "auto_scheduler_enabled": bool(retry.get("auto_scheduler_enabled", True)),
        "auto_scheduler_interval_minutes": int(retry.get("auto_scheduler_interval_minutes", 10)),
    }


def update_download_retry_settings(settings: Dict[str, Any]) -> bool:
    """
    Update download retry settings in config.yaml.
    
    Args:
        settings: Dict with retry settings to update
        
    Returns:
        True if successful, False otherwise
    """
    try:
        config = load_config()
        
        # Ensure nested dicts exist
        if "downloads" not in config:
            config["downloads"] = {}
        if "retry" not in config["downloads"]:
            config["downloads"]["retry"] = {}
        
        # Update settings
        config["downloads"]["retry"].update(settings)
        
        # Save config
        return save_config(config)
    except Exception:
        return False


def save_config(config_data: Dict[str, Any]) -> bool:
    """
    Save configuration to config.yaml.
    
    Args:
        config_data: Configuration dictionary to save
        
    Returns:
        True if successful, False otherwise
    """
    try:
        with open(_CONFIG_PATH, "w") as f:
            yaml.dump(config_data, f, default_flow_style=False, sort_keys=False)
        # Clear cache to force reload
        global _CONFIG_CACHE
        _CONFIG_CACHE = None
        return True
    except Exception:
        return False
