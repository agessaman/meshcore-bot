"""modules.logging_setup: the bot and meshcore logger wiring."""

import configparser
import json
import logging

from modules.logging_setup import (
    MESHCORE_LOGGER_NAMES,
    JsonFormatter,
    configure_bot_logging,
    configure_meshcore_loggers,
    meshcore_log_level,
)


def _cfg(**logging_values):
    cfg = configparser.ConfigParser()
    if logging_values:
        cfg["Logging"] = logging_values
    return cfg


def test_defaults_without_logging_section_console_only(tmp_path):
    logger, formatter = configure_bot_logging(_cfg(), tmp_path)
    assert logger.name == "MeshCoreBot" and logger.level == logging.INFO and logger.propagate is False
    assert [type(h).__name__ for h in logger.handlers] == ["StreamHandler"]
    for name in MESHCORE_LOGGER_NAMES:
        mc = logging.getLogger(name)
        assert mc.level == logging.INFO and mc.propagate is False and len(mc.handlers) == 1


def test_file_handler_and_json_formatter(tmp_path):
    cfg = _cfg(log_level="DEBUG", log_file="logs/bot.log", json_logging="true", meshcore_log_level="WARNING")
    logger, formatter = configure_bot_logging(cfg, tmp_path)
    assert isinstance(formatter, JsonFormatter)
    assert {type(h).__name__ for h in logger.handlers} == {"StreamHandler", "RotatingFileHandler"}
    assert (tmp_path / "logs").is_dir()
    assert logging.getLogger("meshcore").level == logging.WARNING
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "hi", None, None)
    assert json.loads(formatter.format(record))["message"] == "hi"


def test_meshcore_debug_shares_bot_handlers(tmp_path):
    logger, formatter = configure_bot_logging(_cfg(), tmp_path)
    configure_meshcore_loggers(logging.DEBUG, None, shared_handlers=list(logger.handlers))
    assert logging.getLogger("meshcore_cli").handlers == logger.handlers
    configure_meshcore_loggers(meshcore_log_level(_cfg()), formatter)
    handler = logging.getLogger("meshcore_cli").handlers[0]
    assert handler not in logger.handlers and handler.formatter is formatter
