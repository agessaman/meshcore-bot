"""Tests for modules.commands.status_command."""

import configparser
import time
from unittest.mock import MagicMock, Mock

from modules.commands.status_command import StatusCommand
from tests.conftest import mock_message


def _make_bot(start_time=None):
    bot = MagicMock()
    bot.logger = Mock()
    config = configparser.ConfigParser()
    config.add_section("Bot")
    config.set("Bot", "bot_name", "TestBot")
    config.add_section("Channels")
    config.set("Channels", "monitor_channels", "general")
    config.set("Channels", "respond_to_dms", "true")
    config.add_section("Keywords")
    bot.config = config
    bot.translator = MagicMock()
    bot.translator.translate = Mock(side_effect=lambda key, **kw: key)
    bot.command_manager = MagicMock()
    bot.command_manager.monitor_channels = ["general", "local"]
    bot.start_time = start_time if start_time is not None else time.time() - 3700
    bot.is_radio_zombie = False
    bot.is_radio_offline = False
    bot.services = {}
    db = MagicMock()
    db.db_path = ":memory:"
    bot.db_manager = db
    return bot


class TestStatusCommandBuild:
    def setup_method(self):
        self.bot = _make_bot()
        self.cmd = StatusCommand(self.bot)

    def test_uptime_hours_minutes(self):
        self.bot.start_time = time.time() - (2 * 3600 + 15 * 60)
        result = self.cmd._build_status()
        assert "Up:" in result
        assert "2h" in result

    def test_uptime_days(self):
        self.bot.start_time = time.time() - (25 * 3600)
        result = self.cmd._build_status()
        assert "1d" in result

    def test_radio_ok(self):
        self.bot.is_radio_zombie = False
        self.bot.is_radio_offline = False
        result = self.cmd._build_status()
        assert "Radio: ok" in result

    def test_radio_zombie(self):
        self.bot.is_radio_zombie = True
        result = self.cmd._build_status()
        assert "Radio: ZOMBIE" in result

    def test_radio_offline(self):
        self.bot.is_radio_zombie = False
        self.bot.is_radio_offline = True
        result = self.cmd._build_status()
        assert "Radio: OFFLINE" in result

    def test_channel_count(self):
        result = self.cmd._build_status()
        assert "Channels: 2" in result

    def test_services_none(self):
        self.bot.services = {}
        result = self.cmd._build_status()
        assert "Services: none" in result

    def test_services_listed(self):
        svc = MagicMock()
        svc.description = "Earthquake alerts"
        self.bot.services = {"earthquake": svc}
        result = self.cmd._build_status()
        assert "Services: 1" in result
        assert "earthquake" in result

    def test_is_dm_only(self):
        assert self.cmd.requires_dm is True

    def test_disabled_returns_false(self):
        self.bot.config.set("Status_Command", "enabled", "false") if self.bot.config.has_section("Status_Command") else None
        if not self.bot.config.has_section("Status_Command"):
            self.bot.config.add_section("Status_Command")
        self.bot.config.set("Status_Command", "enabled", "false")
        cmd = StatusCommand(self.bot)
        import asyncio
        result = asyncio.run(cmd.execute(mock_message()))
        assert result is False


class TestStatusCommandIsCallable:
    """is_radio_zombie/offline can be properties (bool) or callables."""

    def test_callable_zombie(self):
        bot = _make_bot()
        bot.is_radio_zombie = lambda: True
        cmd = StatusCommand(bot)
        result = cmd._build_status()
        assert "Radio: ZOMBIE" in result

    def test_callable_offline(self):
        bot = _make_bot()
        bot.is_radio_zombie = lambda: False
        bot.is_radio_offline = lambda: True
        cmd = StatusCommand(bot)
        result = cmd._build_status()
        assert "Radio: OFFLINE" in result
