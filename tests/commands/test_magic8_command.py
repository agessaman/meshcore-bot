"""Tests for modules.commands.magic8_command."""

import pytest

from modules.commands.magic8_command import Magic8Command, magic8_responses
from tests.conftest import command_mock_bot, mock_message


class TestMagic8Command:
    """Tests for Magic8Command."""

    def test_can_execute_when_enabled(self, command_mock_bot):
        command_mock_bot.config.add_section("Magic8_Command")
        command_mock_bot.config.set("Magic8_Command", "enabled", "true")
        cmd = Magic8Command(command_mock_bot)
        msg = mock_message(content="magic8 will it rain?", is_dm=True)
        assert cmd.can_execute(msg) is True

    def test_can_execute_when_disabled(self, command_mock_bot):
        command_mock_bot.config.add_section("Magic8_Command")
        command_mock_bot.config.set("Magic8_Command", "enabled", "false")
        cmd = Magic8Command(command_mock_bot)
        msg = mock_message(content="magic8 will it rain?", is_dm=True)
        assert cmd.can_execute(msg) is False

    @pytest.mark.asyncio
    async def test_execute_returns_valid_response(self, command_mock_bot):
        command_mock_bot.config.add_section("Magic8_Command")
        command_mock_bot.config.set("Magic8_Command", "enabled", "true")
        cmd = Magic8Command(command_mock_bot)
        msg = mock_message(content="magic8 will it rain?", is_dm=True)
        result = await cmd.execute(msg)
        assert result is True
        call_args = command_mock_bot.command_manager.send_response.call_args
        assert call_args is not None
        response = call_args[0][1]
        assert "🎱" in response
        assert any(r in response for r in magic8_responses)

    @pytest.mark.asyncio
    async def test_execute_channel_includes_sender_mention(self, command_mock_bot):
        command_mock_bot.config.add_section("Magic8_Command")
        command_mock_bot.config.set("Magic8_Command", "enabled", "true")
        cmd = Magic8Command(command_mock_bot)
        msg = mock_message(content="magic8 test?", channel="general", is_dm=False, sender_id="Alice")
        result = await cmd.execute(msg)
        assert result is True
        call_args = command_mock_bot.command_manager.send_response.call_args
        assert call_args is not None
        response = call_args[0][1]
        assert "Alice" in response or "@[" in response
