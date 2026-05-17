"""PacketCapture service log level respects verbose/debug, not global bot log_level."""

from __future__ import annotations

import configparser
import logging
from unittest.mock import MagicMock

from modules.service_plugins.packet_capture_service import PacketCaptureService


def _svc_from_ini(ini: str) -> PacketCaptureService:
    cp = configparser.ConfigParser()
    cp.read_string(ini.strip())
    bot = MagicMock()
    bot.config = cp
    bot.logger = logging.getLogger("test_bot")
    bot.logger.setLevel(logging.DEBUG)
    svc = object.__new__(PacketCaptureService)
    svc.bot = bot
    svc.logger = logging.getLogger("PacketCaptureService.test")
    svc._load_config()
    return svc


def test_debug_false_uses_info_even_when_bot_is_debug():
    svc = _svc_from_ini(
        """
        [PacketCapture]
        enabled = true
        verbose = false
        debug = false
        """
    )
    assert svc.debug is False
    assert svc.verbose is False
    assert svc.logger.level == logging.INFO


def test_debug_true_uses_debug_level():
    svc = _svc_from_ini(
        """
        [PacketCapture]
        enabled = true
        verbose = false
        debug = true
        """
    )
    assert svc.logger.level == logging.DEBUG


def test_log_packet_summary_only_when_verbose_or_debug():
    svc = _svc_from_ini(
        """
        [PacketCapture]
        enabled = true
        verbose = false
        debug = false
        """
    )
    svc.logger.handlers.clear()
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    svc.logger.addHandler(_Capture())
    svc._log_packet_summary("packet line")
    assert records == []

    svc.verbose = True
    svc._log_packet_summary("packet line")
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
