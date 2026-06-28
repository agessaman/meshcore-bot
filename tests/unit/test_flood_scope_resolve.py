"""Unit tests for outbound flood scope resolution."""

import configparser
from unittest.mock import AsyncMock, MagicMock, Mock, patch

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

    def test_channel_flood_scope_normalizes_channel_and_scope(self):
        config = _make_config()
        config.set("Channels", "flood_scope.weather", "sea")
        cm = _command_manager(config)

        assert cm.resolve_channel_send_scope(channel="#Weather") == "#sea"

    def test_channel_global_marker_overrides_global_fallback(self):
        config = _make_config(outgoing_flood_scope_override="#west")
        config.set("Channels", "flood_scope.weather", "*")
        cm = _command_manager(config)

        assert cm.resolve_channel_send_scope(channel="weather") == "*"

    def test_precedence_explicit_over_message(self):
        cm = _command_manager(_make_config())
        msg = MeshMessage(content="x", channel="general", is_dm=False, reply_scope="#east")
        assert cm.resolve_channel_send_scope(scope="#west", message=msg) == "#west"

    def test_precedence_reply_and_config_section_over_channel(self):
        config = _make_config()
        config.set("Channels", "flood_scope.weather", "#channel")
        config.add_section("Weather_Service")
        config.set("Weather_Service", "flood_scope", "#plugin")
        cm = _command_manager(config)
        msg = MeshMessage(content="x", channel="weather", is_dm=False, reply_scope="#reply")

        assert cm.resolve_channel_send_scope(message=msg, config_section="Weather_Service") == "#reply"
        assert cm.resolve_channel_send_scope(
            config_section="Weather_Service", channel="weather"
        ) == "#plugin"


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


@pytest.mark.asyncio
async def test_send_channel_message_applies_channel_scope():
    config = _make_config(outgoing_flood_scope_override="#west")
    config.set("Channels", "flood_scope.weather", "sea")
    bot = MagicMock()
    bot.config = config
    bot.logger = Mock()
    bot.connected = True
    bot.is_radio_zombie = False
    bot.is_radio_offline = False
    bot.channel_manager.get_channel_number.return_value = 2

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

    await cm.send_channel_message("#Weather", "hi", scope=None)

    set_flood_scope.assert_awaited()
    assert set_flood_scope.await_args_list[0].args[0] == "#sea"


# ---------------------------------------------------------------------------
# Shared helpers for Path-F and Path-G tests
# ---------------------------------------------------------------------------

def _make_scoped_cm(scope_str: str = "west"):
    """CommandManager wired for send_channel_message unit tests with a regional scope."""
    config = _make_config(outgoing_flood_scope_override=scope_str)
    bot = MagicMock()
    bot.config = config
    bot.logger = Mock()
    bot.connected = True
    bot.is_radio_zombie = False
    bot.is_radio_offline = False
    bot.channel_manager.get_channel_number.return_value = 1
    bot.meshcore = MagicMock()

    cm = object.__new__(CommandManager)
    cm.bot = bot
    cm.logger = bot.logger
    cm.flood_scope_allow_global = False
    cm.flood_scope_keys = {}
    cm._check_rate_limits = AsyncMock(return_value=(True, None))
    cm._handle_send_result = MagicMock(return_value=True)
    return cm, bot


def _ok_result():
    r = MagicMock()
    r.type = "OK"
    r.payload = {}
    return r


def _scope_error_result():
    r = MagicMock()
    r.type = "ERROR"
    r.payload = {}
    return r


def _no_event_result():
    """Result that the real _is_no_event_received treats as a retry trigger."""
    from meshcore import EventType
    r = MagicMock()
    r.type = EventType.ERROR
    r.payload = {"reason": "no_event_received"}
    return r


# ---------------------------------------------------------------------------
# Path F: firmware rejects SET_FLOOD_SCOPE
# ---------------------------------------------------------------------------

