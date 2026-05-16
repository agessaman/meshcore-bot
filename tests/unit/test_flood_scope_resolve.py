"""Unit tests for outbound flood scope resolution."""

import configparser
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from modules.command_manager import CommandManager
from modules.models import MeshMessage
from modules.service_plugins.base_service import BaseServicePlugin


def _make_config(**channels_opts: str) -> configparser.ConfigParser:
    config = configparser.ConfigParser()
    config.add_section("Channels")
    for key, val in channels_opts.items():
        config.set("Channels", key, val)
    return config


def _command_manager(config: configparser.ConfigParser) -> CommandManager:
    bot = MagicMock()
    bot.config = config
    bot.logger = Mock()
    cm = object.__new__(CommandManager)
    cm.bot = bot
    cm.logger = bot.logger
    cm.flood_scope_allow_global = False
    cm.flood_scope_keys = {}
    return cm


class TestResolveChannelSendScope:
    def test_explicit_scope_wins(self):
        cm = _command_manager(_make_config())
        assert cm.resolve_channel_send_scope(scope="#west") == "#west"

    def test_message_reply_scope_when_scope_arg_none(self):
        cm = _command_manager(_make_config())
        msg = MeshMessage(content="x", channel="general", is_dm=False, reply_scope="#east")
        assert cm.resolve_channel_send_scope(scope=None, message=msg) == "#east"

    def test_config_section_flood_scope(self):
        config = configparser.ConfigParser()
        config.add_section("Channels")
        config.add_section("Weather_Service")
        config.set("Weather_Service", "flood_scope", "west")
        cm = _command_manager(config)
        assert cm.resolve_channel_send_scope(
            scope=None, config_section="Weather_Service"
        ) == "#west"

    def test_returns_none_for_override_fallback(self):
        cm = _command_manager(_make_config(outgoing_flood_scope_override="#west"))
        assert cm.resolve_channel_send_scope(scope=None) is None

    def test_precedence_explicit_over_message(self):
        cm = _command_manager(_make_config())
        msg = MeshMessage(content="x", channel="general", is_dm=False, reply_scope="#east")
        assert cm.resolve_channel_send_scope(scope="#west", message=msg) == "#west"


class _StubService(BaseServicePlugin):
    config_section = "Weather_Service"

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


class TestGetMeshFloodScope:
    def test_reads_and_normalizes_section_key(self):
        config = configparser.ConfigParser()
        config.add_section("Weather_Service")
        config.set("Weather_Service", "flood_scope", "#sea")
        bot = MagicMock()
        bot.config = config
        bot.logger = Mock()
        svc = _StubService(bot)
        assert svc.get_mesh_flood_scope() == "#sea"

    def test_empty_returns_none(self):
        config = configparser.ConfigParser()
        config.add_section("Weather_Service")
        bot = MagicMock()
        bot.config = config
        bot.logger = Mock()
        svc = _StubService(bot)
        assert svc.get_mesh_flood_scope() is None


@pytest.mark.asyncio
async def test_send_channel_message_applies_override_when_resolve_returns_none():
    config = _make_config(outgoing_flood_scope_override="west")
    bot = MagicMock()
    bot.config = config
    bot.logger = Mock()
    bot.connected = True
    bot.is_radio_zombie = False
    bot.is_radio_offline = False
    bot.channel_manager.get_channel_number.return_value = 1

    set_flood_scope = AsyncMock(return_value=MagicMock(type="OK"))
    send_chan_msg = AsyncMock(return_value=MagicMock(type="OK", payload={}))
    bot.meshcore = MagicMock()
    bot.meshcore.commands.set_flood_scope = set_flood_scope
    bot.meshcore.commands.send_chan_msg = send_chan_msg

    cm = object.__new__(CommandManager)
    cm.bot = bot
    cm.logger = bot.logger
    cm.flood_scope_allow_global = False
    cm.flood_scope_keys = {}
    cm._check_rate_limits = AsyncMock(return_value=(True, None))
    cm._handle_send_result = MagicMock(return_value=True)
    cm._is_no_event_received = MagicMock(return_value=False)

    await cm.send_channel_message("general", "hi", scope=None)

    set_flood_scope.assert_awaited()
    assert set_flood_scope.await_args_list[0].args[0] == "#west"
