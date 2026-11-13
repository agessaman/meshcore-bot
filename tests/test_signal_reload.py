"""Tests for Unix signal handling in the process entrypoint."""

import signal
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

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


def _registered_handlers(monkeypatch, *, terminal_states, sighup_disposition=signal.SIG_DFL):
    """Register handlers with a scripted controlling-terminal check."""
    states = iter(terminal_states)
    monkeypatch.setattr("meshcore_bot._has_controlling_terminal", lambda: next(states))
    monkeypatch.setattr("meshcore_bot.signal.getsignal", lambda sig: sighup_disposition)
    handlers = {}
    loop = MagicMock()
    loop.add_signal_handler.side_effect = lambda sig, cb: handlers.__setitem__(sig, cb)
    bot = MagicMock()
    bot.reload_config.return_value = (True, "ok")
    shutdown_event = MagicMock()
    _configure_unix_signal_handlers(loop, bot, shutdown_event)
    return handlers, bot, shutdown_event


def test_shutdown_survives_a_closed_terminal(monkeypatch) -> None:
    """print() raises EIO once the terminal is gone; the signal must still land."""
    handlers, bot, shutdown_event = _registered_handlers(monkeypatch, terminal_states=[False])

    def _dead_stdout(*args, **kwargs):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr("builtins.print", _dead_stdout)
    handlers[signal.SIGTERM]()

    shutdown_event.set.assert_called_once()
    bot._shutdown_event.set.assert_called_once()
    assert bot.connected is False


def test_sighup_shuts_down_when_the_terminal_closed(monkeypatch) -> None:
    if not hasattr(signal, "SIGHUP"):
        return
    # Terminal present at startup, gone when SIGHUP arrives.
    handlers, bot, shutdown_event = _registered_handlers(
        monkeypatch, terminal_states=[True, False]
    )
    handlers[signal.SIGHUP]()

    shutdown_event.set.assert_called_once()
    bot.reload_config.assert_not_called()


def test_sighup_reloads_while_the_terminal_is_still_open(monkeypatch) -> None:
    """A deliberate `kill -HUP` from another shell is still a reload."""
    if not hasattr(signal, "SIGHUP"):
        return
    handlers, bot, shutdown_event = _registered_handlers(
        monkeypatch, terminal_states=[True, True]
    )
    handlers[signal.SIGHUP]()

    bot.reload_config.assert_called_once()
    shutdown_event.set.assert_not_called()


def test_sighup_reloads_for_a_service_without_a_terminal(monkeypatch) -> None:
    if not hasattr(signal, "SIGHUP"):
        return
    handlers, bot, shutdown_event = _registered_handlers(monkeypatch, terminal_states=[False])
    handlers[signal.SIGHUP]()

    bot.reload_config.assert_called_once()
    shutdown_event.set.assert_not_called()


def test_sighup_reloads_under_nohup(monkeypatch) -> None:
    """nohup asks for the bot to outlive its terminal, so it must not exit."""
    if not hasattr(signal, "SIGHUP"):
        return
    handlers, bot, shutdown_event = _registered_handlers(
        monkeypatch, terminal_states=[True, False], sighup_disposition=signal.SIG_IGN
    )
    handlers[signal.SIGHUP]()

    bot.reload_config.assert_called_once()
    shutdown_event.set.assert_not_called()


# Runs the real check on a real pty: once with the terminal open, once after
# the terminal side is closed. Mocks cannot cover this; macOS keeps /dev/tty
# openable after a hangup, which an open-only check gets wrong.
_TERMINAL_PROBE = """
import os, pty, signal, sys, time
sys.path.insert(0, sys.argv[1])
pid, master = pty.fork()
if pid == 0:
    signal.signal(signal.SIGHUP, lambda *a: None)
    from meshcore_bot import _has_controlling_terminal
    before = _has_controlling_terminal()
    os.read(0, 1)  # returns EOF once the terminal is closed
    after = _has_controlling_terminal()
    with open(sys.argv[2], "w") as out:
        out.write(f"{before} {after}")
    os._exit(0)
time.sleep(1.0)
os.close(master)
os.waitpid(pid, 0)
"""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX terminals only")
def test_has_controlling_terminal_notices_a_closed_terminal(tmp_path: Path) -> None:
    result_file = tmp_path / "result.txt"
    repo_root = Path(__file__).resolve().parent.parent
    subprocess.run(
        [sys.executable, "-c", _TERMINAL_PROBE, str(repo_root), str(result_file)],
        check=True,
        timeout=30,
    )
    assert result_file.read_text() == "True False"
