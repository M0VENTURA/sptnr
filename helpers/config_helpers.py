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
