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

# Direct handle to the unified_scan.log handler (set in _setup_standard_logging)
# so a runtime set_log_level() toggle can raise/lower its level.
_UNIFIED_FILE_HANDLER = None


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

    # The unified_file handler follows the configured root level so DEBUG
    # records (per-step enrichment detail) reach unified_scan.log when debug
    # is enabled in config.html.  Updating it here makes a runtime toggle take
    # effect immediately instead of on the next restart.
    global _UNIFIED_FILE_HANDLER
    if _UNIFIED_FILE_HANDLER is not None:
        _UNIFIED_FILE_HANDLER.setLevel(level)

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
    """Filters out noisy API requests and sub-INFO noise from unified logs.

    In normal (INFO) mode, sub-INFO records never reach unified_scan.log so
    the scan log stays readable.  When the configured root level is DEBUG
    (``logging.level: debug`` in config.html), DEBUG records ARE allowed
    through — that is what surfaces the per-step enrichment detail
    (album-type lookup, album art chain, artist metadata, similar artists,
    etc.) in unified_scan.log for troubleshooting a slow scan.

    The debug check is evaluated on EVERY ``filter()`` call (a cheap config
    dict read) rather than cached at construction, so toggling debug in
    config.html at runtime takes effect immediately without a restart.
    """

    def _debug_enabled(self) -> bool:
        try:
            return _resolve_log_level() == "DEBUG"
        except Exception:
            return False

    def filter(self, record: logging.LogRecord) -> bool:
        if self._debug_enabled():
            # In debug mode only skip the known noisy access/scheduler names.
            if record.name.startswith("uvicorn.access") or record.name.startswith("werkzeug"):
                return False
            if record.name.startswith("apscheduler"):
                return False
            return True

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
    """Configures centralized logging system.

    The structlog → stdlib bridge is ALWAYS configured (both modes), so
    ``structlog.get_logger(__name__)`` calls from service modules flow into
    the log files (info.log / debug.log / error.log / unified_scan.log) no
    matter what ``STRUCTLOG`` is set to.  ``STRUCTLOG`` only selects the
    on-disk rendering:

    - ``STRUCTLOG=1`` → JSON (or ConsoleRenderer with ``STRUCTLOG_CONSOLE``)
    - unset/``0``     → plain ``2026-08-23 09:00:00 [INFO] message`` lines
    """
    log_dir = resolve_log_dir()
    use_structlog = os.environ.get("STRUCTLOG", "").strip() in ("1", "true", "yes")

    _configure_structlog_bridge()
    _setup_standard_logging(service_name, log_dir, use_structlog=use_structlog)


def _plain_renderer(_logger: Any, _method: str, event_dict: dict[str, Any]) -> str:
    """Render a structlog event dict as a plain log line.

    Produces ``2026-08-23 09:00:00 [INFO] [module] message key=value`` — the
    same shape the old stdlib formatter emitted, so the /logs page's
    timestamp/level parsing keeps working.  Called by the plain (non-JSON)
    ``ProcessorFormatter``; ``foreign_pre_chain`` has already populated
    ``level`` / ``logger`` / ``timestamp`` before this runs.
    """
    ts = event_dict.get("timestamp", "")
    level = str(event_dict.get("level", "info")).upper()
    name = event_dict.get("logger", "")
    event = event_dict.pop("event", "")
    parts = [f"{ts} [{level}]" if ts else f"[{level}]"]
    if name:
        parts.append(f"[{name}]")
    if event:
        parts.append(str(event))
    for key, value in event_dict.items():
        if key in ("logger", "level", "timestamp"):
            continue
        parts.append(f"{key}={value!r}")
    return " ".join(parts)


def _configure_structlog_bridge() -> None:
    """Wire structlog so its loggers flow into stdlib handlers.

    Without this, ``structlog.get_logger(__name__).info(...)`` from the
    structlog-converted service modules prints to stdout ONLY — nothing
    reaches info.log / debug.log / error.log / unified_scan.log.  With the
    bridge (``wrap_for_formatter``) every structlog call becomes a stdlib
    record handled by the dictConfig handlers below.
    """
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
        # Plain mode: still use ProcessorFormatter so the structlog bridge
        # records render cleanly (message + key=value) instead of dumping the
        # event dict.  The processor adds its own "event" text; the stdlib
        # formatter adds timestamp + level + logger name.
        _pf_foreign_chain = [
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="%Y-%m-%d %H:%M:%S", utc=False),
        ]
        unified_formatter = {
            "()": structlog.stdlib.ProcessorFormatter,
            "processor": _plain_renderer,
            "foreign_pre_chain": _pf_foreign_chain,
        }
        verbose_formatter = {
            "()": structlog.stdlib.ProcessorFormatter,
            "processor": _plain_renderer,
            "foreign_pre_chain": _pf_foreign_chain,
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
                # Follow the configured root level so DEBUG records (per-step
                # enrichment detail) reach unified_scan.log when the user
                # enables debug in config.html.  In INFO mode the filter
                # already drops sub-INFO records, so this stays clean.
                "level": root_level,
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
            # ── Download-queue related loggers ─────────────────────────────
            # All download/queue activity belongs in queue.log (with errors in
            # error.log), NOT info.log.  The web UI has dedicated Queue and
            # Logs pages that read these files, so the info.log stays focused
            # on scan / library / server activity.
            #
            # ``services.queue`` routes every ``services.queue.*`` module.
            # ``services.downloads`` routes every ``services.downloads.*``
            # module (download scan, watcher, match engine, organisers,
            # slskd, verification, retry, scheduler…).
            # ``db.repositories.queue*`` is the queue repository layer (its
            # "Duplicate skipped" messages flooded info.log on every cycle).
            "services.queue": {
                "handlers": ["queue_file", "error_file"],
                "level": "INFO",
                "propagate": False,
            },
            "services.downloads": {
                "handlers": ["queue_file", "error_file"],
                "level": "INFO",
                "propagate": False,
            },
            "db.repositories.queue": {
                "handlers": ["queue_file", "error_file"],
                "level": "INFO",
                "propagate": False,
            },
            "db.repositories.queue_admin": {
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

    # Keep a direct handle to the unified_file handler so a runtime
    # ``set_log_level()`` toggle can adjust its level (dictConfig handlers
    # don't carry the config key as a ``name``, so we can't look it up by
    # name on the root logger).
    global _UNIFIED_FILE_HANDLER
    _UNIFIED_FILE_HANDLER = None
    root_logger = logging.getLogger()
    for handler in root_logger.handlers:
        if isinstance(handler, logging.FileHandler) and getattr(handler, "baseFilename", "").endswith(
            os.path.join(log_dir, "unified_scan.log")
        ):
            _UNIFIED_FILE_HANDLER = handler
            break
