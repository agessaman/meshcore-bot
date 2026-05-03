"""Tests for modules.commands.status_command."""

import asyncio
import configparser
from unittest.mock import AsyncMock, MagicMock, Mock

from modules.commands.status_command import StatusCommand
from tests.conftest import mock_message


def _make_bot(*, enabled=True):
    bot = MagicMock()
    bot.logger = Mock()
    bot.connected = True
    bot.is_radio_zombie = False
    bot.is_radio_offline = False
    bot.channel_responses_enabled = True
    bot.web_viewer_integration = MagicMock()
    bot.web_viewer_integration.running = True

    config = configparser.ConfigParser()
    config.add_section("Bot")
    config.set("Bot", "bot_name", "TestBot")
    config.add_section("Channels")
    config.set("Channels", "monitor_channels", "general")
    config.add_section("Keywords")
    config.add_section("Status_Command")
    config.set("Status_Command", "enabled", "true" if enabled else "false")
    config.add_section("Admin_ACL")
    config.set("Admin_ACL", "admin_pubkeys", "a" * 64)
    config.set("Admin_ACL", "admin_commands", "status")
    bot.config = config

    bot.translator = MagicMock()
    bot.translator.translate = Mock(side_effect=lambda key, **kw: key)
    bot.translator.get_value = Mock(return_value=None)
    bot.command_manager = MagicMock()
    bot.command_manager.monitor_channels = ["general"]
    bot.command_manager.send_response = AsyncMock(return_value=True)
    return bot


def _run(coro):
    return asyncio.run(coro)


class TestStatusCommandPermissions:
    def test_dm_admin_allowed(self):
        cmd = StatusCommand(_make_bot(enabled=True))
        msg = mock_message(content="status", is_dm=True, sender_id="user1")
        msg.sender_pubkey = "a" * 64
        assert cmd.can_execute(msg) is True

    def test_channel_disallowed(self):
        cmd = StatusCommand(_make_bot(enabled=True))
        msg = mock_message(content="status", is_dm=False, sender_id="user1")
        msg.sender_pubkey = "a" * 64
        assert cmd.can_execute(msg) is False

    def test_non_admin_disallowed(self):
        cmd = StatusCommand(_make_bot(enabled=True))
        msg = mock_message(content="status", is_dm=True, sender_id="user1")
        msg.sender_pubkey = "b" * 64
        assert cmd.can_execute(msg) is False

    def test_disabled_disallowed(self):
        cmd = StatusCommand(_make_bot(enabled=False))
        msg = mock_message(content="status", is_dm=True, sender_id="user1")
        msg.sender_pubkey = "a" * 64
        assert cmd.can_execute(msg) is False


class TestStatusCommandExecute:
    def test_execute_sends_single_status_message(self):
        bot = _make_bot(enabled=True)
        cmd = StatusCommand(bot)
        msg = mock_message(content="status", is_dm=True, sender_id="user1")
        msg.sender_pubkey = "a" * 64

        result = _run(cmd.execute(msg))

        assert result is True
        bot.command_manager.send_response.assert_called_once()
        text = bot.command_manager.send_response.call_args[0][1]
        assert "Bot Status" in text
        assert "connected: True" in text
        assert "radio_zombie: False" in text
        assert "web_viewer_running: True" in text
