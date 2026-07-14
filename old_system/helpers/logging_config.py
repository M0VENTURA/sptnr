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
import time

# Configuration
LOG_DIR = os.environ.get("LOG_PATH", "/config")
if not LOG_DIR.endswith("/"):
    LOG_DIR = os.path.dirname(LOG_DIR) if os.path.isfile(LOG_DIR) else LOG_DIR

# Log file paths
UNIFIED_LOG_PATH = os.path.join(LOG_DIR, "unified_scan.log")
INFO_LOG_PATH = os.path.join(LOG_DIR, "info.log")
DEBUG_LOG_PATH = os.path.join(LOG_DIR, "debug.log")

# Ensure log directory exists with fallback for permission errors
try:
    os.makedirs(LOG_DIR, exist_ok=True)
except (PermissionError, OSError) as e:
    # If we can't create the default log directory (e.g., /config),
    # fall back to a local directory for development/testing
    script_dir = os.path.dirname(os.path.abspath(__file__))
    LOG_DIR = os.path.join(script_dir, "logs")
    
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
    except (PermissionError, OSError) as fallback_error:
        # If we can't even create a local logs directory, raise a clear error
        raise RuntimeError(
            f"Unable to create log directory. Tried '{os.environ.get('LOG_PATH', '/config')}' "
            f"(failed with {type(e).__name__}: {e}) and fallback '{LOG_DIR}' "
            f"(failed with {type(fallback_error).__name__}: {fallback_error}). "
            f"Please set LOG_PATH environment variable to a writable directory."
        ) from fallback_error
    
    # Update log file paths with fallback directory
    UNIFIED_LOG_PATH = os.path.join(LOG_DIR, "unified_scan.log")
    INFO_LOG_PATH = os.path.join(LOG_DIR, "info.log")
    DEBUG_LOG_PATH = os.path.join(LOG_DIR, "debug.log")

# Log rotation settings (keep 7 days of logs)
# Using time-based rotation: one file per day, keep 7 days
BACKUP_COUNT = 7  # Keep 7 daily log files (7 days of history)

# Logger names
UNIFIED_LOGGER = "unified"
INFO_LOGGER = "info"
DEBUG_LOGGER = "debug"


class ServicePrefixFormatter(logging.Formatter):
    """Formatter that adds a service prefix to log messages and uses proper timezone handling."""
    
    def __init__(self, prefix, fmt=None):
        super().__init__(fmt or '%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
        self.prefix = prefix
    
    def formatTime(self, record, datefmt=None):
        """
        Override formatTime to use local system time with proper timezone handling.
        Uses time.localtime to ensure the timestamp reflects the system's local timezone.
        """
        ct = time.localtime(record.created)
        if datefmt:
            s = time.strftime(datefmt, ct)
        else:
            t = time.strftime(self.default_time_format, ct)
            s = self.default_msec_format % (t, record.msecs)
        return s
    
    def format(self, record):
        # Only add prefix if message doesn't already have it
        if not record.msg.startswith(self.prefix):
            record.msg = f"{self.prefix}{record.msg}"
        return super().format(record)


class UnifiedLogFormatter(logging.Formatter):
    """Formatter for unified log with proper timezone handling."""
    
    def __init__(self):
        super().__init__('%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    
    def formatTime(self, record, datefmt=None):
        """
        Override formatTime to use local system time with proper timezone handling.
        Uses time.localtime to ensure the timestamp reflects the system's local timezone.
        """
        ct = time.localtime(record.created)
        if datefmt:
            s = time.strftime(datefmt, ct)
        else:
            t = time.strftime(self.default_time_format, ct)
            s = self.default_msec_format % (t, record.msecs)
        return s


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
    
    # Create formatters with millisecond precision for better timing tracking
    unified_formatter = UnifiedLogFormatter()
    prefix_formatter = ServicePrefixFormatter(prefix, fmt='%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s')
    
    # --- Unified Logger (basic operations only) ---
    unified_logger = logging.getLogger(UNIFIED_LOGGER)
    unified_logger.setLevel(logging.INFO)
    unified_logger.propagate = False
    
    if not unified_logger.handlers:
        unified_handler = logging.handlers.TimedRotatingFileHandler(
            UNIFIED_LOG_PATH,
            when='midnight',
            interval=1,
            backupCount=BACKUP_COUNT,
            encoding='utf-8'
        )
        unified_handler.setFormatter(unified_formatter)
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
            backupCount=BACKUP_COUNT,
            encoding='utf-8'
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
            backupCount=BACKUP_COUNT,
            encoding='utf-8'
        )
        debug_handler.setFormatter(prefix_formatter)
        debug_logger.addHandler(debug_handler)
        # Add a test/info log entry with a Unicode star to verify correct display
        info_logger = logging.getLogger(INFO_LOGGER)
        info_logger.info('Logging Unicode test: ★ (U+2605)')
    
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


def get_unified_log_targets():
    """Return candidate file paths currently used by the unified logger."""
    unified_logger, _, _ = get_loggers()
    paths = []

    for handler in unified_logger.handlers:
        base = getattr(handler, 'baseFilename', None)
        if base and base not in paths:
            paths.append(base)

    if UNIFIED_LOG_PATH not in paths:
        paths.append(UNIFIED_LOG_PATH)

    return paths


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

    # Safety net: append directly only when no handlers are present.
    # This avoids duplicate lines when handlers are healthy.
    if not unified_logger.handlers:
        try:
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
            level_name = logging.getLevelName(level)
            line = f"{timestamp} [{level_name}] {msg}\n"
            target_paths = get_unified_log_targets()
            primary_path = target_paths[0] if target_paths else UNIFIED_LOG_PATH
            with open(primary_path, 'a', encoding='utf-8') as fh:
                fh.write(line)
        except Exception:
            # Never raise from logging path.
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
    # Flush to ensure message is written immediately (for real-time monitoring)
    for handler in info_logger.handlers:
        try:
            handler.flush()
        except Exception:
            pass


def log_debug(msg, level=logging.DEBUG, exc_info=False):
    """
    Log to debug.log - detailed debugging information.
    
    Args:
        msg: Message to log
        level: Log level (default DEBUG)
        exc_info: Include exception information (default False)
    """
    _, _, debug_logger = get_loggers()
    debug_logger.log(level, msg, exc_info=exc_info)
    # Flush to ensure message is written immediately for debugging purposes
    for handler in debug_logger.handlers:
        try:
            handler.flush()
        except Exception:
            pass


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
    # All three log functions now flush internally


# Initialize loggers on module import
setup_logging()

# Suppress noisy urllib3 connection pool warnings
# These warnings are logged when retries happen, but the retries are handled properly
# We only want to see actual errors, not warnings about connection issues that get retried
urllib3_logger = logging.getLogger('urllib3.connectionpool')
urllib3_logger.setLevel(logging.ERROR)  # Only show actual errors, not warnings
