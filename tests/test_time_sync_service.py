"""Tests for the authenticated time-sync source service."""

from __future__ import annotations

import configparser
from unittest.mock import AsyncMock, Mock

import pytest

from modules.service_plugins.time_sync_service import TimeSyncService


BOT_PUBLIC_KEY = "22" * 32


class _ChannelManager:
    def __init__(self, channels: set[str] | None = None):
        self.channels = channels or set()

    def get_channel_number(self, channel: str):
        return 1 if channel in self.channels else None

    def get_channel_by_name(self, channel: str):
        if channel in self.channels:
            return {
                "channel_name": channel,
                "channel_key_hex": "5d13043d9a5e61bc61aeb63208f5c64e",
            }
        return None


def _make_bot(
    *,
    enabled: bool = True,
    channel: str = "#time",
    identity_name: str = "TimeBot",
    flood_scope: str = "#local",
    full_flood_enabled: bool = False,
    text_broadcast_enabled: bool = False,
    text_channel: str = "",
    known_channels: set[str] | None = None,
):
    config = configparser.ConfigParser()
    config.add_section("Bot")
    config.set("Bot", "bot_name", identity_name)
    config.add_section("Time_Sync")
    config.set("Time_Sync", "enabled", "true" if enabled else "false")
    config.set("Time_Sync", "channel", channel)
    config.set("Time_Sync", "sequence", "12345")
    config.set("Time_Sync", "flood_scope", flood_scope)
    config.set("Time_Sync", "full_flood_enabled", "true" if full_flood_enabled else "false")
    config.set("Time_Sync", "text_broadcast_enabled", "true" if text_broadcast_enabled else "false")
    config.set("Time_Sync", "text_channel", text_channel)

    bot = Mock()
    bot.config = config
    bot.logger = Mock()
    bot.meshcore = Mock()
    bot.meshcore.self_info = {"public_key": BOT_PUBLIC_KEY, "name": identity_name}
    bot.meshcore.commands = Mock()
    bot.meshcore.commands.sign = AsyncMock(return_value=Mock(payload={"signature": b"\x33" * 64}))
    if known_channels is None:
        known_channels = {name for name in (channel, text_channel) if name and name != "#missing"}
    bot.channel_manager = _ChannelManager(channels=known_channels)
    bot.db_manager = Mock()
    bot.db_manager.get_metadata = Mock(return_value=None)
    bot.db_manager.set_metadata = Mock()
    bot.command_manager = Mock()
    bot.command_manager.send_group_datagram = AsyncMock(return_value=True)
    bot.command_manager.send_channel_message = AsyncMock(return_value=True)
    return bot


@pytest.mark.asyncio
async def test_disabled_source_sends_nothing():
    bot = _make_bot(enabled=False)
    service = TimeSyncService(bot)

    await service.start()

    assert service._running is False
    bot.command_manager.send_group_datagram.assert_not_awaited()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("channel", ""),
        ("channel", "#missing"),
    ],
)
def test_enabling_fails_with_incomplete_or_invalid_config(field, value):
    kwargs = {field: value}
    bot = _make_bot(**kwargs)
    service = TimeSyncService(bot)

    with pytest.raises(Exception):
        service._load_settings()


def test_missing_channel_error_explains_radio_channel_requirement():
    bot = _make_bot(channel="#missing")
    service = TimeSyncService(bot)

    with pytest.raises(Exception) as exc_info:
        service._load_settings()

    message = str(exc_info.value)
    assert "was not found in the MeshCore channel cache" in message
    assert "add it to the radio's configured channels" in message


@pytest.mark.parametrize("identity_name", ["", "Time\nBot", "a" * 21])
def test_enabling_fails_with_invalid_existing_identity_name(identity_name):
    bot = _make_bot(identity_name=identity_name)
    service = TimeSyncService(bot)

    with pytest.raises(Exception):
        service._load_settings()


def test_display_name_is_derived_from_existing_bot_identity_not_timesync_config():
    bot = _make_bot(identity_name="RadioName")
    bot.config.set("Time_Sync", "display_name", "IgnoredName")
    service = TimeSyncService(bot)

    settings = service._load_settings()

    assert settings.identity_name == "RadioName"


def test_identity_name_falls_back_to_bot_name_when_radio_name_unavailable():
    bot = _make_bot(identity_name="ConfigName")
    bot.meshcore.self_info = {"public_key": BOT_PUBLIC_KEY}
    service = TimeSyncService(bot)

    settings = service._load_settings()

    assert settings.identity_name == "ConfigName"


@pytest.mark.asyncio
async def test_send_once_uses_group_datagram_and_advances_sequence():
    bot = _make_bot()
    service = TimeSyncService(bot)
    service._settings = service._load_settings()
    service._sequence = 65535

    sent = await service.send_once(timestamp=1783862400)

    assert sent is True
    assert service._sequence == 0
    bot.command_manager.send_group_datagram.assert_awaited_once()
    bot.command_manager.send_channel_message.assert_not_awaited()
    args = bot.command_manager.send_group_datagram.await_args.args
    kwargs = bot.command_manager.send_group_datagram.await_args.kwargs
    assert args[0] == "#time"
    assert args[1] == 0x0121
    assert args[2].startswith(b"Tv1")
    assert kwargs["scope"] == "#local"


