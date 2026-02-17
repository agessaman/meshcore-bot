"""Tests for modules.commands.base_command."""

import pytest
from unittest.mock import MagicMock

from modules.commands.base_command import BaseCommand
from modules.commands.ping_command import PingCommand
from modules.commands.dadjoke_command import DadJokeCommand
from modules.commands.joke_command import JokeCommand
from modules.commands.stats_command import StatsCommand
from modules.commands.hacker_command import HackerCommand
from modules.commands.sports_command import SportsCommand
from modules.commands.alert_command import AlertCommand
from modules.models import MeshMessage
from tests.conftest import mock_message


class _TestCommand(BaseCommand):
    """Minimal concrete command for testing BaseCommand behavior."""
    name = "testcmd"
    keywords = ["testcmd"]
    description = "Test"
    short_description = "Test"
    usage = "testcmd"
    examples = ["testcmd"]

    def can_execute(self, message: MeshMessage) -> bool:
        return super().can_execute(message)

    def get_help_text(self) -> str:
        return "Help"

    async def execute(self, message: MeshMessage) -> bool:
        return await self.send_response(message, "ok")


class TestDeriveConfigSectionName:
    """Tests for _derive_config_section_name()."""

    def test_regular_name(self, command_mock_bot):
        cmd = _TestCommand(command_mock_bot)
        cmd.name = "dice"
        assert cmd._derive_config_section_name() == "Dice_Command"

    def test_camel_case_dadjoke(self, command_mock_bot):
        cmd = DadJokeCommand(command_mock_bot)
        assert cmd._derive_config_section_name() == "DadJoke_Command"

    def test_camel_case_webviewer(self, command_mock_bot):
        cmd = _TestCommand(command_mock_bot)
        cmd.name = "webviewer"
        assert cmd._derive_config_section_name() == "WebViewer_Command"


class TestIsChannelAllowed:
    """Tests for is_channel_allowed()."""

    def test_dm_always_allowed(self, command_mock_bot):
        cmd = PingCommand(command_mock_bot)
        msg = mock_message(channel=None, is_dm=True)
        assert cmd.is_channel_allowed(msg) is True

    def test_channel_in_monitor_list_allowed(self, command_mock_bot):
        cmd = PingCommand(command_mock_bot)
        msg = mock_message(channel="general", is_dm=False)
        assert cmd.is_channel_allowed(msg) is True

    def test_channel_not_in_monitor_list_rejected(self, command_mock_bot):
        cmd = PingCommand(command_mock_bot)
        msg = mock_message(channel="unknown_channel", is_dm=False)
        assert cmd.is_channel_allowed(msg) is False

    def test_no_channel_rejected(self, command_mock_bot):
        cmd = PingCommand(command_mock_bot)
        msg = mock_message(channel=None, is_dm=False)
        assert cmd.is_channel_allowed(msg) is False


