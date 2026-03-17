"""Tests for modules.commands.webviewer_command."""

import asyncio
import configparser
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from modules.commands.webviewer_command import WebViewerCommand
from tests.conftest import mock_message


# ---------------------------------------------------------------------------
# Bot factory
# ---------------------------------------------------------------------------

def _make_bot(enabled=True, has_integration=True):
    bot = MagicMock()
    bot.logger = Mock()

    config = configparser.ConfigParser()
    config.add_section("Bot")
    config.set("Bot", "bot_name", "TestBot")
    config.add_section("Channels")
    config.set("Channels", "monitor_channels", "general")
    config.set("Channels", "respond_to_dms", "true")
    config.add_section("Keywords")
    config.add_section("WebViewer_Command")
    config.set("WebViewer_Command", "enabled", "true" if enabled else "false")

    bot.config = config
    bot.translator = MagicMock()
    bot.translator.translate = Mock(side_effect=lambda key, **kw: key)
    bot.translator.get_value = Mock(return_value=None)
    bot.command_manager = MagicMock()
    bot.command_manager.monitor_channels = ["general"]
    bot.command_manager.send_response = AsyncMock(return_value=True)

    if has_integration:
        integration = MagicMock()
        integration.enabled = True
        integration.running = True
        integration.host = "localhost"
        integration.port = 5000
        bot_int = MagicMock()
        bot_int.circuit_breaker_open = False
        bot_int.circuit_breaker_failures = 0
        bot_int.is_shutting_down = False
        integration.bot_integration = bot_int
        bot.web_viewer_integration = integration
    else:
        bot.web_viewer_integration = None

    return bot


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# can_execute / enabled flag
# ---------------------------------------------------------------------------

class TestCanExecute:
    def test_enabled_true(self):
        cmd = WebViewerCommand(_make_bot(enabled=True))
        msg = mock_message(content="webviewer status", is_dm=True)
        assert cmd.can_execute(msg) is True

    def test_enabled_false(self):
        cmd = WebViewerCommand(_make_bot(enabled=False))
        cmd.webviewer_enabled = False
        msg = mock_message(content="webviewer status", is_dm=True)
        assert cmd.can_execute(msg) is False


# ---------------------------------------------------------------------------
# matches_keyword
# ---------------------------------------------------------------------------

class TestMatchesKeyword:
    def test_matches_webviewer(self):
        cmd = WebViewerCommand(_make_bot())
        msg = mock_message(content="webviewer status")
        assert cmd.matches_keyword(msg) is True

    def test_matches_web(self):
        cmd = WebViewerCommand(_make_bot())
        msg = mock_message(content="web status")
        assert cmd.matches_keyword(msg) is True

    def test_matches_wv(self):
        cmd = WebViewerCommand(_make_bot())
        msg = mock_message(content="wv status")
        assert cmd.matches_keyword(msg) is True

    def test_exact_match_webviewer(self):
        cmd = WebViewerCommand(_make_bot())
        msg = mock_message(content="webviewer")
        assert cmd.matches_keyword(msg) is True

    def test_no_match(self):
        cmd = WebViewerCommand(_make_bot())
        msg = mock_message(content="ping")
        assert cmd.matches_keyword(msg) is False

    def test_with_exclamation_prefix(self):
        cmd = WebViewerCommand(_make_bot())
        msg = mock_message(content="!webviewer status")
        assert cmd.matches_keyword(msg) is True


# ---------------------------------------------------------------------------
# execute — no subcommand
# ---------------------------------------------------------------------------

class TestExecuteNoSubcommand:
    def test_no_subcommand_shows_usage(self):
        bot = _make_bot()
        cmd = WebViewerCommand(bot)
        msg = mock_message(content="webviewer", is_dm=True)
        _run(cmd.execute(msg))
        bot.command_manager.send_response.assert_called_once()
        call_args = bot.command_manager.send_response.call_args[0]
        assert "Usage" in call_args[1] or "subcommand" in call_args[1].lower()

    def test_exclamation_prefix_stripped(self):
        bot = _make_bot()
        cmd = WebViewerCommand(bot)
        msg = mock_message(content="!webviewer", is_dm=True)
        _run(cmd.execute(msg))
        bot.command_manager.send_response.assert_called_once()

    def test_returns_true(self):
        bot = _make_bot()
        cmd = WebViewerCommand(bot)
        msg = mock_message(content="webviewer", is_dm=True)
        result = _run(cmd.execute(msg))
        assert result is True


# ---------------------------------------------------------------------------
# execute — unknown subcommand
# ---------------------------------------------------------------------------

class TestExecuteUnknownSubcommand:
    def test_unknown_subcommand_sends_error(self):
        bot = _make_bot()
        cmd = WebViewerCommand(bot)
        msg = mock_message(content="webviewer foobar", is_dm=True)
        _run(cmd.execute(msg))
        bot.command_manager.send_response.assert_called_once()
        call_args = bot.command_manager.send_response.call_args[0]
        assert "Unknown" in call_args[1] or "unknown" in call_args[1].lower()


