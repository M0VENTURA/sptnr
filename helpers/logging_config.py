"""
Centralized logging configuration for Popularr.
Config-driven, thread-safe logging configuration.
"""

from __future__ import annotations

import os
import logging
import logging.config
from typing import Any

import structlog

from helpers.config_helpers import get_config

_VALID_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


def _resolve_log_level() -> str:
    """Resolve the configured root log level (default ``INFO``)."""
    try:
        cfg = get_config()
        level = (cfg.get("logging", {}) or {}).get("level") or cfg.get("log_level")
    except Exception:
        level = None
        
    if not level:
        level = os.environ.get("LOG_LEVEL") or os.environ.get("SPTNR_LOG_LEVEL") or "INFO"
        
    level = str(level).strip().upper()
    if level not in _VALID_LEVELS:
        level = "INFO"
    return level


def set_log_level(level: str) -> str:
    """Update the root logger level at runtime (no restart required)."""
    level = str(level or "").strip().upper()
    if level not in _VALID_LEVELS:
        level = "INFO"
    logging.getLogger().setLevel(level)
    return level


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
        if record.levelno < logging.INFO:
            return False
            
        if record.name.startswith("uvicorn.access") or record.name.startswith("werkzeug"):
            return False

        if record.name.startswith("apscheduler"):
            return False

        return True


def log_unified(message: str, **kwargs: Any) -> None:
    """Write a progress message to the unified scan log.

    Uses the stdlib logger directly so the message ALWAYS lands in
    ``unified_scan.log`` — structlog's stdlib bridge is only configured when
    ``STRUCTLOG=1``, and relying on it unconditionally silently dropped every
    unified line to stdout when the env var was unset (the default).  In
    structlog mode the ``ProcessorFormatter`` on the ``unified_file`` handler
    re-renders the record via ``foreign_pre_chain``, so output stays JSON; in
    plain mode the stdlib formatter prints ``message`` verbatim.

    ``kwargs`` are appended as ``key=value`` pairs so callers that pass extra
    context keep it visible in both modes (a stdlib ``extra=`` cannot be used
    safely here — arbitrary keys collide with reserved LogRecord attributes).
    """
    if kwargs:
        rendered = message + " " + " ".join(f"{k}={v!r}" for k, v in kwargs.items())
    else:
        rendered = message
    logging.getLogger("popularr.unified").info(rendered)


def log_queue(message: str, **kwargs: Any) -> None:
    """Write a download-queue event to ``queue.log``."""
    if kwargs:
        message = message + " " + " ".join(f"{k}={v!r}" for k, v in kwargs.items())
    logging.getLogger("popularr.queue").info(message)


def log_search(message: str, **kwargs: Any) -> None:
    """Write a Soulseek search event to ``search.log``."""
    if kwargs:
        message = message + " " + " ".join(f"{k}={v!r}" for k, v in kwargs.items())
    logging.getLogger("popularr.search").info(message)


class SafePrefixFormatter(logging.Formatter):
    """Appends a service prefix safely without mutating the shared LogRecord."""
    
    def __init__(self, prefix: str, fmt: str | None = None, datefmt: str | None = None):
        super().__init__(fmt, datefmt)
        self.prefix = prefix

    def format(self, record: logging.LogRecord) -> str:
        original_msg = record.msg
        if isinstance(record.msg, str) and not record.msg.startswith(self.prefix):
            record.msg = f"{self.prefix}{record.msg}"
            
        result = super().format(record)
        record.msg = original_msg  
        return result


def setup_logging(service_name: str = "popularr") -> None:
    """Configures centralized logging system."""
    log_dir = resolve_log_dir()
    use_structlog = os.environ.get("STRUCTLOG", "").strip() in ("1", "true", "yes")

    if use_structlog:
        _setup_structlog(service_name, log_dir)
    else:
        _setup_standard_logging(service_name, log_dir, use_structlog=False)