@pytest.mark.asyncio
async def test_manual_send_once_reloads_full_flood_setting_before_send():
    bot = _make_bot(flood_scope="#local")
    service = TimeSyncService(bot)
    service._settings = service._load_settings()
    service._sequence = 7

    bot.config.set("Time_Sync", "full_flood_enabled", "true")
    bot.config.set("Time_Sync", "flood_scope", "")

    sent = await service.send_once(timestamp=1783862400)

    assert sent is True
    kwargs = bot.command_manager.send_group_datagram.await_args.kwargs
    assert kwargs["scope"] == "*"


@pytest.mark.asyncio
async def test_periodic_send_once_can_keep_loaded_settings():
    bot = _make_bot(flood_scope="#local")
    service = TimeSyncService(bot)
    service._settings = service._load_settings()
    service._sequence = 8

    bot.config.set("Time_Sync", "full_flood_enabled", "true")
    bot.config.set("Time_Sync", "flood_scope", "")

    sent = await service.send_once(timestamp=1783862400, reload_settings=False)

    assert sent is True
    kwargs = bot.command_manager.send_group_datagram.await_args.kwargs
    assert kwargs["scope"] == "#local"


@pytest.mark.asyncio
async def test_manual_send_once_force_persists_current_sequence():
    bot = _make_bot()
    service = TimeSyncService(bot)
    service._settings = service._load_settings()
    service._sequence = 20
    service._last_sequence_persist = 1783862399

    sent = await service.send_once(timestamp=1783862400)

    assert sent is True
    assert service._sequence == 21
    bot.db_manager.set_metadata.assert_called_with("time_sync.sequence", "21")


def test_default_time_sync_requires_regional_flood_scope():
    bot = _make_bot(flood_scope="")
    service = TimeSyncService(bot)

    with pytest.raises(Exception) as exc_info:
        service._load_settings()

    assert "regional flood_scope is required" in str(exc_info.value)


def test_full_flood_opt_in_allows_global_scope():
    bot = _make_bot(flood_scope="", full_flood_enabled=True)
    service = TimeSyncService(bot)

    settings = service._load_settings()

    assert settings.full_flood_enabled is True
    assert settings.flood_scope == "*"


@pytest.mark.asyncio
async def test_send_once_optionally_sends_human_readable_text_broadcast():
    bot = _make_bot(text_broadcast_enabled=True, text_channel="#time-text")
    service = TimeSyncService(bot)
    service._settings = service._load_settings()
    service._sequence = 42

    sent = await service.send_once(timestamp=1783862400)

    assert sent is True
    assert service._sequence == 43
    bot.command_manager.send_group_datagram.assert_awaited_once()
    bot.command_manager.send_channel_message.assert_awaited_once()
    args = bot.command_manager.send_channel_message.await_args.args
    kwargs = bot.command_manager.send_channel_message.await_args.kwargs
    assert args[0] == "#time-text"
    assert args[1] == "Time sync: 2026-07-12 13:20:00 UTC"
    assert "unix 1783862400" not in args[1]
    assert "seq 42" not in args[1]
    assert "source TimeBot" not in args[1]
    assert kwargs["command_id"] == "time_sync_text_#time-text_1783862400_42"
    assert kwargs["skip_user_rate_limit"] is True
    assert kwargs["scope"] == "#local"
    assert kwargs["timestamp"].isoformat() == "2026-07-12T13:20:00+00:00"


@pytest.mark.asyncio
async def test_text_broadcast_failure_does_not_fail_datagram_send():
    bot = _make_bot(text_broadcast_enabled=True)
    bot.command_manager.send_channel_message = AsyncMock(return_value=False)
    service = TimeSyncService(bot)
    service._settings = service._load_settings()
    service._sequence = 9

    sent = await service.send_once(timestamp=1783862400)

    assert sent is True
    assert service._sequence == 10
    bot.command_manager.send_group_datagram.assert_awaited_once()
    bot.command_manager.send_channel_message.assert_awaited_once()
    assert bot.command_manager.send_channel_message.await_args.args[0] == "#time"


@pytest.mark.asyncio
async def test_text_broadcast_exception_does_not_fail_datagram_send():
    bot = _make_bot(text_broadcast_enabled=True)
    bot.command_manager.send_channel_message = AsyncMock(side_effect=RuntimeError("radio busy"))
    service = TimeSyncService(bot)
    service._settings = service._load_settings()
    service._sequence = 10

    sent = await service.send_once(timestamp=1783862400)

    assert sent is True
    assert service._sequence == 11
    bot.command_manager.send_group_datagram.assert_awaited_once()
    bot.command_manager.send_channel_message.assert_awaited_once()


def test_enabled_text_broadcast_rejects_unknown_text_channel():
    bot = _make_bot(
        text_broadcast_enabled=True,
        text_channel="#unknown-text",
        known_channels={"#time"},
    )
    service = TimeSyncService(bot)

    with pytest.raises(Exception) as exc_info:
        service._load_settings()

    message = str(exc_info.value)
    assert "text_channel '#unknown-text' was not found" in message
    assert "leave it blank to use the datagram channel" in message


def test_default_interval_is_one_week():
    bot = _make_bot()
    service = TimeSyncService(bot)

    settings = service._load_settings()

    assert settings.interval_seconds == 604800
