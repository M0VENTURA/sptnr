"""
Centralized logging configuration for Popularr.
Config-driven, thread-safe logging configuration.
"""

import os
import logging
import logging.config
from helpers.config_helpers import get_config


_VALID_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})


def _resolve_log_level() -> str:
    """Resolve the configured root log level (default ``INFO``).

    Debug logging is off by default — it is very verbose and grows ``debug.log``
    quickly.  The level can be raised from the config page (``logging.level``),
    the legacy top-level ``log_level`` key, or the ``LOG_LEVEL``/``SPTNR_LOG_LEVEL``
    env vars.  Anything unrecognised falls back to ``INFO``.
    """
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
    """Update the root logger level at runtime (no restart required).

    Used by the config page so a log-level change applies immediately rather
    than waiting for the next app restart.

    Returns:
        The normalized level that was applied (e.g. ``"INFO"``).
    """
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

        # Scheduler bookkeeping (APScheduler registrations, job-store adds)
        # stays in info.log — the scanning log only shows scan activity.
        if record.name.startswith("apscheduler"):
            return False

        return True


def log_unified(message: str) -> None:
    """Write a progress message to the unified scan log (``unified_scan.log``).

    Used extensively by scanning pipelines to record human-readable progress
    that operators can tail in real time.  Logs at INFO level so the
    ``UnifiedLogFilter`` on the ``unified_file`` handler lets it through.
    """
    logging.getLogger("popularr.unified").info(message)


def log_queue(message: str) -> None:
    """Write a download-queue event to ``queue.log``.

    Only queue activity (searching/downloading/completing/failing queue items)
    belongs in this log.  Soulseek searches are kept separate in ``search.log``.
    """
    logging.getLogger("popularr.queue").info(message)


def log_search(message: str) -> None:
    """Write a Soulseek search event to ``search.log``.

    Records every automatic and manual Soulseek search so the full history can
    be reviewed under the /logs page while the dashboard/monitor only surface
    the last hour.
    """
    logging.getLogger("popularr.search").info(message)


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


def setup_logging(service_name: str = "popularr") -> None:
    """Configures centralized logging system.

    When ``STRUCTLOG=1`` env var is set, uses ``structlog`` for JSON output
    on stderr (parsable by Loki/Datadog/Splunk). File logging still uses
    standard log format for readability.
    """
    log_dir = resolve_log_dir()

    use_structlog = os.environ.get("STRUCTLOG", "").strip() in ("1", "true", "yes")

    if use_structlog:
        _setup_structlog(service_name, log_dir)
    else:
        _setup_standard_logging(service_name, log_dir)


def _setup_standard_logging(service_name: str, log_dir: str) -> None:
    """Configure standard dictConfig-based logging."""
    fmt = "%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s"
    date_fmt = "%Y-%m-%d %H:%M:%S"
    root_level = _resolve_log_level()

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
            "verbose": {
                "format": "%(asctime)s.%(msecs)03d [%(levelname)s] [%(name)s] %(message)s",
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
            # Root logger: sends to unified + info + debug files.
            # unified_file keeps the UnifiedLogFilter which blocks DEBUG and
            # HTTP access noise, so the unified log stays clean even with all
            # messages routed to it.
            "": {
                "handlers": ["unified_file", "info_file", "debug_file", "error_file"],
                # Root level is config-driven and defaults to INFO so verbose
                # DEBUG output is off by default (debug.log stays quiet until
                # the operator enables it via config.html / LOG_LEVEL env).
                "level": root_level,
            },
            # Unified logger: only writes to unified_scan.log, does NOT propagate
            # to root.  Use log_unified() or logging.getLogger("popularr.unified")
            # for human-readable scan progress.
            "popularr.unified": {
                "handlers": ["unified_file"],
                "level": "INFO",
                "propagate": False,
            },
            # Queue logger: only writes to queue.log.  Use log_queue() for
            # download-queue activity (distinct from Soulseek searches).
            "popularr.queue": {
                "handlers": ["queue_file"],
                "level": "INFO",
                "propagate": False,
            },
            # Search logger: only writes to search.log.  Use log_search() for
            # Soulseek search activity (automatic and manual).
            "popularr.search": {
                "handlers": ["search_file"],
                "level": "INFO",
                "propagate": False,
            },
            # ── Queue module loggers ──────────────────────────────────────
            # Queue lifecycle code (services.queue.* plus the queue-lifecycle
            # services.downloads modules) logs via its module logger
            # (``logging.getLogger(__name__)``), which would otherwise
            # propagate to the ROOT logger and pollute unified_scan.log /
            # info.log / debug.log with queue activity.  Route them to
            # queue.log ONLY (propagate=False), and to error.log for ERROR
            # records — the queue belongs in queue.log, never in the general
            # app logs, unless it is an error.
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


def _setup_structlog(service_name: str, log_dir: str) -> None:
    """Configure structlog for JSON output on stderr."""
    import structlog

    # Standard library logging still goes to files
    _setup_standard_logging(service_name, log_dir)

    # Override root logger to also emit JSON via structlog
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.dev.ConsoleRenderer() if os.environ.get("STRUCTLOG_CONSOLE") else structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Redirect standard logging to structlog for unified output
    structlog.stdlib.recreate_defaults(log_level=logging.getLevelName(_resolve_log_level()))
