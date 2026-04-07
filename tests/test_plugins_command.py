"""Tests for modules.commands.plugins_command."""

import configparser
from unittest.mock import MagicMock, Mock

from modules.commands.plugins_command import PluginsCommand
from tests.conftest import mock_message


def _make_bot(services=None):
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
    bot.command_manager.monitor_channels = ["general"]
    bot.services = services if services is not None else {}
    return bot


class TestPluginsCommandBuild:
    def test_no_services(self):
        cmd = PluginsCommand(_make_bot())
        result = cmd._build_list()
        assert "No service plugins loaded" in result

    def test_single_service_with_description(self):
        svc = MagicMock()
        svc.description = "Sends earthquake alerts"
        bot = _make_bot({"earthquake": svc})
        cmd = PluginsCommand(bot)
        result = cmd._build_list()
        assert "earthquake" in result
        assert "Sends earthquake alerts" in result

    def test_single_service_no_description(self):
        svc = MagicMock()
        svc.description = ""
        bot = _make_bot({"discord": svc})
        cmd = PluginsCommand(bot)
        result = cmd._build_list()
        assert "discord" in result

    def test_multiple_services_sorted(self):
        a = MagicMock()
        a.description = "A service"
        b = MagicMock()
        b.description = "B service"
        bot = _make_bot({"zebra": a, "alpha": b})
        cmd = PluginsCommand(bot)
        result = cmd._build_list()
        assert result.index("alpha") < result.index("zebra")

    def test_description_truncated_at_60(self):
        svc = MagicMock()
        svc.description = "x" * 80
        bot = _make_bot({"svc": svc})
        cmd = PluginsCommand(bot)
        result = cmd._build_list()
        assert "..." in result

    def test_header_shows_count(self):
        a = MagicMock()
        a.description = "a"
        b = MagicMock()
        b.description = "b"
        bot = _make_bot({"a": a, "b": b})
        cmd = PluginsCommand(bot)
        result = cmd._build_list()
        assert "Plugins (2)" in result

    def test_disabled_returns_false(self):
        bot = _make_bot()
        bot.config.add_section("Plugins_Command")
        bot.config.set("Plugins_Command", "enabled", "false")
        cmd = PluginsCommand(bot)
        import asyncio
        result = asyncio.run(cmd.execute(mock_message()))
        assert result is False
