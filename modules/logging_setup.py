"""Logging configuration for the bot process: formatters, handlers, library loggers."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import colorlog

from .utils import resolve_path

# Loggers the meshcore library and meshcore-cli write to.
MESHCORE_LOGGER_NAMES = (
    'meshcore',
    'meshcore_cli',
    'meshcore.meshcore',
    'meshcore_cli.meshcore_cli',
    'meshcore_cli.commands',
    'meshcore_cli.connection',
)

_APSCHEDULER_LOGGER_NAMES = (
    "apscheduler",
    "apscheduler.scheduler",
    "apscheduler.executors",
    "apscheduler.jobstores",
)


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per line for log aggregation pipelines (Loki, Elasticsearch, etc.)."""

    def format(self, record: logging.LogRecord) -> str:
        ts = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(record.created))
        ms = int(record.msecs)
        obj: dict[str, Any] = {
            'timestamp': f'{ts}.{ms:03d}Z',
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
        }
        if record.exc_info:
            obj['exc_info'] = self.formatException(record.exc_info)
        if record.stack_info:
            obj['stack_info'] = self.formatStack(record.stack_info)
        return json.dumps(obj, ensure_ascii=False)


def meshcore_log_level(config: Any) -> int:
    """[Logging] meshcore_log_level as a logging level (INFO when unset)."""
    name = config.get('Logging', 'meshcore_log_level', fallback='INFO') if config.has_section('Logging') else 'INFO'
    return getattr(logging, name)


def configure_meshcore_loggers(
    level: int,
    formatter: logging.Formatter | None,
    shared_handlers: Iterable[logging.Handler] | None = None,
) -> None:
    """Point the meshcore loggers at ``level`` with their own handlers, never the root.

    With ``shared_handlers`` they write through those (the bot's console and
    file handlers); otherwise each gets a console StreamHandler.
    """
    for name in MESHCORE_LOGGER_NAMES:
        mc_logger = logging.getLogger(name)
        mc_logger.setLevel(level)
        mc_logger.handlers.clear()
        if shared_handlers is not None:
            for handler in shared_handlers:
                mc_logger.addHandler(handler)
        else:
            handler = logging.StreamHandler()
            if formatter:
                handler.setFormatter(formatter)
            mc_logger.addHandler(handler)
        mc_logger.propagate = False


def configure_bot_logging(config: Any, bot_root: Path) -> tuple[logging.Logger, logging.Formatter]:
    """Configure the 'MeshCoreBot' logger, the meshcore loggers and noisy third-party ones.

    Reads [Logging]; without it, logs INFO to the console (journal) only.
    Returns the bot logger and the formatter it uses.
    """
    if config.has_section('Logging'):
        log_level = getattr(logging, config.get('Logging', 'log_level', fallback='INFO'))
        colored_output = config.getboolean('Logging', 'colored_output', fallback=True)
        log_file = config.get('Logging', 'log_file', fallback='meshcore_bot.log')
        mc_level = getattr(logging, config.get('Logging', 'meshcore_log_level', fallback='INFO'))
        json_logging = config.getboolean('Logging', 'json_logging', fallback=False)
    else:
        log_level = logging.INFO
        colored_output = True
        log_file = ''  # Console/journal only when no [Logging] section
        mc_level = logging.INFO
        json_logging = False

    formatter: logging.Formatter
    if json_logging:
        formatter = JsonFormatter()
    elif colored_output:
        formatter = colorlog.ColoredFormatter(
            '%(log_color)s%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S',
            log_colors={
                'DEBUG': 'cyan',
                'INFO': 'green',
                'WARNING': 'yellow',
                'ERROR': 'red',
                'CRITICAL': 'red,bg_white',
            }
        )
    else:
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )

    # Normalize root logger early to avoid duplicate/basicConfig output from dependencies.
    # We intentionally keep root handlerless; modules that want logging should attach
    # handlers explicitly (MeshCoreBot and meshcore loggers below).
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(log_level)

    logger = logging.getLogger('MeshCoreBot')
    logger.setLevel(log_level)
    # Clear any existing handlers to prevent duplicates
    logger.handlers.clear()

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    log_file = log_file.strip() if log_file else ''
    if not log_file:
        logger.info("No log file specified, using console logging only")
    else:
        # Relative paths resolve from the bot root; absolute paths are used as-is.
        log_file = resolve_path(log_file, bot_root)
        log_dir = Path(log_file).parent
        if not log_dir.exists():
            try:
                log_dir.mkdir(parents=True, exist_ok=True)
            except (OSError, PermissionError) as e:
                logger.warning(f"Could not create log directory {log_dir}: {e}. Using console logging only.")
                log_file = None
        if log_file:
            try:
                log_max_bytes = config.getint('Logging', 'log_max_bytes', fallback=5 * 1024 * 1024)
                log_backup_count = config.getint('Logging', 'log_backup_count', fallback=3)
                file_handler = RotatingFileHandler(
                    log_file,
                    maxBytes=log_max_bytes,
                    backupCount=log_backup_count,
                    encoding='utf-8',
                )
                file_handler.setFormatter(formatter)
                logger.addHandler(file_handler)
            except (OSError, PermissionError) as e:
                logger.warning(f"Could not open log file {log_file}: {e}. Using console logging only.")

    # Prevent propagation to root logger to avoid duplicate output
    logger.propagate = False

    configure_meshcore_loggers(mc_level, formatter)

    # Silence noisy third-party loggers that can emit unformatted console output.
    # APScheduler: keep INFO (but route through our formatter) and prevent propagation.
    # tzlocal: keep WARNING+ (it can be very chatty at DEBUG).
    for name in _APSCHEDULER_LOGGER_NAMES:
        third = logging.getLogger(name)
        third.handlers.clear()
        third.setLevel(logging.INFO)
        third.propagate = False
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        third.addHandler(handler)

    tzlocal_logger = logging.getLogger("tzlocal")
    tzlocal_logger.handlers.clear()
    tzlocal_logger.setLevel(logging.WARNING)
    tzlocal_logger.propagate = False

    mode = 'json' if json_logging else ('colored' if colored_output else 'plain')
    logger.info(
        f"Logging configured - Bot: {logging.getLevelName(log_level)}, "
        f"MeshCore: {logging.getLevelName(mc_level)}, format: {mode}"
    )
    return logger, formatter
