"""Tests for modules.commands.ping_command."""

import pytest

from modules.commands.ping_command import PingCommand
from tests.conftest import command_mock_bot, mock_message


class TestPingCommand:
    """Tests for PingCommand."""

    def test_can_execute_when_enabled(self, command_mock_bot):
        command_mock_bot.config.add_section("Ping_Command")
        command_mock_bot.config.set("Ping_Command", "enabled", "true")
        cmd = PingCommand(command_mock_bot)
        msg = mock_message(content="ping", is_dm=True)
        assert cmd.can_execute(msg) is True

    def test_can_execute_when_disabled(self, command_mock_bot):
        command_mock_bot.config.add_section("Ping_Command")
        command_mock_bot.config.set("Ping_Command", "enabled", "false")
        cmd = PingCommand(command_mock_bot)
        msg = mock_message(content="ping", is_dm=True)
        assert cmd.can_execute(msg) is False

    @pytest.mark.asyncio
    async def test_execute_returns_keyword_response(self, command_mock_bot):
        command_mock_bot.config.add_section("Ping_Command")
        command_mock_bot.config.set("Ping_Command", "enabled", "true")
        command_mock_bot.config.set("Keywords", "ping", "Pong!")
        cmd = PingCommand(command_mock_bot)
        msg = mock_message(content="ping", is_dm=True)
        result = await cmd.execute(msg)
        assert result is True
        call_args = command_mock_bot.command_manager.send_response.call_args
        assert call_args is not None
        response = call_args[0][1]
        assert "Pong" in response or "pong" in response.lower()