def _setup_structlog(service_name: str, log_dir: str) -> None:
    """Configure structlog for JSON/Console output bridging to stdlib."""
    
    shared_processors = [
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]

    structlog.configure(
        processors=shared_processors + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    _setup_standard_logging(service_name, log_dir, use_structlog=True)


def _setup_standard_logging(service_name: str, log_dir: str, use_structlog: bool = False) -> None:
    """Configure standard dictConfig-based logging."""
    fmt = "%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s"
    date_fmt = "%Y-%m-%d %H:%M:%S"
    root_level = _resolve_log_level()

    # If structlog is enabled, we map the formatters to use the structlog processor
    # to render the key-value pairs cleanly into the files without double-timestamps.
    if use_structlog:
        is_console = bool(os.environ.get("STRUCTLOG_CONSOLE"))
        processor = structlog.dev.ConsoleRenderer(colors=False) if is_console else structlog.processors.JSONRenderer()
        
        unified_formatter = {
            "()": structlog.stdlib.ProcessorFormatter,
            "processor": processor,
            "foreign_pre_chain": [
                structlog.stdlib.add_log_level,
                structlog.stdlib.add_logger_name,
                structlog.processors.TimeStamper(fmt="iso"),
            ],
        }
        verbose_formatter = unified_formatter
    else:
        unified_formatter = {"format": fmt, "datefmt": date_fmt}
        verbose_formatter = {
            "format": "%(asctime)s.%(msecs)03d [%(levelname)s] [%(name)s] %(message)s",
            "datefmt": date_fmt,
        }

    config: dict[str, Any] = {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {
            "unified_filter": {
                "()": UnifiedLogFilter,
            }
        },
        "formatters": {
            "unified": unified_formatter,
            "verbose": verbose_formatter,
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
                "formatter": "verbose",
                "level": "INFO",
            },
            "debug_file": {
                "class": "logging.handlers.TimedRotatingFileHandler",
                "filename": os.path.join(log_dir, "debug.log"),
                "when": "midnight",
                "backupCount": 7,
                "encoding": "utf-8",
                "formatter": "verbose",
                "level": "DEBUG",
            },
            "error_file": {
                "class": "logging.handlers.TimedRotatingFileHandler",
                "filename": os.path.join(log_dir, "error.log"),
                "when": "midnight",
                "backupCount": 7,
                "encoding": "utf-8",
                "formatter": "verbose",
                "level": "ERROR",
            },
            "queue_file": {
                "class": "logging.handlers.TimedRotatingFileHandler",
                "filename": os.path.join(log_dir, "queue.log"),
                "when": "midnight",
                "backupCount": 7,
                "encoding": "utf-8",
                "formatter": "unified",
                "level": "INFO",
            },
            "search_file": {
                "class": "logging.handlers.TimedRotatingFileHandler",
                "filename": os.path.join(log_dir, "search.log"),
                "when": "midnight",
                "backupCount": 7,
                "encoding": "utf-8",
                "formatter": "unified",
                "level": "INFO",
            },
        },
        "loggers": {
            "": {
                "handlers": ["unified_file", "info_file", "debug_file", "error_file"],
                "level": root_level,
            },
            "popularr.unified": {
                "handlers": ["unified_file"],
                "level": "INFO",
                "propagate": False,
            },
            "popularr.queue": {
                "handlers": ["queue_file"],
                "level": "INFO",
                "propagate": False,
            },
            "popularr.search": {
                "handlers": ["search_file"],
                "level": "INFO",
                "propagate": False,
            },
            "services.queue": {
                "handlers": ["queue_file", "error_file"],
                "level": "INFO",
                "propagate": False,
            },
            "services.downloads.download_completion_service": {
                "handlers": ["queue_file", "error_file"],
                "level": "INFO",
                "propagate": False,
            },
            "services.downloads.download_pipeline_service": {
                "handlers": ["queue_file", "error_file"],
                "level": "INFO",
                "propagate": False,
            },
            "services.downloads.download_processing_service": {
                "handlers": ["queue_file", "error_file"],
                "level": "INFO",
                "propagate": False,
            },
            "services.downloads.download_queue_normalizer": {
                "handlers": ["queue_file", "error_file"],
                "level": "INFO",
                "propagate": False,
            },
            "services.downloads.download_queue_service": {
                "handlers": ["queue_file", "error_file"],
                "level": "INFO",
                "propagate": False,
            },
            "services.downloads.download_retry_service": {
                "handlers": ["queue_file", "error_file"],
                "level": "INFO",
                "propagate": False,
            },
            "services.downloads.slskd_service": {
                "handlers": ["queue_file", "error_file"],
                "level": "INFO",
                "propagate": False,
            },
            "services.downloads.download_organize_helpers": {
                "handlers": ["queue_file", "error_file"],
                "level": "INFO",
                "propagate": False,
            },
            "services.downloads.download_verification_service": {
                "handlers": ["queue_file", "error_file"],
                "level": "INFO",
                "propagate": False,
            },
            "urllib3": {"level": "ERROR"},
            "httpx": {"level": "ERROR"},
            "apscheduler.schedulers.background": {"level": "WARNING"},
            "apscheduler.executors.default": {"level": "WARNING"},
        },
    }

    logging.config.dictConfig(config)
