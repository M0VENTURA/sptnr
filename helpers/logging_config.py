"""
Centralized logging configuration for Popularr (SPTNR).
Config-driven, thread-safe logging configuration.
"""

import os
import logging
import logging.config
from helpers.config_helpers import get_config


def resolve_log_dir() -> str:
    """Resolve the directory path for log files safely."""
    try:
        cfg = get_config()
        log_dir = cfg.get("paths", {}).get("log_dir") or cfg.get("log_dir")
        if log_dir:
            return log_dir
    except Exception:
        pass

    log_path = os.environ.get("LOG_PATH", "/config")
    
    # Handle case where LOG_PATH is passed as a file path
    if not log_path.endswith("/") and "." in os.path.basename(log_path):
        log_path = os.path.dirname(log_path)

    try:
        os.makedirs(log_path, exist_ok=True)
        return log_path
    except (PermissionError, OSError):
        script_dir = os.path.dirname(os.path.abspath(__file__))
        fallback = os.path.join(script_dir, "logs")
        os.makedirs(fallback, exist_ok=True)
        return fallback

class UnifiedLogFilter(logging.Filter):
    """Filters out noisy API requests and sub-INFO noise from unified logs."""
    
    def filter(self, record: logging.LogRecord) -> bool:
        # Exclude debug/verbose levels
        if record.levelno < logging.INFO:
            return False
            
        # Fast non-string matching check if logger name indicates web requests
        if record.name.startswith("uvicorn.access") or record.name.startswith("werkzeug"):
            return False

        return True


class SafePrefixFormatter(logging.Formatter):
    """Appends a service prefix safely without mutating the shared LogRecord."""
    
    def __init__(self, prefix: str, fmt=None, datefmt=None):
        super().__init__(fmt, datefmt)
        self.prefix = prefix

    def format(self, record: logging.LogRecord) -> str:
        # Clone format behavior without mutating original record.msg
        original_msg = record.msg
        if isinstance(record.msg, str) and not record.msg.startswith(self.prefix):
            record.msg = f"{self.prefix}{record.msg}"
            
        result = super().format(record)
        record.msg = original_msg  # Restore original
        return result


def setup_logging(service_name: str = "sptnr") -> None:
    """Configures centralized logging system via dictConfig."""
    log_dir = resolve_log_dir()
    
    fmt = "%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s"
    date_fmt = "%Y-%m-%d %H:%M:%S"
    
    config = {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {
            "unified_filter": {
                "()": UnifiedLogFilter,
            }
        },
        "formatters": {
            "unified": {
                "format": fmt,
                "datefmt": date_fmt,
            },
            "prefixed": {
                "()": SafePrefixFormatter,
                "prefix": f"{service_name}_",
                "format": fmt,
                "datefmt": date_fmt,
            },
        },
        "handlers": {
            "unified_file": {
                "class": "logging.handlers.TimedRotatingFileHandler",
                "filename": os.path.join(log_dir, "unified_scan.log"),
                "when": "midnight",
                "backupCount": 7,
                "encoding": "utf-8",
                "formatter": "unified",
                "filters": ["unified_filter"],
                "level": "INFO",
            },
            "info_file": {
                "class": "logging.handlers.TimedRotatingFileHandler",
                "filename": os.path.join(log_dir, "info.log"),
                "when": "midnight",
                "backupCount": 7,
                "encoding": "utf-8",
                "formatter": "prefixed",
                "level": "INFO",
            },
            "debug_file": {
                "class": "logging.handlers.TimedRotatingFileHandler",
                "filename": os.path.join(log_dir, "debug.log"),
                "when": "midnight",
                "backupCount": 7,
                "encoding": "utf-8",
                "formatter": "prefixed",
                "level": "DEBUG",
            },
        },
        "loggers": {
            # Root logger routes all logs appropriately
            "": {
                "handlers": ["unified_file", "info_file", "debug_file"],
                "level": "DEBUG",
            },
            # Silence noisy external libraries
            "urllib3": {"level": "ERROR"},
            "requests": {"level": "ERROR"},
        },
    }

    logging.config.dictConfig(config)