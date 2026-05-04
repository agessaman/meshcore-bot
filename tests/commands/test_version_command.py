"""Tests for modules.commands.version_command."""

import pytest

from modules.commands.version_command import VersionCommand
from tests.conftest import mock_message


class TestVersionCommand:
    """Tests for VersionCommand."""

    def test_can_execute_when_enabled(self, command_mock_bot):
        command_mock_bot.config.add_section("Version_Command")
        command_mock_bot.config.set("Version_Command", "enabled", "true")
        cmd = VersionCommand(command_mock_bot)
        msg = mock_message(content="version", is_dm=True)
        assert cmd.can_execute(msg) is True

    def test_can_execute_when_disabled(self, command_mock_bot):
        command_mock_bot.config.add_section("Version_Command")
        command_mock_bot.config.set("Version_Command", "enabled", "false")
        cmd = VersionCommand(command_mock_bot)
        msg = mock_message(content="version", is_dm=True)
        assert cmd.can_execute(msg) is False

    @pytest.mark.asyncio
    async def test_execute_returns_bot_version(self, command_mock_bot):
        command_mock_bot.config.add_section("Version_Command")
        command_mock_bot.config.set("Version_Command", "enabled", "true")
        command_mock_bot.bot_version = "dev-abc1234"

        cmd = VersionCommand(command_mock_bot)
        msg = mock_message(content="version", is_dm=True)
        result = await cmd.execute(msg)

        assert result is True
        call_args = command_mock_bot.command_manager.send_response.call_args
        assert call_args is not None
        assert call_args[0][1] == "@[TestUser] Bot version: dev-abc1234"

    @pytest.mark.asyncio
    async def test_execute_falls_back_to_resolver(self, command_mock_bot, monkeypatch):
        command_mock_bot.config.add_section("Version_Command")
        command_mock_bot.config.set("Version_Command", "enabled", "true")
        command_mock_bot.bot_version = None
        command_mock_bot.bot_root = "."
        monkeypatch.setattr(
            "modules.commands.version_command.resolve_runtime_version",
            lambda _root: {"display": "v0.9"},
        )

        cmd = VersionCommand(command_mock_bot)
        msg = mock_message(content="ver", is_dm=True)
        result = await cmd.execute(msg)

        assert result is True
        call_args = command_mock_bot.command_manager.send_response.call_args
        assert call_args is not None
        assert call_args[0][1] == "@[TestUser] Bot version: v0.9"

