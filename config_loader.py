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
