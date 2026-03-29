"""
Cached configuration helpers to avoid redundant YAML file reads.

This module provides cached config loaders to eliminate the 30+ duplicate
config loading blocks scattered throughout app.py and other modules.
"""

import os
import yaml
import functools
from typing import Dict, Any, Tuple


# Default config path
CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/config.yaml")
DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "config.yaml")


def _read_yaml(path: str) -> Tuple[Dict[str, Any], str]:
    """
    Read and parse a YAML file.
    
    Args:
        path: Path to YAML file
        
    Returns:
        Tuple of (parsed_dict, raw_content_string)
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
            return yaml.safe_load(content) or {}, content
    except FileNotFoundError:
        return {}, ""
    except yaml.YAMLError:
        return {}, ""


@functools.lru_cache(maxsize=1)
def get_config() -> Dict[str, Any]:
    """
    Get the full configuration dict with caching.
    
    This is cached to avoid repeated file I/O. The cache is cleared
    automatically when config is updated via update_config().
    
    Returns:
        Dict containing full config from config.yaml
    """
    config, _ = _read_yaml(CONFIG_PATH)
    
    # If no config found, try default location
    if not config:
        config, _ = _read_yaml(DEFAULT_CONFIG_PATH)
    
    # If still no config, return minimal defaults
    if not config:
        config = {
            "navidrome": {"base_url": "", "user": "", "pass": ""},
            "api_integrations": {},
            "features": {}
        }
    
    return config


@functools.lru_cache(maxsize=1)
def get_navidrome_config() -> Dict[str, Any]:
    """
    Get Navidrome configuration with caching.
    
    This replaces 30+ instances of:
        cfg, _ = _read_yaml(CONFIG_PATH)
        navidrome_config = cfg.get("navidrome", {})
    
    Returns:
        Dict containing navidrome config section
    """
    config = get_config()
    return config.get("navidrome", {})


def get_api_integrations_config() -> Dict[str, Any]:
    """
    Get API integrations configuration.
    
    Returns:
        Dict containing api_integrations config section
    """
    config = get_config()
    return config.get("api_integrations", {})


def get_features_config() -> Dict[str, Any]:
    """
    Get features configuration.
    
    Returns:
        Dict containing features config section
    """
    config = get_config()
    return config.get("features", {})


def clear_config_cache():
    """
    Clear the configuration cache to force reload on next access.
    
    Call this after updating config.yaml to ensure fresh data is loaded.
    """
    get_config.cache_clear()
    get_navidrome_config.cache_clear()


def update_config(updates: Dict[str, Any]) -> bool:
    """
    Update configuration and clear cache.
    
    Args:
        updates: Dictionary of updates to merge into config
        
    Returns:
        True if successful, False otherwise
    """
    try:
        # Read current config
        current_config, _ = _read_yaml(CONFIG_PATH)
        
        # Merge updates
        for key, value in updates.items():
            if isinstance(value, dict) and key in current_config and isinstance(current_config[key], dict):
                # Deep merge for nested dicts
                current_config[key].update(value)
            else:
                current_config[key] = value
        
        # Write back
        with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
            yaml.safe_dump(current_config, f, default_flow_style=False, allow_unicode=True)
        
        # Clear cache to force reload
        clear_config_cache()
        
        return True
    except Exception as e:
        import logging
        logging.error(f"Failed to update config: {e}")
        return False


# ============================================================================
# SERVICE-LEVEL CONFIGURATION HELPERS
# ============================================================================
# These helpers centralize access to individual service configurations,
# providing a consistent pattern for checking enabled/disabled status and
# retrieving service-specific settings.


def get_service_config(service: str) -> Dict[str, Any]:
    """
    Get configuration for a specific service.
    
    Args:
        service: Service name (e.g., 'spotify', 'lastfm', 'discogs', 'navidrome', 'slskd')
    
    Returns:
        Dict containing service config, or empty dict if not found
    
    Usage:
        spotify_config = get_service_config('spotify')
        if spotify_config.get('enabled'):
            client_id = spotify_config.get('client_id')
    """
    service = service.lower().strip()
    
    # Handle navidrome specially since it's at root level
    if service == 'navidrome':
        return get_navidrome_config()
    
    # For other services, check api_integrations first
    integrations = get_api_integrations_config()
    if service in integrations:
        return integrations.get(service, {})
    
    # Check top-level config for qbittorrent, slskd, downloads, etc.
    config = get_config()
    if service in config:
        return config.get(service, {})
    
    return {}


def is_service_enabled(service: str) -> bool:
    """
    Check whether a service is enabled.
    
    Args:
        service: Service name (e.g., 'spotify', 'lastfm', 'navidrome', 'slskd')
    
    Returns:
        True if service is enabled, False otherwise
    
    Usage:
        if is_service_enabled('spotify'):
            # Use Spotify API
    """
    config = get_service_config(service)
    
    # Most services use 'enabled' flag
    if 'enabled' in config:
        return bool(config.get('enabled'))
    
    # If no explicit 'enabled' flag, check for required fields
    # (implies service is configured if credentials exist)
    required_fields = {
        'spotify': ['client_id', 'client_secret'],
        'lastfm': ['api_key'],
        'discogs': ['token'],
        'musicbrainz': [],  # Always enabled by default
    }
    
    if service in required_fields:
        required = required_fields[service]
        return all(config.get(field) for field in required) if required else True
    
    return False


def get_service_api_key(service: str, key_name: str = None) -> str:
    """
    Get an API key for a service.
    
    Args:
        service: Service name
        key_name: Specific key name (e.g., 'client_id', 'api_key', 'token')
                 If not provided, attempts common names in order
    
    Returns:
        API key value, or empty string if not found
    
    Usage:
        key = get_service_api_key('spotify', 'client_id')
        # or
        key = get_service_api_key('lastfm')  # looks for 'api_key' by default
    """
    config = get_service_config(service)
    service = service.lower().strip()
    
    if key_name:
        return (config.get(key_name) or "").strip()
    
    # Common API key field names by service
    common_keys = {
        'spotify': ['client_id'],
        'lastfm': ['api_key', 'key'],
        'discogs': ['token', 'api_key'],
        'musicbrainz': ['api_key'],
        'slskd': ['api_key', 'key'],
        'qbittorrent': ['password', 'api_token'],
        'audiodb': ['api_key', 'key'],
        'youtube': ['api_key', 'key'],
        'google': ['api_key', 'key'],
    }
    
    # Try common keys for this service
    for key in common_keys.get(service, ['api_key', 'token']):
        value = (config.get(key) or "").strip()
        if value:
            return value
    
    return ""


def get_all_services_status() -> Dict[str, Dict[str, Any]]:
    """
    Get status (enabled/disabled) for all configured services.
    
    Returns:
        Dict mapping service name to status info:
        {
            'spotify': {'enabled': True, 'configured': True},
            'lastfm': {'enabled': True, 'configured': False},
            ...
        }
    
    Usage:
        status = get_all_services_status()
        for service, info in status.items():
            if info['enabled']:
                # Service is enabled
    """
    services = {}
    
    config = get_config()
    integrations = config.get('api_integrations', {})
    
    # Check api_integrations
    for service_name in integrations:
        service_config = integrations[service_name]
        services[service_name] = {
            'enabled': bool(service_config.get('enabled', False)),
            'configured': bool(service_config.get('enabled', False) or service_config),
            'config_section': 'api_integrations'
        }
    
    # Check top-level services
    top_level = ['navidrome', 'slskd', 'qbittorrent', 'downloads', 'watcher']
    for service_name in top_level:
        if service_name in config:
            service_config = config[service_name]
            services[service_name] = {
                'enabled': bool(service_config.get('enabled', True if service_name == 'navidrome' else False)),
                'configured': bool(service_config),
                'config_section': 'root'
            }
    
    return services