class TestSetFloodScopeResultHandling:
    """set_flood_scope result checking — warning logged on ERROR/None, send still proceeds."""

    @pytest.mark.asyncio
    async def test_error_result_logs_warning_and_still_sends(self):
        """set_flood_scope returning type=ERROR: warning logged, send_chan_msg still called."""
        cm, bot = _make_scoped_cm("west")
        bot.meshcore.commands.set_flood_scope = AsyncMock(return_value=_scope_error_result())
        bot.meshcore.commands.send_chan_msg = AsyncMock(return_value=_ok_result())
        cm._is_no_event_received = MagicMock(return_value=False)

        result = await cm.send_channel_message("general", "hi", scope=None)

        assert result is True
        bot.meshcore.commands.send_chan_msg.assert_awaited_once()
        warning_calls = str(bot.logger.warning.call_args_list)
        assert "set_flood_scope" in warning_calls
        assert "#west" in warning_calls

    @pytest.mark.asyncio
    async def test_none_result_logs_warning_and_still_sends(self):
        """set_flood_scope returning None: warning logged, send_chan_msg still called."""
        cm, bot = _make_scoped_cm("west")
        bot.meshcore.commands.set_flood_scope = AsyncMock(return_value=None)
        bot.meshcore.commands.send_chan_msg = AsyncMock(return_value=_ok_result())
        cm._is_no_event_received = MagicMock(return_value=False)

        result = await cm.send_channel_message("general", "hi", scope=None)

        assert result is True
        bot.meshcore.commands.send_chan_msg.assert_awaited_once()
        assert any(
            "set_flood_scope" in str(c.args)
            for c in bot.logger.warning.call_args_list
        )

    @pytest.mark.asyncio
    async def test_restore_failure_logs_warning(self):
        """set_flood_scope('*') restore returning ERROR: warning logged."""
        cm, bot = _make_scoped_cm("west")
        # Pre-send succeeds; restore-to-global fails
        bot.meshcore.commands.set_flood_scope = AsyncMock(
            side_effect=[_ok_result(), _scope_error_result()]
        )
        bot.meshcore.commands.send_chan_msg = AsyncMock(return_value=_ok_result())
        cm._is_no_event_received = MagicMock(return_value=False)

        await cm.send_channel_message("general", "hi", scope=None)

        assert any(
            "restore" in str(c.args)
            for c in bot.logger.warning.call_args_list
        )

    @pytest.mark.asyncio
    async def test_ok_result_no_scope_failure_warning(self):
        """set_flood_scope returning OK: no scope-failure warning logged."""
        cm, bot = _make_scoped_cm("west")
        bot.meshcore.commands.set_flood_scope = AsyncMock(return_value=_ok_result())
        bot.meshcore.commands.send_chan_msg = AsyncMock(return_value=_ok_result())
        cm._is_no_event_received = MagicMock(return_value=False)

        await cm.send_channel_message("general", "hi", scope=None)

        assert not any(
            "set_flood_scope" in str(c.args) and "failed" in str(c.args)
            for c in bot.logger.warning.call_args_list
        )


# ---------------------------------------------------------------------------
# Path G: retry re-applies scope
# ---------------------------------------------------------------------------

class TestRetryReappliesScope:
    """set_flood_scope(scope) is re-applied before each retry attempt."""

    @pytest.mark.asyncio
    async def test_scope_call_sequence_on_retry(self):
        """no_event_received on attempt 0: full set_flood_scope call sequence is correct.

        Expected: set(scope) → send[fail] → set(*) → set(scope) → send[ok] → set(*)
        """
        cm, bot = _make_scoped_cm("west")
        bot.meshcore.commands.set_flood_scope = AsyncMock(return_value=_ok_result())
        bot.meshcore.commands.send_chan_msg = AsyncMock(
            side_effect=[_no_event_result(), _ok_result()]
        )

        with patch("modules.command_manager.asyncio.sleep", new_callable=AsyncMock):
            result = await cm.send_channel_message("general", "hi", scope=None)

        assert result is True
        calls = [c.args[0] for c in bot.meshcore.commands.set_flood_scope.await_args_list]
        assert calls == ["#west", "*", "#west", "*"]

    @pytest.mark.asyncio
    async def test_retry_reapply_failure_logs_warning(self):
        """set_flood_scope fails on retry re-apply: 'retry re-apply' warning logged."""
        cm, bot = _make_scoped_cm("west")
        # pre-send OK, restore OK, re-apply ERROR, second restore OK
        bot.meshcore.commands.set_flood_scope = AsyncMock(
            side_effect=[_ok_result(), _ok_result(), _scope_error_result(), _ok_result()]
        )
        bot.meshcore.commands.send_chan_msg = AsyncMock(
            side_effect=[_no_event_result(), _ok_result()]
        )

        with patch("modules.command_manager.asyncio.sleep", new_callable=AsyncMock):
            await cm.send_channel_message("general", "hi", scope=None)

        assert any(
            "retry re-apply" in str(c.args)
            for c in bot.logger.warning.call_args_list
        )

    @pytest.mark.asyncio
    async def test_all_retries_exhaust_scope_still_restored(self):
        """All 3 attempts fail: set_flood_scope('*') still called after each attempt."""
        cm, bot = _make_scoped_cm("west")
        bot.meshcore.commands.set_flood_scope = AsyncMock(return_value=_ok_result())
        bot.meshcore.commands.send_chan_msg = AsyncMock(return_value=_no_event_result())
        cm._handle_send_result = MagicMock(return_value=False)

        with patch("modules.command_manager.asyncio.sleep", new_callable=AsyncMock):
            result = await cm.send_channel_message("general", "hi", scope=None)

        assert result is False
        restore_calls = [
            c.args[0] for c in bot.meshcore.commands.set_flood_scope.await_args_list
            if c.args[0] == "*"
        ]
        # One restore per attempt (3 attempts total)
        assert len(restore_calls) == 3
