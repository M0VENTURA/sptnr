#!/usr/bin/env python3
"""
Centralized logging configuration for sptnr.

This module provides a unified logging setup with three log levels:
1. unified_scan.log - Basic operational logs (INFO level) for dashboard viewing
2. info.log - All requests and operations (INFO level) 
3. debug.log - Detailed debug information (DEBUG level)
"""

import os
import logging
import logging.handlers
from datetime import datetime

# Configuration
LOG_DIR = os.environ.get("LOG_PATH", "/config")
if not LOG_DIR.endswith("/"):
    LOG_DIR = os.path.dirname(LOG_DIR) if os.path.isfile(LOG_DIR) else LOG_DIR

# Log file paths
UNIFIED_LOG_PATH = os.path.join(LOG_DIR, "unified_scan.log")
INFO_LOG_PATH = os.path.join(LOG_DIR, "info.log")
DEBUG_LOG_PATH = os.path.join(LOG_DIR, "debug.log")

# Ensure log directory exists
os.makedirs(LOG_DIR, exist_ok=True)

# Log rotation settings (keep 7 days of logs)
# Using time-based rotation: one file per day, keep 7 days
BACKUP_COUNT = 7  # Keep 7 daily log files (7 days of history)

# Logger names
UNIFIED_LOGGER = "unified"
INFO_LOGGER = "info"
DEBUG_LOGGER = "debug"


class ServicePrefixFormatter(logging.Formatter):
    """Formatter that adds a service prefix to log messages."""
    
    def __init__(self, prefix, fmt=None):
        super().__init__(fmt or '%(asctime)s [%(levelname)s] %(message)s')
        self.prefix = prefix
    
    def format(self, record):
        # Only add prefix if message doesn't already have it
        if not record.msg.startswith(self.prefix):
            record.msg = f"{self.prefix}{record.msg}"
        return super().format(record)


class UnifiedLogFilter(logging.Filter):
    """
    Filter for unified log - only allows basic operational messages.
    Filters out verbose debug info and HTTP requests.
    """
    
    def filter(self, record):
        # Filter out HTTP request logs
        if 'GET /api/' in record.getMessage() or 'POST /api/' in record.getMessage():
            return False
        if '"GET' in record.getMessage() or '"POST' in record.getMessage():
            return False
        # Filter out very verbose debug messages
        if '[DEBUG]' in record.getMessage() or '[VERBOSE]' in record.getMessage():
            return False
        return True


def setup_logging(service_name="sptnr"):
    """
    Set up logging with three handlers: unified, info, and debug.
    
    Args:
        service_name: Prefix to add to log messages
        
    Returns:
        tuple: (unified_logger, info_logger, debug_logger)
    """
    prefix = f"{service_name}_"
    
    # Create formatters
    standard_formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
    prefix_formatter = ServicePrefixFormatter(prefix)
    
    # --- Unified Logger (basic operations only) ---
    unified_logger = logging.getLogger(UNIFIED_LOGGER)
    unified_logger.setLevel(logging.INFO)
    unified_logger.propagate = False
    
    if not unified_logger.handlers:
        unified_handler = logging.handlers.TimedRotatingFileHandler(
            UNIFIED_LOG_PATH,
            when='midnight',
            interval=1,
            backupCount=BACKUP_COUNT
        )
        unified_handler.setFormatter(standard_formatter)
        unified_handler.addFilter(UnifiedLogFilter())
        unified_logger.addHandler(unified_handler)
    
    # --- Info Logger (all non-Flask requests and operations) ---
    info_logger = logging.getLogger(INFO_LOGGER)
    info_logger.setLevel(logging.INFO)
    info_logger.propagate = False
    
    if not info_logger.handlers:
        info_handler = logging.handlers.TimedRotatingFileHandler(
            INFO_LOG_PATH,
            when='midnight',
            interval=1,
            backupCount=BACKUP_COUNT
        )
        info_handler.setFormatter(prefix_formatter)
        info_logger.addHandler(info_handler)
    
    # --- Debug Logger (all debug information) ---
    debug_logger = logging.getLogger(DEBUG_LOGGER)
    debug_logger.setLevel(logging.DEBUG)
    debug_logger.propagate = False
    
    if not debug_logger.handlers:
        debug_handler = logging.handlers.TimedRotatingFileHandler(
            DEBUG_LOG_PATH,
            when='midnight',
            interval=1,
            backupCount=BACKUP_COUNT
        )
        debug_handler.setFormatter(prefix_formatter)
        debug_logger.addHandler(debug_handler)
    
    return unified_logger, info_logger, debug_logger


def get_loggers():
    """
    Get or create the three main loggers.
    
    Returns:
        tuple: (unified_logger, info_logger, debug_logger)
    """
    unified_logger = logging.getLogger(UNIFIED_LOGGER)
    info_logger = logging.getLogger(INFO_LOGGER)
    debug_logger = logging.getLogger(DEBUG_LOGGER)
    
    # Set up if not already configured
    if not unified_logger.handlers:
        setup_logging()
        unified_logger = logging.getLogger(UNIFIED_LOGGER)
        info_logger = logging.getLogger(INFO_LOGGER)
        debug_logger = logging.getLogger(DEBUG_LOGGER)
    
    return unified_logger, info_logger, debug_logger


def log_unified(msg, level=logging.INFO):
    """
    Log to unified_scan.log - basic operational messages only.
    
    Args:
        msg: Message to log
        level: Log level (default INFO)
    """
    unified_logger, _, _ = get_loggers()
    unified_logger.log(level, msg)
    # Flush to ensure message is written
    for handler in unified_logger.handlers:
        try:
            handler.flush()
        except Exception:
            pass


def log_info(msg, level=logging.INFO):
    """
    Log to info.log - all requests and operations.
    
    Args:
        msg: Message to log
        level: Log level (default INFO)
    """
    _, info_logger, _ = get_loggers()
    info_logger.log(level, msg)


def log_debug(msg, level=logging.DEBUG):
    """
    Log to debug.log - detailed debugging information.
    
    Args:
        msg: Message to log
        level: Log level (default DEBUG)
    """
    _, _, debug_logger = get_loggers()
    debug_logger.log(level, msg)


def log_all(msg, level=logging.INFO):
    """
    Log to all three logs (unified, info, debug).
    Use for important messages that should appear everywhere.
    
    Args:
        msg: Message to log
        level: Log level (default INFO)
    """
    log_unified(msg, level)
    log_info(msg, level)
    log_debug(msg, level)


# Initialize loggers on module import
setup_logging()
