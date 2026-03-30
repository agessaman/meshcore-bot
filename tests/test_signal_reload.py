"""Tests for Unix signal handling in the process entrypoint."""

import signal
from unittest.mock import MagicMock

from meshcore_bot import _configure_unix_signal_handlers


def test_configure_unix_signal_handlers_registers_shutdown_and_reload() -> None:
    handlers = {}
    loop = MagicMock()
    loop.add_signal_handler.side_effect = lambda sig, cb: handlers.__setitem__(sig, cb)
    bot = MagicMock()
    bot.reload_config.return_value = (True, "Configuration reloaded successfully")
    shutdown_event = MagicMock()

    _configure_unix_signal_handlers(loop, bot, shutdown_event)

    assert signal.SIGTERM in handlers
    assert signal.SIGINT in handlers
    if hasattr(signal, "SIGHUP"):
        assert signal.SIGHUP in handlers

    # Shutdown signals set the shutdown event.
    handlers[signal.SIGTERM]()
    handlers[signal.SIGINT]()
    assert shutdown_event.set.call_count == 2

    # Reload signal triggers in-process config reload without shutdown.
    if hasattr(signal, "SIGHUP"):
        handlers[signal.SIGHUP]()
        bot.reload_config.assert_called_once()
        shutdown_event.set.assert_called()  # only from shutdown handlers above


def test_sighup_reload_failure_logs_warning() -> None:
    if not hasattr(signal, "SIGHUP"):
        return

    handlers = {}
    loop = MagicMock()
    loop.add_signal_handler.side_effect = lambda sig, cb: handlers.__setitem__(sig, cb)
    bot = MagicMock()
    bot.reload_config.return_value = (False, "Radio settings changed. Restart required.")
    shutdown_event = MagicMock()

    _configure_unix_signal_handlers(loop, bot, shutdown_event)
    handlers[signal.SIGHUP]()

    bot.reload_config.assert_called_once()
    bot.logger.warning.assert_called_once()
