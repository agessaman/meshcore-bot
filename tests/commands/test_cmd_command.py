"""Tests for modules.commands.cmd_command."""

import pytest

from modules.commands.alert_command import AlertCommand
from modules.commands.cmd_command import CmdCommand
from modules.commands.ping_command import PingCommand
from modules.commands.sports_command import SportsCommand
from modules.commands.trace_command import TraceCommand
from modules.commands.wx_command import WxCommand
from tests.conftest import mock_message


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
    async def test_is_not_listed_when_sports_cmd_disabled(self, command_mock_bot):
        command_mock_bot.config.add_section("Cmd_Command")
        command_mock_bot.config.set("Cmd_Command", "enabled", "true")
        command_mock_bot.config.add_section("Ping_Command")
        command_mock_bot.config.set("Ping_Command", "enabled", "true")
        command_mock_bot.config.add_section("Wx_Command")
        command_mock_bot.config.set("Wx_Command", "enabled", "true")
        command_mock_bot.config.add_section("Sports_Command")
        command_mock_bot.config.set("Sports_Command", "enabled", "false")
        command_mock_bot.config.add_section("Trace_Command")
        command_mock_bot.config.set("Trace_Command", "enabled", "true")
        command_mock_bot.config.add_section("Alert_Command")
        command_mock_bot.config.set("Alert_Command", "enabled", "true")

        command_mock_bot.command_manager.keywords = {}
        command_mock_bot.command_manager.commands = {
            'ping': PingCommand(command_mock_bot),
            'wx': WxCommand(command_mock_bot),
            'sports': SportsCommand(command_mock_bot),
            'trace': TraceCommand(command_mock_bot),
            'alert': AlertCommand(command_mock_bot)
        }

        cmd = CmdCommand(command_mock_bot)
        msg = mock_message(content="cmd", is_dm=True)
        result = await cmd.execute(msg)
        call_args = command_mock_bot.command_manager.send_response.call_args

        assert result is True
        assert 'ping' in call_args[0][1]
        assert 'wx' in call_args[0][1]
        assert 'sports' not in call_args[0][1]
        assert 'trace' in call_args[0][1]
        assert 'alert' in call_args[0][1]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("disabled_value", ["false", "no", "0", "False", "FALSE", "off"])
    async def test_is_not_listed_for_any_disabled_spelling(self, command_mock_bot, disabled_value):
        """config.ini accepts every boolean spelling configparser does, not just 'false'."""
        command_mock_bot.config.add_section("Cmd_Command")
        command_mock_bot.config.set("Cmd_Command", "enabled", "true")
        command_mock_bot.config.add_section("Ping_Command")
        command_mock_bot.config.set("Ping_Command", "enabled", "true")
        command_mock_bot.config.add_section("Sports_Command")
        command_mock_bot.config.set("Sports_Command", "enabled", disabled_value)

        command_mock_bot.command_manager.keywords = {}
        command_mock_bot.command_manager.commands = {
            'ping': PingCommand(command_mock_bot),
            'sports': SportsCommand(command_mock_bot),
        }

        cmd = CmdCommand(command_mock_bot)
        msg = mock_message(content="cmd", is_dm=True)
        result = await cmd.execute(msg)
        call_args = command_mock_bot.command_manager.send_response.call_args

        assert result is True
        assert 'ping' in call_args[0][1]
        assert 'sports' not in call_args[0][1]

    @pytest.mark.asyncio
    async def test_is_listed_when_command_has_no_config_section(self, command_mock_bot):
        """A command with no [<Name>_Command] section defaults to enabled, as before."""
        command_mock_bot.config.add_section("Cmd_Command")
        command_mock_bot.config.set("Cmd_Command", "enabled", "true")

        command_mock_bot.command_manager.keywords = {}
        command_mock_bot.command_manager.commands = {
            'ping': PingCommand(command_mock_bot),
            'sports': SportsCommand(command_mock_bot),
        }

        cmd = CmdCommand(command_mock_bot)
        msg = mock_message(content="cmd", is_dm=True)
        result = await cmd.execute(msg)
        call_args = command_mock_bot.command_manager.send_response.call_args

        assert result is True
        assert 'ping' in call_args[0][1]
        assert 'sports' in call_args[0][1]

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

    @pytest.mark.asyncio
    async def test_execute_returns_reference_url_when_configured(self, command_mock_bot):
        command_mock_bot.config.add_section("Cmd_Command")
        command_mock_bot.config.set("Cmd_Command", "enabled", "true")
        command_mock_bot.config.set("Cmd_Command", "cmd_reference_url", "https://example.com/commands")
        command_mock_bot.command_manager.keywords = {}
        command_mock_bot.command_manager.commands = {"ping": type("MockCmd", (), {"keywords": ["ping"]})()}
        cmd = CmdCommand(command_mock_bot)
        msg = mock_message(content="cmd", is_dm=True)

        result = await cmd.execute(msg)

        assert result is True
        call_args = command_mock_bot.command_manager.send_response.call_args
        assert call_args is not None
        assert call_args[0][1] == "Full command reference: https://example.com/commands"

    @pytest.mark.asyncio
    async def test_execute_reference_url_takes_precedence_over_custom_keyword(self, command_mock_bot):
        command_mock_bot.config.add_section("Cmd_Command")
        command_mock_bot.config.set("Cmd_Command", "enabled", "true")
        command_mock_bot.config.set("Cmd_Command", "cmd_reference_url", "https://example.com/commands")
        command_mock_bot.command_manager.keywords = {"cmd": "Custom cmd output"}
        command_mock_bot.command_manager.commands = {"ping": type("MockCmd", (), {"keywords": ["ping"]})()}
        cmd = CmdCommand(command_mock_bot)
        msg = mock_message(content="cmd", is_dm=True)

        result = await cmd.execute(msg)

        assert result is True
        call_args = command_mock_bot.command_manager.send_response.call_args
        assert call_args is not None
        assert call_args[0][1] == "Full command reference: https://example.com/commands"

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
            commands[name] = CmdCommand(command_mock_bot)
            command_mock_bot.config.add_section(name.title() + "_Command")
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