# ---------------------------------------------------------------------------
# _handle_status
# ---------------------------------------------------------------------------

class TestHandleStatus:
    def test_status_with_integration(self):
        bot = _make_bot(has_integration=True)
        cmd = WebViewerCommand(bot)
        msg = mock_message(content="webviewer status", is_dm=True)
        _run(cmd.execute(msg))
        bot.command_manager.send_response.assert_called_once()
        call_args = bot.command_manager.send_response.call_args[0]
        assert "Status" in call_args[1] or "status" in call_args[1].lower() or "enabled" in call_args[1]

    def test_status_without_integration(self):
        bot = _make_bot(has_integration=False)
        cmd = WebViewerCommand(bot)
        msg = mock_message(content="webviewer status", is_dm=True)
        _run(cmd.execute(msg))
        bot.command_manager.send_response.assert_called_once()
        call_args = bot.command_manager.send_response.call_args[0]
        assert "not available" in call_args[1]

    def test_status_without_bot_integration_attr(self):
        bot = _make_bot(has_integration=True)
        del bot.web_viewer_integration.bot_integration
        cmd = WebViewerCommand(bot)
        msg = mock_message(content="webviewer status", is_dm=True)
        _run(cmd.execute(msg))
        bot.command_manager.send_response.assert_called_once()

    def test_status_running_false(self):
        bot = _make_bot(has_integration=True)
        bot.web_viewer_integration.running = False
        cmd = WebViewerCommand(bot)
        msg = mock_message(content="webviewer status", is_dm=True)
        _run(cmd.execute(msg))
        bot.command_manager.send_response.assert_called_once()


# ---------------------------------------------------------------------------
# _handle_reset
# ---------------------------------------------------------------------------

class TestHandleReset:
    def test_reset_with_bot_integration(self):
        bot = _make_bot(has_integration=True)
        cmd = WebViewerCommand(bot)
        msg = mock_message(content="webviewer reset", is_dm=True)
        _run(cmd.execute(msg))
        bot.web_viewer_integration.bot_integration.reset_circuit_breaker.assert_called_once()
        call_args = bot.command_manager.send_response.call_args[0]
        assert "reset" in call_args[1].lower()

    def test_reset_without_integration(self):
        bot = _make_bot(has_integration=False)
        cmd = WebViewerCommand(bot)
        msg = mock_message(content="webviewer reset", is_dm=True)
        _run(cmd.execute(msg))
        call_args = bot.command_manager.send_response.call_args[0]
        assert "not available" in call_args[1]

    def test_reset_without_bot_integration(self):
        bot = _make_bot(has_integration=True)
        bot.web_viewer_integration.bot_integration = None
        cmd = WebViewerCommand(bot)
        msg = mock_message(content="webviewer reset", is_dm=True)
        _run(cmd.execute(msg))
        call_args = bot.command_manager.send_response.call_args[0]
        assert "not available" in call_args[1]


# ---------------------------------------------------------------------------
# _handle_restart
# ---------------------------------------------------------------------------

class TestHandleRestart:
    def test_restart_with_integration(self):
        bot = _make_bot(has_integration=True)
        bot.web_viewer_integration.restart_viewer = Mock()
        cmd = WebViewerCommand(bot)
        msg = mock_message(content="webviewer restart", is_dm=True)
        _run(cmd.execute(msg))
        bot.web_viewer_integration.restart_viewer.assert_called_once()
        call_args = bot.command_manager.send_response.call_args[0]
        assert "restart" in call_args[1].lower()

    def test_restart_without_integration(self):
        bot = _make_bot(has_integration=False)
        cmd = WebViewerCommand(bot)
        msg = mock_message(content="webviewer restart", is_dm=True)
        _run(cmd.execute(msg))
        call_args = bot.command_manager.send_response.call_args[0]
        assert "not available" in call_args[1]

    def test_restart_exception_handled(self):
        bot = _make_bot(has_integration=True)
        bot.web_viewer_integration.restart_viewer = Mock(side_effect=Exception("crash"))
        cmd = WebViewerCommand(bot)
        msg = mock_message(content="webviewer restart", is_dm=True)
        _run(cmd.execute(msg))
        call_args = bot.command_manager.send_response.call_args[0]
        assert "Failed" in call_args[1] or "failed" in call_args[1].lower()


# ---------------------------------------------------------------------------
# get_help_text
# ---------------------------------------------------------------------------

class TestGetHelpText:
    def test_returns_usage_string(self):
        cmd = WebViewerCommand(_make_bot())
        result = cmd.get_help_text()
        assert "webviewer" in result.lower() or "Usage" in result
