"""Tests for modules.commands.hello_command."""

import pytest
from unittest.mock import patch

from modules.commands.hello_command import HelloCommand
from tests.conftest import command_mock_bot, mock_message


class TestHelloCommand:
    """Tests for HelloCommand."""

    def test_is_emoji_only_message_vulcan_salute(self, command_mock_bot):
        command_mock_bot.config.add_section("Hello_Command")
        command_mock_bot.config.set("Hello_Command", "enabled", "true")
        cmd = HelloCommand(command_mock_bot)
        assert cmd.is_emoji_only_message("🖖") is True

    def test_is_emoji_only_message_with_whitespace(self, command_mock_bot):
        command_mock_bot.config.add_section("Hello_Command")
        command_mock_bot.config.set("Hello_Command", "enabled", "true")
        cmd = HelloCommand(command_mock_bot)
        assert cmd.is_emoji_only_message("  🖖  ") is True

    def test_is_emoji_only_message_text_returns_false(self, command_mock_bot):
        command_mock_bot.config.add_section("Hello_Command")
        command_mock_bot.config.set("Hello_Command", "enabled", "true")
        cmd = HelloCommand(command_mock_bot)
        assert cmd.is_emoji_only_message("hello") is False

    def test_is_emoji_only_message_empty_returns_false(self, command_mock_bot):
        command_mock_bot.config.add_section("Hello_Command")
        command_mock_bot.config.set("Hello_Command", "enabled", "true")
        cmd = HelloCommand(command_mock_bot)
        assert cmd.is_emoji_only_message("") is False

    @patch("datetime.datetime")
    def test_get_random_greeting_deterministic_with_mocked_time(self, mock_datetime, command_mock_bot):
        command_mock_bot.config.add_section("Hello_Command")
        command_mock_bot.config.set("Hello_Command", "enabled", "true")
        mock_now = mock_datetime.now.return_value
        mock_now.hour = 10  # morning
        cmd = HelloCommand(command_mock_bot)
        with patch("modules.commands.hello_command.random.choice", side_effect=lambda x: x[0]):
            result = cmd.get_random_greeting()
        assert isinstance(result, str) and len(result) > 0

    def test_get_emoji_response_vulcan(self, command_mock_bot):
        command_mock_bot.config.add_section("Hello_Command")
        command_mock_bot.config.set("Hello_Command", "enabled", "true")
        cmd = HelloCommand(command_mock_bot)
        with patch("modules.commands.hello_command.random.choice", side_effect=lambda x: x[0]):
            result = cmd.get_emoji_response("🖖", "TestBot")
        assert "🖖" in result or "TestBot" in result

    def test_can_execute_when_enabled(self, command_mock_bot):
        command_mock_bot.config.add_section("Hello_Command")
        command_mock_bot.config.set("Hello_Command", "enabled", "true")
        cmd = HelloCommand(command_mock_bot)
        msg = mock_message(content="hello", is_dm=True)
        assert cmd.can_execute(msg) is True

    def test_can_execute_when_disabled(self, command_mock_bot):
        command_mock_bot.config.add_section("Hello_Command")
        command_mock_bot.config.set("Hello_Command", "enabled", "false")
        cmd = HelloCommand(command_mock_bot)
        msg = mock_message(content="hello", is_dm=True)
        assert cmd.can_execute(msg) is False

    @pytest.mark.asyncio
    async def test_execute_text_greeting_sends_response(self, command_mock_bot):
        command_mock_bot.config.add_section("Hello_Command")
        command_mock_bot.config.set("Hello_Command", "enabled", "true")
        cmd = HelloCommand(command_mock_bot)
        msg = mock_message(content="hello", is_dm=True)
        with patch("modules.commands.hello_command.random.choice", side_effect=lambda x: x[0]):
            result = await cmd.execute(msg)
        assert result is True
        call_args = command_mock_bot.command_manager.send_response.call_args
        assert call_args is not None
        response = call_args[0][1]
        assert isinstance(response, str) and len(response) > 0
