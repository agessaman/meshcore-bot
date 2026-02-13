"""Tests for modules.commands.cmd_command."""

import pytest

from modules.commands.cmd_command import CmdCommand
from tests.conftest import command_mock_bot, mock_message


class TestCmdCommand:
    """Tests for CmdCommand."""

    def test_can_execute_when_enabled(self, command_mock_bot):
        command_mock_bot.config.add_section("Cmd_Command")
        command_mock_bot.config.set("Cmd_Command", "enabled", "true")
        command_mock_bot.command_manager.commands = {"ping": object(), "help": object()}
        cmd = CmdCommand(command_mock_bot)
        msg = mock_message(content="cmd", is_dm=True)
        assert cmd.can_execute(msg) is True

    def test_can_execute_when_disabled(self, command_mock_bot):
        command_mock_bot.config.add_section("Cmd_Command")
        command_mock_bot.config.set("Cmd_Command", "enabled", "false")
        cmd = CmdCommand(command_mock_bot)
        msg = mock_message(content="cmd", is_dm=True)
        assert cmd.can_execute(msg) is False

    @pytest.mark.asyncio
    async def test_execute_returns_command_list(self, command_mock_bot):
        command_mock_bot.config.add_section("Cmd_Command")
        command_mock_bot.config.set("Cmd_Command", "enabled", "true")
        command_mock_bot.command_manager.keywords = {}  # No custom cmd keyword -> use dynamic list
        mock_ping = type("MockCmd", (), {"keywords": ["ping"]})()
        mock_help = type("MockCmd", (), {"keywords": ["help"]})()
        command_mock_bot.command_manager.commands = {"ping": mock_ping, "help": mock_help}
        cmd = CmdCommand(command_mock_bot)
        msg = mock_message(content="cmd", is_dm=True)
        result = await cmd.execute(msg)
        assert result is True
        call_args = command_mock_bot.command_manager.send_response.call_args
        assert call_args is not None
        response = call_args[0][1]
        assert "ping" in response or "help" in response or "cmd" in response

    def test_get_commands_list_truncation(self, command_mock_bot):
        """Test that _get_commands_list truncates long lists with '(N more)' suffix."""
        import re
        command_mock_bot.config.add_section("Cmd_Command")
        command_mock_bot.config.set("Cmd_Command", "enabled", "true")
        command_mock_bot.command_manager.keywords = {}
        # Create 25 mock commands with long names to force truncation
        commands = {}
        for i in range(25):
            name = f"longcommandname{i:02d}"
            mock_cmd = type("MockCmd", (), {"keywords": [name]})()
            commands[name] = mock_cmd
        command_mock_bot.command_manager.commands = commands
        cmd = CmdCommand(command_mock_bot)
        # "Available commands: " = 20 chars; "longcommandnameNN" = 17 chars; ", " = 2 chars
        # 3 commands fit in 75 chars; suffix " (22 more)" = 11 chars; total = 86
        # max_length=90 allows 3 commands + suffix, but not a 4th command
        result = cmd._get_commands_list(max_length=90)
        # Should contain truncation indicator
        assert "more)" in result
        # Should start with prefix
        assert result.startswith("Available commands: ")
        # Should NOT contain doubled numbers like "(5 5 more)"
        assert not re.search(r'\(\d+ \d+ more\)', result)