class TestGetConfigValue:
    """Tests for get_config_value() section migration."""

    def test_new_section_used_first(self, command_mock_bot):
        command_mock_bot.config.add_section("Ping_Command")
        command_mock_bot.config.set("Ping_Command", "enabled", "true")
        cmd = PingCommand(command_mock_bot)
        assert cmd.get_config_value("Ping_Command", "enabled", fallback=False, value_type="bool") is True

    def test_legacy_section_migration(self, command_mock_bot):
        # Old [Hacker] section used when [Hacker_Command] not present
        command_mock_bot.config.add_section("Hacker")
        command_mock_bot.config.set("Hacker", "enabled", "true")
        cmd = _TestCommand(command_mock_bot)
        val = cmd.get_config_value("Hacker_Command", "enabled", fallback=False, value_type="bool")
        assert val is True

    def test_joke_command_enabled_standard(self, command_mock_bot):
        """Joke_Command uses enabled (standard) when present."""
        command_mock_bot.config.add_section("Joke_Command")
        command_mock_bot.config.set("Joke_Command", "enabled", "false")
        cmd = JokeCommand(command_mock_bot)
        assert cmd.joke_enabled is False

    def test_joke_command_joke_enabled_legacy(self, command_mock_bot):
        """Joke_Command falls back to joke_enabled when enabled absent."""
        command_mock_bot.config.add_section("Joke_Command")
        command_mock_bot.config.set("Joke_Command", "joke_enabled", "false")
        cmd = JokeCommand(command_mock_bot)
        assert cmd.joke_enabled is False

    def test_joke_command_legacy_jokes_section(self, command_mock_bot):
        """Joke_Command reads joke_enabled from legacy [Jokes] when enabled absent."""
        command_mock_bot.config.add_section("Jokes")
        command_mock_bot.config.set("Jokes", "joke_enabled", "false")
        cmd = JokeCommand(command_mock_bot)
        assert cmd.joke_enabled is False

    def test_dadjoke_command_enabled_standard(self, command_mock_bot):
        """DadJoke_Command uses enabled (standard) when present."""
        command_mock_bot.config.add_section("DadJoke_Command")
        command_mock_bot.config.set("DadJoke_Command", "enabled", "false")
        cmd = DadJokeCommand(command_mock_bot)
        assert cmd.dadjoke_enabled is False

    def test_dadjoke_command_dadjoke_enabled_legacy(self, command_mock_bot):
        """DadJoke_Command falls back to dadjoke_enabled when enabled absent."""
        command_mock_bot.config.add_section("DadJoke_Command")
        command_mock_bot.config.set("DadJoke_Command", "dadjoke_enabled", "false")
        cmd = DadJokeCommand(command_mock_bot)
        assert cmd.dadjoke_enabled is False

    def test_stats_command_enabled_standard(self, command_mock_bot_with_db):
        """Stats_Command uses enabled (standard) when present."""
        command_mock_bot_with_db.config.add_section("Stats_Command")
        command_mock_bot_with_db.config.set("Stats_Command", "enabled", "false")
        cmd = StatsCommand(command_mock_bot_with_db)
        assert cmd.stats_enabled is False

    def test_stats_command_stats_enabled_legacy(self, command_mock_bot_with_db):
        """Stats_Command falls back to stats_enabled when enabled absent."""
        command_mock_bot_with_db.config.add_section("Stats_Command")
        command_mock_bot_with_db.config.set("Stats_Command", "stats_enabled", "false")
        cmd = StatsCommand(command_mock_bot_with_db)
        assert cmd.stats_enabled is False

    def test_hacker_command_enabled_standard(self, command_mock_bot):
        """Hacker_Command uses enabled (standard) when present."""
        command_mock_bot.config.add_section("Hacker_Command")
        command_mock_bot.config.set("Hacker_Command", "enabled", "true")
        cmd = HackerCommand(command_mock_bot)
        assert cmd.enabled is True

    def test_hacker_command_hacker_enabled_legacy(self, command_mock_bot):
        """Hacker_Command falls back to hacker_enabled when enabled absent."""
        command_mock_bot.config.add_section("Hacker_Command")
        command_mock_bot.config.set("Hacker_Command", "hacker_enabled", "true")
        cmd = HackerCommand(command_mock_bot)
        assert cmd.enabled is True

    def test_sports_command_enabled_standard(self, command_mock_bot):
        """Sports_Command uses enabled (standard) when present."""
        command_mock_bot.config.add_section("Sports_Command")
        command_mock_bot.config.set("Sports_Command", "enabled", "false")
        cmd = SportsCommand(command_mock_bot)
        assert cmd.sports_enabled is False

    def test_sports_command_sports_enabled_legacy(self, command_mock_bot):
        """Sports_Command falls back to sports_enabled when enabled absent."""
        command_mock_bot.config.add_section("Sports_Command")
        command_mock_bot.config.set("Sports_Command", "sports_enabled", "false")
        cmd = SportsCommand(command_mock_bot)
        assert cmd.sports_enabled is False

    def test_alert_command_enabled_standard(self, command_mock_bot):
        """Alert_Command uses enabled (standard) when present."""
        command_mock_bot.config.add_section("Alert_Command")
        command_mock_bot.config.set("Alert_Command", "enabled", "false")
        cmd = AlertCommand(command_mock_bot)
        assert cmd.alert_enabled is False

    def test_alert_command_alert_enabled_legacy(self, command_mock_bot):
        """Alert_Command falls back to alert_enabled when enabled absent."""
        command_mock_bot.config.add_section("Alert_Command")
        command_mock_bot.config.set("Alert_Command", "alert_enabled", "false")
        cmd = AlertCommand(command_mock_bot)
        assert cmd.alert_enabled is False


class TestCanExecute:
    """Tests for can_execute()."""

    def test_channel_check_blocks_unknown_channel(self, command_mock_bot):
        cmd = PingCommand(command_mock_bot)
        msg = mock_message(content="ping", channel="other", is_dm=False)
        assert cmd.can_execute(msg) is False

    def test_dm_allowed(self, command_mock_bot):
        command_mock_bot.config.add_section("Ping_Command")
        command_mock_bot.config.set("Ping_Command", "enabled", "true")
        cmd = PingCommand(command_mock_bot)
        msg = mock_message(content="ping", is_dm=True)
        assert cmd.can_execute(msg) is True
