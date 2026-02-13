"""Tests for modules.commands.help_command."""

import pytest

from modules.commands.help_command import HelpCommand
from tests.conftest import command_mock_bot, mock_message


class TestHelpCommand:
    """Tests for HelpCommand."""

    def test_can_execute_when_enabled(self, command_mock_bot):
        command_mock_bot.config.add_section("Help_Command")
        command_mock_bot.config.set("Help_Command", "enabled", "true")
        cmd = HelpCommand(command_mock_bot)
        msg = mock_message(content="help", is_dm=True)
        assert cmd.can_execute(msg) is True

    def test_can_execute_when_disabled(self, command_mock_bot):
        command_mock_bot.config.add_section("Help_Command")
        command_mock_bot.config.set("Help_Command", "enabled", "false")
        cmd = HelpCommand(command_mock_bot)
        msg = mock_message(content="help", is_dm=True)
        assert cmd.can_execute(msg) is False

    @pytest.mark.asyncio
    async def test_execute_returns_true(self, command_mock_bot):
        command_mock_bot.config.add_section("Help_Command")
        command_mock_bot.config.set("Help_Command", "enabled", "true")
        cmd = HelpCommand(command_mock_bot)
        msg = mock_message(content="help", is_dm=True)
        result = await cmd.execute(msg)
        assert result is True
        # Note: HelpCommand.execute() is a placeholder; actual help logic is in
        # CommandManager.check_keywords(). Response sending is tested there.
